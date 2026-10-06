"""End-to-end update: a local release server, a real helper, a real port.

The driver process is the stand-in for ``yaver update``. It downloads the
zip, checks the checksum, and starts the helper the same way the product
does. The helper then replaces a temporary install. Nothing here binds
the live dashboard, and the install is never this git checkout.

On Windows the Linux helper runs under Git bash. The test checks the
swap, the saved ``.env``, and the data folder. The helper does not start
Yaver, including when the plan names a command to run afterwards.

On Windows, tests hold a file inside ``_internal`` open. Plan pid 0
makes the PowerShell helper and the Python helper try the move while
that handle is open, and Windows denies it for about ten seconds. The
same helper with that process's pid waits, then moves the folder after
the handle is closed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import socket
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.self_update import UpdateError, _write_shell_plan
from src.update_helper import run_plan

_BUSY_PORTS = {8080, 5173, 4096, 8090}
_ROOT = Path(__file__).resolve().parents[1]
_HELPER_SH = _ROOT / "src" / "update_helper.sh"
_HELPER_PS1 = _ROOT / "src" / "update_helper.ps1"
_HELPER_PY = _ROOT / "src" / "update_helper.py"
_POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")

_DRIVER = r"""
import os
import sys
import traceback
from pathlib import Path

base, install, work, port, listener, layout, probe, root = sys.argv[1:9]
root_path = Path(root)
sys.path.insert(0, str(root_path))
os.chdir(root_path)
work_path = Path(work)
work_path.mkdir(parents=True, exist_ok=True)
try:
    install_path = Path(install).resolve()
    if install_path == root_path or root_path in install_path.parents:
        raise RuntimeError("refusing to update the git checkout")
    chosen = int(port)
    if chosen in {8080, 5173, 4096, 8090}:
        raise RuntimeError("refusing a live Yaver port")
    import src
    import src.self_update as su

    su.install_root = lambda: install_path
    su._work_dir = lambda: work_path
    su._dashboard_probe = lambda: (probe, chosen)
    marker = str(work_path / "listening.txt")
    su._restart_argv = lambda: [
        sys.executable,
        listener,
        probe,
        str(chosen),
        marker,
        "yaver-e2e-marker",
    ]
    if layout == "frozen":
        su.local_layout = lambda: "frozen"
    real_plan = su._plan

    def _plan(*args, **kwargs):
        built = real_plan(*args, **kwargs)
        built["health_seconds"] = 15
        return built

    su._plan = _plan
    src.__version__ = "0.9.0"
    su.run_apply_job(base, lambda: os._exit(0))
except SystemExit:
    raise
except Exception:
    (work_path / "driver-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    os._exit(1)
"""

_LISTENER = """\
import socket
import sys
import time
from pathlib import Path

host = sys.argv[1]
port = int(sys.argv[2])
Path(sys.argv[3]).write_text(sys.argv[4], encoding="utf-8")
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((host, port))
sock.listen(8)
time.sleep(60)
"""

_SLEEPER = "import time\ntime.sleep(60)\n"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _release_server(meta: dict, body: bytes) -> tuple[ThreadingHTTPServer, str]:
    payload = json.dumps(meta).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/api/latest"):
                self._send(200, payload, "application/json")
                return
            if self.path.startswith("/download/"):
                self._send(200, body, "application/octet-stream")
                return
            self.send_response(404)
            self.end_headers()

        def _send(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = int(server.server_address[1])
    if port in _BUSY_PORTS:
        server.server_close()
        raise RuntimeError("release server bound a live port")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def _close_server(server: ThreadingHTTPServer | None) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()


def _free_port(host: str = "127.0.0.1") -> int:
    for _ in range(8):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            port = int(sock.getsockname()[1])
        if port not in _BUSY_PORTS:
            return port
    raise RuntimeError(f"no free port on {host}")


def _can_bind(host: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
    except OSError:
        return False
    return True


def _hold(host: str) -> tuple[socket.socket, int]:
    """Listen and accept so the port stays open for the whole refusal wait.

    A connect that nobody accepts sits in the backlog. After a few of those,
    the next connect times out and the helpers treat that port as closed.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = int(sock.getsockname()[1])
    if port in _BUSY_PORTS:
        sock.close()
        raise RuntimeError("held a live port")
    sock.listen(8)
    sock.settimeout(0.5)

    def _accept() -> None:
        while True:
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                conn.close()
            except OSError:
                pass

    threading.Thread(target=_accept, name="yaver-e2e-hold", daemon=True).start()
    return sock, port


def _accepts(host: str, port: int) -> None:
    deadline = time.time() + 5
    last = "closed"
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last = str(exc)
            time.sleep(0.2)
    raise AssertionError(f"{host}:{port} did not accept a connection ({last})")


def _read_json(path: Path) -> dict:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return json.loads(raw.decode("utf-8"))


def _wait_json(path: Path, seconds: float) -> dict:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        if path.is_file():
            try:
                return _read_json(path)
            except json.JSONDecodeError as exc:
                last = str(exc)
        time.sleep(0.2)
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    raise AssertionError(f"no result at {path}: {last} {text}")


def _text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _stop_marked(token: str) -> None:
    if not token or any(char in token for char in " *?[]'\"\\"):
        raise AssertionError(f"unsafe process token {token!r}")
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*"
            + token
            + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=False,
            timeout=20,
        )
        return
    subprocess.run(["pkill", "-f", token], check=False, timeout=15)


def _portable(path: Path) -> str:
    text = str(path.resolve())
    if sys.platform == "win32":
        return text.replace("\\", "/")
    return text


def _meta(body: bytes, layout: str) -> dict:
    return {
        "version": "9.9.9",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "layout": layout,
        "notes": "e2e",
    }


def _run_driver(
    base: str,
    install: Path,
    work: Path,
    port: int,
    listener: Path,
    layout: str,
    probe: str,
) -> subprocess.CompletedProcess[str]:
    script = work / "driver.py"
    script.write_text(_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            str(script),
            base,
            str(install),
            str(work),
            str(port),
            str(listener),
            layout,
            probe,
            str(_ROOT),
        ],
        cwd=str(_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )


def _driver_details(proc: subprocess.CompletedProcess[str], work: Path) -> str:
    parts = [f"exit={proc.returncode}", proc.stdout or "", proc.stderr or ""]
    for name in ("driver-error.txt", "update.log", "last_result.json", "plan.json"):
        parts.append(f"--- {name} ---\n{_text(work / name)}")
    return "\n".join(parts)


def _write_listener(path: Path) -> None:
    path.write_text(_LISTENER, encoding="utf-8")


def _bash() -> str:
    if sys.platform == "win32":
        if not _GIT_BASH.is_file():
            pytest.skip("Git bash is not installed; the Linux helper was not executed")
        return str(_GIT_BASH)
    found = shutil.which("sh")
    if not found:
        pytest.skip("sh is not installed; the Linux helper was not executed")
    return found


def _shell_env(bash: str, home: Path) -> tuple[dict[str, str], bool]:
    """Return the helper environment and whether ``setsid`` is a stand-in."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if sys.platform != "win32" and shutil.which("setsid"):
        return env, False
    home.mkdir(parents=True, exist_ok=True)
    shim = home / "setsid"
    shim.write_bytes(b'#!/bin/sh\nexec "$@"\n')
    subprocess.run(
        [bash, "-c", 'chmod +x "$1"', "chmod", str(shim)],
        check=False,
        timeout=15,
    )
    env["PATH"] = str(home) + os.pathsep + env.get("PATH", "")
    seen = subprocess.run(
        [bash, "-c", "command -v setsid"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    assert seen.returncode == 0, seen.stdout + seen.stderr
    return env, True


def _shell_plan(path: Path, fields: dict[str, str]) -> None:
    lines = []
    for key, value in fields.items():
        if "'" in value or "\n" in value or "\r" in value:
            raise AssertionError(f"{key} cannot be stored in the shell plan")
        lines.append(f"{key}='{value}'")
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def _run_shell(
    bash: str,
    plan: Path,
    env: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash, str(_HELPER_SH), str(plan)],
        cwd=str(plan.parent),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def test_shell_helper_is_a_unix_script() -> None:
    raw = _HELPER_SH.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n")
    assert b"\r" not in raw


def test_shell_plan_records_the_dashboard_address(tmp_path: Path) -> None:
    plan = {
        "pid": 0,
        "port": 3210,
        "layout": "frozen",
        "staging": str(tmp_path / "stage"),
        "install_root": str(tmp_path / "Yaver"),
        "version": "9.9.9",
        "log": str(tmp_path / "update.log"),
        "result": str(tmp_path / "result.json"),
        "health_seconds": 12,
        "probe_host": "10.1.2.3",
        "argv": ["/opt/yaver/yaver", "start"],
    }
    dest = tmp_path / "plan.env"
    _write_shell_plan(dest, plan)
    text = dest.read_text(encoding="utf-8")
    assert "PROBE='10.1.2.3'" in text
    assert "ARGV0='/opt/yaver/yaver'" in text
    assert "ARGV1='start'" in text
    assert "PORT='3210'" in text
    plan["staging"] = "/tmp/it's"
    with pytest.raises(UpdateError, match="quoted"):
        _write_shell_plan(dest, plan)


def test_helper_does_not_launch_the_plan_command(tmp_path: Path) -> None:
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    port = _free_port("127.0.0.1")
    listener = tmp_path / "yaver_e2e_py_probe.py"
    _write_listener(listener)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": port,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    sys.executable,
                    str(listener),
                    "127.0.0.1",
                    str(port),
                    str(tmp_path / "listening.txt"),
                    "py-marker",
                ],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 2,
            }
        ),
        encoding="utf-8",
    )
    try:
        run_plan(plan)
        assert (install / "yaver.exe").read_bytes() == b"new-exe"
        assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
        assert not (tmp_path / "listening.txt").exists()
        result = _read_json(tmp_path / "result.json")
        assert result["ok"] is True
    finally:
        _stop_marked("yaver_e2e_py_probe.py")


def test_release_download_replaces_a_source_install_and_opens_the_port(tmp_path: Path) -> None:
    body = _zip(
        {
            "src/daemon.py": b"print('new-daemon')\n",
            "src/keep.py": b"new-keep\n",
            "VERSION": b"9.9.9\n",
            "README.txt": b"shipped\n",
        }
    )
    install = tmp_path / "app"
    (install / "src").mkdir(parents=True)
    (install / "src" / "daemon.py").write_text("old-daemon", encoding="utf-8")
    (install / "src" / "old.py").write_text("remove-me", encoding="utf-8")
    (install / "src" / "keep.py").write_text("old-keep", encoding="utf-8")
    (install / "VERSION").write_text("0.9.0\n", encoding="utf-8")
    (install / "notes.txt").write_text("leave me", encoding="utf-8")
    (install / ".venv").mkdir()
    (install / ".venv" / "pyvenv.cfg").write_text("home = here\n", encoding="utf-8")
    outside = tmp_path / "office-data"
    (outside / "yaver").mkdir(parents=True)
    (outside / "yaver" / "keep.txt").write_text("outside", encoding="utf-8")
    env_bytes = f"TOKEN=keep-me\nYAVER_BASE_DIR={outside}\n".encode()
    (install / ".env").write_bytes(env_bytes)
    (tmp_path / "canary.txt").write_text("canary", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    listener = tmp_path / "yaver_e2e_source_listen.py"
    _write_listener(listener)
    probe = "127.0.0.1"
    port = _free_port(probe)
    server, base = _release_server(_meta(body, "source"), body)
    try:
        proc = _run_driver(base, install, work, port, listener, "source", probe)
        assert proc.returncode == 0, _driver_details(proc, work)
        result = _wait_json(work / "last_result.json", 60)
        assert result == {"ok": True, "version": "9.9.9", "error": ""}, _text(work / "update.log")
        plan = _read_json(work / "plan.json")
        assert plan["install_root"] == str(install.resolve())
        assert plan["probe_host"] == probe
        assert plan["port"] == port
        assert plan["pid"] != os.getpid()
        assert (work / "yaver-9.9.9.zip").is_file()
        assert (install / "VERSION").read_text(encoding="utf-8").startswith("9.9.9")
        assert "new-daemon" in (install / "src" / "daemon.py").read_text(encoding="utf-8")
        assert (install / "src" / "keep.py").read_text(encoding="utf-8") == "new-keep\n"
        assert not (install / "src" / "old.py").exists()
        assert (install / "README.txt").read_bytes() == b"shipped\n"
        assert (install / "notes.txt").read_text(encoding="utf-8") == "leave me"
        assert (install / ".venv" / "pyvenv.cfg").is_file()
        assert (install / ".env").read_bytes() == env_bytes
        assert (outside / "yaver" / "keep.txt").read_text(encoding="utf-8") == "outside"
        assert (tmp_path / "canary.txt").read_text(encoding="utf-8") == "canary"
        assert not (work / "listening.txt").exists()
    finally:
        _close_server(server)
        _stop_marked("yaver_e2e_source_listen.py")


def test_release_download_replaces_a_frozen_install_and_keeps_inside_data(tmp_path: Path) -> None:
    body = _zip(
        {
            "yaver.exe": b"new-exe",
            "_internal/marker.txt": b"bundle",
        }
    )
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / "_internal" / "old.txt").write_text("old-bundle", encoding="utf-8")
    data = install / "office-data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    env_bytes = b"TOKEN=keep-me\nYAVER_BASE_DIR=office-data\n"
    (install / ".env").write_bytes(env_bytes)
    (tmp_path / "canary.txt").write_text("canary", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    listener = tmp_path / "yaver_e2e_frozen_listen.py"
    _write_listener(listener)
    probe = "127.0.0.1"
    port = _free_port(probe)
    server, base = _release_server(_meta(body, "frozen"), body)
    try:
        proc = _run_driver(base, install, work, port, listener, "frozen", probe)
        assert proc.returncode == 0, _driver_details(proc, work)
        result = _wait_json(work / "last_result.json", 60)
        assert result == {"ok": True, "version": "9.9.9", "error": ""}, _text(work / "update.log")
        plan = _read_json(work / "plan.json")
        assert plan["layout"] == "frozen"
        assert plan["probe_host"] == probe
        assert plan["port"] == port
        assert Path(plan["install_root"]).resolve() == install.resolve()
        assert (install / "yaver.exe").read_bytes() == b"new-exe"
        assert (install / "_internal" / "marker.txt").read_bytes() == b"bundle"
        assert not (install / "_internal" / "old.txt").exists()
        assert (install / ".env").read_bytes() == env_bytes
        assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
        assert not (tmp_path / "Yaver.userdata").exists()
        assert not (tmp_path / "Yaver.broken").exists()
        previous = tmp_path / "Yaver.previous"
        assert (previous / "yaver.exe").read_bytes() == b"old-exe"
        assert not (previous / "office-data").exists()
        assert (tmp_path / "canary.txt").read_text(encoding="utf-8") == "canary"
        assert not (work / "listening.txt").exists()
    finally:
        _close_server(server)
        _stop_marked("yaver_e2e_frozen_listen.py")


def test_helper_leaves_the_tree_alone_while_the_port_is_open(tmp_path: Path) -> None:
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    sock, port = _hold("127.0.0.1")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": port,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    started = time.monotonic()
    try:
        with pytest.raises(UpdateError, match="still open"):
            run_plan(plan)
    finally:
        sock.close()
    assert time.monotonic() - started >= 25
    assert (install / "yaver.exe").read_bytes() == b"old-exe"
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    assert not (tmp_path / "Yaver.previous").exists()
    result = _read_json(tmp_path / "result.json")
    assert result["ok"] is False
    assert "still open" in result["error"]


def test_powershell_health_uses_the_plan_address(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    if not _POWERSHELL.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    probe = "127.0.0.1"
    port = _free_port(probe)
    listener = tmp_path / "yaver_e2e_ps_probe.py"
    _write_listener(listener)
    marker = tmp_path / "listening.txt"
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": port,
                "probe_host": probe,
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, str(listener), probe, str(port), str(marker), "ps-marker"],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 8,
            }
        ),
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [
                str(_POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_HELPER_PS1),
                "-PlanPath",
                str(plan),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr + _text(tmp_path / "update.log")
        assert (install / "yaver.exe").read_bytes() == b"new-exe"
        assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
        assert (tmp_path / "Yaver.previous" / "yaver.exe").read_bytes() == b"old-exe"
        assert not marker.exists()
        shown = completed.stdout
        log = _text(tmp_path / "update.log")
        assert "env file present" in shown
        assert "env file left in place" in shown
        assert "frozen swap finished" in shown
        assert "env file present" not in log
        assert "updated 9.9.9" in log
        assert "TOKEN=keep-me" not in shown
        assert "TOKEN=keep-me" not in log
        assert "starting restored" not in log
        assert "started " not in shown
        assert "started " not in log
        result = _read_json(tmp_path / "result.json")
        assert result["ok"] is True
    finally:
        _stop_marked("yaver_e2e_ps_probe.py")


def test_powershell_does_not_treat_another_address_as_the_dashboard(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    if not _POWERSHELL.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    port = _free_port("127.0.0.1")
    listener = tmp_path / "yaver_e2e_ps_other.py"
    _write_listener(listener)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": port,
                "probe_host": "127.0.0.2",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    sys.executable,
                    str(listener),
                    "127.0.0.1",
                    str(port),
                    str(tmp_path / "listening.txt"),
                    "ps-other",
                ],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 3,
            }
        ),
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [
                str(_POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_HELPER_PS1),
                "-PlanPath",
                str(plan),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr + _text(tmp_path / "update.log")
        assert (install / "yaver.exe").read_bytes() == b"new-exe"
        assert not (tmp_path / "listening.txt").exists()
        assert "starting restored" not in _text(tmp_path / "update.log")
    finally:
        _stop_marked("yaver_e2e_ps_other.py")


def test_powershell_helper_leaves_the_tree_alone_while_the_port_is_open(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    if not _POWERSHELL.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    sock, port = _hold("127.0.0.1")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": port,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                str(_POWERSHELL),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(_HELPER_PS1),
                "-PlanPath",
                str(plan),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    finally:
        sock.close()
    assert time.monotonic() - started >= 25
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert (install / "yaver.exe").read_bytes() == b"old-exe"
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    assert not (tmp_path / "Yaver.previous").exists()
    result = _read_json(tmp_path / "result.json")
    assert result["ok"] is False
    assert "still open" in result["error"]


_LOCKER = """\
import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

target, release, ready = sys.argv[1:4]
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL
handle = kernel.CreateFileW(str(Path(target)), 0x80000000, 0x00000001, None, 3, 0, None)
invalid = int(ctypes.c_void_p(-1).value or 0)
if not handle or int(handle) in {0, invalid}:
    raise SystemExit(ctypes.get_last_error() or 2)
Path(ready).write_text("open", encoding="utf-8")
while not Path(release).exists():
    time.sleep(0.05)
kernel.CloseHandle(handle)
"""


def _windows_helper() -> None:
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    if not _POWERSHELL.is_file():
        pytest.skip("Windows PowerShell is not installed")


def _lock_path(target: Path, tmp_path: Path) -> tuple[subprocess.Popen[str], Path]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        target.write_bytes(b"locked")
    script = tmp_path / "yaver_e2e_lock_file.py"
    script.write_text(_LOCKER, encoding="utf-8")
    release = tmp_path / "release-lock"
    ready = tmp_path / "lock-ready"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(target), str(release), str(ready)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 10
    while time.time() < deadline and not ready.is_file():
        if proc.poll() is not None:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise AssertionError(f"locker exited {proc.returncode}: {err}")
        time.sleep(0.05)
    if not ready.is_file():
        proc.kill()
        raise AssertionError("locker did not open the file")
    return proc, release


def _lock_internal(install: Path, tmp_path: Path) -> tuple[subprocess.Popen[str], Path]:
    return _lock_path(install / "_internal" / "locked.bin", tmp_path)


def _release_locker(proc: subprocess.Popen[str], release: Path) -> None:
    release.write_text("go", encoding="utf-8")
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _ps_argv(plan: Path) -> list[str]:
    return [
        str(_POWERSHELL),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(_HELPER_PS1),
        "-PlanPath",
        str(plan),
    ]


def _locked_plan(tmp_path: Path, install: Path, staging: Path, pid: int) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": pid,
                "port": 0,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "cwd": str(tmp_path),
                "version": "9.9.9",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    return plan


def _locked_install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env").write_bytes(b"TOKEN=keep-me\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_bytes(b"new-exe")
    (staging / "_internal" / "marker.txt").write_bytes(b"bundle")
    return install, staging


def test_powershell_refuses_to_move_internal_while_pid_zero_holds_it(tmp_path: Path) -> None:
    """The reported failure: pid 0 moves _internal while a process has it open."""
    _windows_helper()
    install, staging = _locked_install(tmp_path)
    locker, release = _lock_internal(install, tmp_path)
    plan = _locked_plan(tmp_path, install, staging, 0)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _ps_argv(plan),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
    finally:
        _release_locker(locker, release)
    elapsed = time.monotonic() - started
    assert elapsed >= 8, completed.stdout + completed.stderr
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert (install / "yaver.exe").read_bytes() == b"old-exe"
    assert (install / "_internal" / "locked.bin").is_file()
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    result = _read_json(tmp_path / "result.json")
    assert result["ok"] is False


def test_powershell_moves_internal_after_the_locking_process_exits(tmp_path: Path) -> None:
    """Hold _internal longer than the 10 second retry, then let the pid exit."""
    _windows_helper()
    install, staging = _locked_install(tmp_path)
    locker, release = _lock_internal(install, tmp_path)
    plan = _locked_plan(tmp_path, install, staging, locker.pid)
    out = tmp_path / "helper-out.txt"
    handle = out.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        _ps_argv(plan),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(12)
        assert proc.poll() is None, _text(out)
        assert (install / "yaver.exe").read_bytes() == b"old-exe"
        assert (install / "_internal" / "locked.bin").is_file()
        _release_locker(locker, release)
        code = proc.wait(timeout=20)
    finally:
        if not release.exists():
            release.write_text("go", encoding="utf-8")
        if locker.poll() is None:
            locker.kill()
            locker.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        handle.close()
    shown = _text(out)
    assert code == 0, shown
    assert f"plan_pid={locker.pid}" in shown
    assert (install / "yaver.exe").read_bytes() == b"new-exe"
    assert (install / "_internal" / "marker.txt").read_bytes() == b"bundle"
    assert not (install / "_internal" / "locked.bin").exists()
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    assert "started " not in shown


def _py_argv(plan: Path) -> list[str]:
    return [sys.executable, str(_HELPER_PY), "--apply", str(plan)]


def test_python_helper_refuses_to_move_internal_while_pid_zero_holds_it(tmp_path: Path) -> None:
    """A frozen build with Python on PATH uses this helper. Pid 0 still denies the move."""
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    install, staging = _locked_install(tmp_path)
    locker, release = _lock_internal(install, tmp_path)
    plan = _locked_plan(tmp_path, install, staging, 0)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            _py_argv(plan),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            check=False,
        )
    finally:
        _release_locker(locker, release)
    elapsed = time.monotonic() - started
    assert elapsed >= 8, completed.stdout + completed.stderr
    assert completed.returncode != 0, completed.stdout + completed.stderr
    assert (install / "yaver.exe").read_bytes() == b"old-exe"
    assert (install / "_internal" / "locked.bin").is_file()
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    result = _read_json(tmp_path / "result.json")
    assert result["ok"] is False


def test_python_helper_moves_internal_after_the_locking_process_exits(tmp_path: Path) -> None:
    """The Python helper waits out a lock that lasts longer than its move retries."""
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    install, staging = _locked_install(tmp_path)
    locker, release = _lock_internal(install, tmp_path)
    plan = _locked_plan(tmp_path, install, staging, locker.pid)
    out = tmp_path / "helper-out.txt"
    handle = out.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        _py_argv(plan),
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    try:
        time.sleep(12)
        assert proc.poll() is None, _text(out)
        assert (install / "yaver.exe").read_bytes() == b"old-exe"
        assert (install / "_internal" / "locked.bin").is_file()
        _release_locker(locker, release)
        code = proc.wait(timeout=20)
    finally:
        if not release.exists():
            release.write_text("go", encoding="utf-8")
        if locker.poll() is None:
            locker.kill()
            locker.wait(timeout=5)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        handle.close()
    shown = _text(out)
    assert code == 0, shown
    assert f"plan_pid={locker.pid}" in shown
    assert (install / "yaver.exe").read_bytes() == b"new-exe"
    assert (install / "_internal" / "marker.txt").read_bytes() == b"bundle"
    assert not (install / "_internal" / "locked.bin").exists()
    assert (install / ".env").read_bytes() == b"TOKEN=keep-me\n"
    assert "started " not in shown


def _frozen_dirs(tmp_path: Path, old: bytes, new: bytes) -> tuple[Path, Path, bytes]:
    # Linux publishes ``yaver``, not ``yaver.exe``. Git bash treats those two
    # names as one file, so the fixture keeps only the Linux name.
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver").write_bytes(old)
    (install / "_internal" / "old.txt").write_text("old-bundle", encoding="utf-8")
    data = install / "office-data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    env_bytes = b"TOKEN=keep-me\nYAVER_BASE_DIR=office-data\n"
    (install / ".env").write_bytes(env_bytes)
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver").write_bytes(new)
    (staging / "_internal" / "marker.txt").write_bytes(b"bundle")
    return install, staging, env_bytes


def test_shell_helper_replaces_a_frozen_install_and_opens_the_port(tmp_path: Path) -> None:
    bash = _bash()
    install, staging, env_bytes = _frozen_dirs(tmp_path, b"old-exe", b"new-exe")
    probe = "127.0.0.1"
    port = _free_port(probe)
    listener = tmp_path / "yaver_e2e_sh_listen.py"
    marker = tmp_path / "listening.txt"
    env, _shimmed = _shell_env(bash, tmp_path / "bin")
    plan = tmp_path / "plan.env"
    _shell_plan(
        plan,
        {
            "PID": "0",
            "PORT": str(port),
            "LAYOUT": "frozen",
            "STAGING": _portable(staging),
            "INSTALL": _portable(install),
            "VERSION": "9.9.9",
            "LOG": _portable(tmp_path / "update.log"),
            "RESULT": _portable(tmp_path / "result.json"),
            "HEALTH_SECONDS": "12",
            "PROBE": probe,
            "ARGV0": _portable(Path(sys.executable)),
            "ARGV1": _portable(listener),
        },
    )
    # The listener reads its own source for the bind address. ARGV only
    # has two slots in the shell plan, so the port and marker are baked in.
    listener.write_text(
        "import socket\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('sh-marker', encoding='utf-8')\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"sock.bind(({probe!r}, {port}))\n"
        "sock.listen(8)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    (staging / ".env").write_bytes(b"TOKEN=from-package\n")
    env_path = install / ".env"
    before = env_path.stat()
    env_path.chmod(stat.S_IREAD)
    try:
        completed = _run_shell(bash, plan, env, timeout=40)
        log = _text(tmp_path / "update.log")
        assert completed.returncode == 0, completed.stdout + completed.stderr + log
        result = _read_json(tmp_path / "result.json")
        assert result == {"ok": True, "version": "9.9.9", "error": ""}, log
        assert (install / "yaver").read_bytes() == b"new-exe"
        assert (install / "_internal" / "marker.txt").read_bytes() == b"bundle"
        assert not (install / "_internal" / "old.txt").exists()
        assert (install / ".env").read_bytes() == env_bytes
        assert env_path.stat().st_ino == before.st_ino
        assert env_path.stat().st_mode & stat.S_IWRITE == 0
        assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
        assert not (tmp_path / "Yaver.userdata").exists()
        previous = tmp_path / "Yaver.previous"
        assert (previous / "yaver").read_bytes() == b"old-exe"
        assert not (previous / "office-data").exists()
        assert not marker.exists()
        shown = completed.stdout
        assert "env file present" in shown
        assert "env file left in place" in shown
        assert "package env file skipped" in shown
        assert "frozen swap finished" in shown
        assert "parked data folder office-data" in shown
        assert "env file present" not in log
        assert "updated 9.9.9" in log
        assert "TOKEN=keep-me" not in shown
        assert "TOKEN=keep-me" not in log
        assert "from-package" not in shown
        assert "from-package" not in log
        assert "starting restored" not in log
        assert "started " not in shown
        assert "started " not in log
    finally:
        env_path.chmod(stat.S_IWRITE)
        _stop_marked("yaver_e2e_sh_listen.py")


def test_shell_helper_restores_the_previous_install_when_the_port_stays_closed(tmp_path: Path) -> None:
    bash = _bash()
    install, staging, env_bytes = _frozen_dirs(tmp_path, b"old-exe", b"new-exe")
    port = _free_port("127.0.0.1")
    sleeper = tmp_path / "yaver_e2e_sh_sleep.py"
    sleeper.write_text(_SLEEPER, encoding="utf-8")
    env, _shimmed = _shell_env(bash, tmp_path / "bin")
    plan = tmp_path / "plan.env"
    _shell_plan(
        plan,
        {
            "PID": "0",
            "PORT": str(port),
            "LAYOUT": "frozen",
            "STAGING": _portable(staging),
            "INSTALL": _portable(install),
            "VERSION": "9.9.9",
            "LOG": _portable(tmp_path / "update.log"),
            "RESULT": _portable(tmp_path / "result.json"),
            "HEALTH_SECONDS": "4",
            "PROBE": "127.0.0.1",
            "ARGV0": _portable(Path(sys.executable)),
            "ARGV1": _portable(sleeper),
        },
    )
    try:
        completed = _run_shell(bash, plan, env, timeout=30)
        log = _text(tmp_path / "update.log")
        assert completed.returncode == 0, completed.stdout + completed.stderr + log
        result = _read_json(tmp_path / "result.json")
        assert result["ok"] is True
        assert (install / "yaver").read_bytes() == b"new-exe"
        assert (install / "_internal" / "marker.txt").read_bytes() == b"bundle"
        assert (install / ".env").read_bytes() == env_bytes
        assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
        assert not (tmp_path / "Yaver.userdata").exists()
        assert (tmp_path / "Yaver.previous" / "yaver").read_bytes() == b"old-exe"
        assert "updated 9.9.9" in log
        assert "starting restored" not in log
    finally:
        _stop_marked("yaver_e2e_sh_sleep.py")


def test_shell_helper_leaves_the_tree_alone_while_the_port_is_open(tmp_path: Path) -> None:
    bash = _bash()
    install, staging, env_bytes = _frozen_dirs(tmp_path, b"old-exe", b"new-exe")
    sock, port = _hold("127.0.0.1")
    env, _shimmed = _shell_env(bash, tmp_path / "bin")
    plan = tmp_path / "plan.env"
    _shell_plan(
        plan,
        {
            "PID": "0",
            "PORT": str(port),
            "LAYOUT": "frozen",
            "STAGING": _portable(staging),
            "INSTALL": _portable(install),
            "VERSION": "9.9.9",
            "LOG": _portable(tmp_path / "update.log"),
            "RESULT": _portable(tmp_path / "result.json"),
            "HEALTH_SECONDS": "4",
            "PROBE": "127.0.0.1",
            "ARGV0": _portable(Path(sys.executable)),
            "ARGV1": _portable(staging / "yaver"),
        },
    )
    started = time.monotonic()
    moved: list[str] = []
    stop = threading.Event()
    userdata = tmp_path / "Yaver.userdata"
    keep = install / "office-data" / "yaver" / "keep.txt"

    def watch() -> None:
        while not stop.is_set():
            if userdata.exists():
                moved.append("userdata")
            if not keep.is_file():
                moved.append("missing-keep")
            stop.wait(0.05)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        completed = _run_shell(bash, plan, env, timeout=45)
    finally:
        stop.set()
        watcher.join(timeout=2)
        sock.close()
    log = _text(tmp_path / "update.log")
    assert moved == []
    assert time.monotonic() - started >= 25
    assert completed.returncode != 0, completed.stdout + completed.stderr + log
    assert (install / "yaver").read_bytes() == b"old-exe"
    assert (install / ".env").read_bytes() == env_bytes
    assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert not (tmp_path / "Yaver.userdata").exists()
    assert not (tmp_path / "Yaver.previous").exists()
    result = _read_json(tmp_path / "result.json")
    assert result["ok"] is False
    assert "still open" in result["error"]


def test_shell_helper_puts_data_back_when_previous_cannot_be_removed(tmp_path: Path) -> None:
    """rm of the old tree must not leave the parked data folder beside Yaver."""
    if sys.platform != "win32":
        pytest.skip("an open file stops rm on Windows")
    bash = _bash()
    install, staging, env_bytes = _frozen_dirs(tmp_path, b"old-exe", b"new-exe")
    previous = tmp_path / "Yaver.previous"
    locker, release = _lock_path(previous / "locked.bin", tmp_path)
    env, _shimmed = _shell_env(bash, tmp_path / "bin")
    plan = tmp_path / "plan.env"
    _shell_plan(
        plan,
        {
            "PID": "0",
            "PORT": "0",
            "LAYOUT": "frozen",
            "STAGING": _portable(staging),
            "INSTALL": _portable(install),
            "VERSION": "9.9.9",
            "LOG": _portable(tmp_path / "update.log"),
            "RESULT": _portable(tmp_path / "result.json"),
            "HEALTH_SECONDS": "4",
            "PROBE": "127.0.0.1",
            "ARGV0": _portable(Path(sys.executable)),
            "ARGV1": "",
        },
    )
    try:
        completed = _run_shell(bash, plan, env, timeout=30)
    finally:
        _release_locker(locker, release)
    log = _text(tmp_path / "update.log")
    assert completed.returncode != 0, completed.stdout + completed.stderr + log
    assert (install / "yaver").read_bytes() == b"old-exe"
    assert (install / ".env").read_bytes() == env_bytes
    assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert not (tmp_path / "Yaver.userdata").exists()
