"""Start scripts must send the same OpenCode basic auth as the daemon.

A password-protected ``opencode serve`` answers ``/global/health`` with HTTP
401 and an empty body. The Python client sends
``Authorization: Basic`` for ``OPENCODE_SERVER_PASSWORD``. The Windows and
Linux launchers used to wait with no header, so start never reached the
daemon.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import _dotenv_quote, settings
from src.opencode_serve_supervisor import (
    default_listener_pids,
    probe_healthy,
    resolve_opencode_binary,
    serve_auth_headers,
    serve_command,
    serve_env,
)
from src.process_kill import kill_pid

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "packaging" / "windows"
LINUX = ROOT / "packaging" / "linux"
SECRET = "launcher-secret-7f3a"
OTHER_SECRET = "dotenv-other-secret"
EXPAND = "expand-secret-7f3a"


class _HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path != "/global/health":
            self._reply(404, b"")
            return
        got = self.headers.get("Authorization")
        self.server.seen.append(got)  # type: ignore[attr-defined]
        mode = self.server.mode  # type: ignore[attr-defined]
        expect = self.server.expect  # type: ignore[attr-defined]
        if mode == "open":
            self._reply(200, b'{"healthy":true}')
            return
        if mode == "absent":
            if got:
                self._reply(401, b"")
            else:
                self._reply(200, b'{"healthy":true}')
            return
        if got == expect:
            self._reply(200, b'{"healthy":true}')
            return
        self._reply(401, b"")

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def log_message(self, fmt: str, *args: object) -> None:
        return


@contextmanager
def _health_server(mode: str, expect: str | None = None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    httpd.mode = mode  # type: ignore[attr-defined]
    httpd.expect = expect  # type: ignore[attr-defined]
    httpd.seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _port_is_listening(port: int) -> bool:
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    needle = f":{port}"
    for line in (out.stdout or "").splitlines():
        if needle in line and "LISTEN" in line.upper():
            return True
    return False


def _powershell() -> str | None:
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Write-Output ok"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0 and "ok" in (proc.stdout or ""):
        return "powershell"
    return None


def _bash() -> str | None:
    candidates = ["bash", r"C:\Program Files\Git\bin\bash.exe"]
    for candidate in candidates:
        try:
            proc = subprocess.run(
                [candidate, "-c", "echo ok"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and "ok" in (proc.stdout or ""):
            return candidate
    return None


def _env(*drops: str, **updates: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in drops:
        env.pop(key, None)
    env.update(updates)
    return env


def _run(
    args: list[str],
    env: dict[str, str],
    timeout: float,
) -> tuple[int, str, float]:
    started = time.monotonic()
    proc = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=str(ROOT),
    )
    elapsed = time.monotonic() - started
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), elapsed


def _auth_header(password: str, username: str | None = None) -> str:
    """Same bytes as ``serve_auth_headers`` for this password."""
    previous = {
        "OPENCODE_SERVER_PASSWORD": os.environ.get("OPENCODE_SERVER_PASSWORD"),
        "OPENCODE_SERVER_USERNAME": os.environ.get("OPENCODE_SERVER_USERNAME"),
    }
    os.environ["OPENCODE_SERVER_PASSWORD"] = password
    if username is None:
        os.environ.pop("OPENCODE_SERVER_USERNAME", None)
    else:
        os.environ["OPENCODE_SERVER_USERNAME"] = username
    try:
        headers = serve_auth_headers()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if not headers:
        return ""
    return headers["Authorization"]


def _ps_file(*args: str, env: dict[str, str], timeout: float = 20) -> tuple[int, str, float]:
    ps = _powershell()
    if ps is None:
        pytest.skip("powershell is not available")
    return _run([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args], env, timeout)


def _bash_script(script: str, env: dict[str, str], timeout: float = 20) -> tuple[int, str, float]:
    bash = _bash()
    if bash is None:
        pytest.skip("bash is not available")
    child = _env()
    child.update(env)
    child["VD_LIB"] = str(LINUX / "lib.sh")
    child["MSYS_NO_PATHCONV"] = "1"
    child["MSYS2_ARG_CONV_EXCL"] = "*"
    wrapped = (
        "export PATH=\"/usr/bin:$PATH\"\n"
        "if command -v cygpath >/dev/null 2>&1; then\n"
        "  source \"$(cygpath -u \"$VD_LIB\")\"\n"
        "else\n"
        "  source \"$VD_LIB\"\n"
        "fi\n"
        "if [[ -n \"${VD_ENV_FILE:-}\" ]] && command -v cygpath >/dev/null 2>&1; then\n"
        "  VD_ENV_FILE=\"$(cygpath -u \"$VD_ENV_FILE\")\"\n"
        "fi\n"
        + script
    )
    return _run([bash, "-c", wrapped], child, timeout)


def test_launcher_sources_send_serve_auth_and_keep_dashboard_waits_open():
    ensure = (WIN / "Ensure-OpencodeServe.ps1").read_text(encoding="utf-8")
    wait = (WIN / "Wait-Http.ps1").read_text(encoding="utf-8")
    auth = (WIN / "ServeAuth.ps1").read_text(encoding="utf-8")
    linux_ensure = (LINUX / "ensure-opencode-serve.sh").read_text(encoding="utf-8")
    lib = (LINUX / "lib.sh").read_text(encoding="utf-8")
    backend = (WIN / "start-backend.bat").read_text(encoding="utf-8")
    frontend = (WIN / "start-frontend.bat").read_text(encoding="utf-8")
    linux_backend = (LINUX / "start-backend.sh").read_text(encoding="utf-8")
    linux_front = (LINUX / "start-frontend.sh").read_text(encoding="utf-8")

    assert "Import-OpencodeServeAuth" in ensure
    assert "FailOnUnauthorized" in ensure
    assert "ServeAuth.ps1" in ensure
    assert ensure.index("Import-OpencodeServeAuth") < ensure.index("Start-Process")
    assert "UseNewEnvironment" not in ensure
    assert "OPENCODE_SERVER_PASSWORD=" not in ensure
    assert "FailOnUnauthorized" in wait
    assert "vd_import_opencode_serve_auth" in linux_ensure
    assert "vd_wait_serve_health" in linux_ensure
    assert linux_ensure.index("vd_import_opencode_serve_auth") < linux_ensure.index("nohup")
    assert "vd_wait_serve_health" in lib
    assert "vd_wait_http" in linux_backend
    assert "vd_wait_serve_health" not in linux_backend
    assert "vd_wait_serve_health" not in linux_front
    assert "FailOnUnauthorized" not in backend
    assert "FailOnUnauthorized" not in frontend
    for text in (ensure, wait, auth):
        assert all(ord(ch) < 128 for ch in text)
    for bat in (ROOT / "start-opencode-serve.bat", WIN / "start-opencode-serve.bat"):
        body = bat.read_text(encoding="utf-8")
        assert "Ensure-OpencodeServe.ps1" in body
        assert "Wait-Http.ps1" not in body
        assert "OPENCODE_SERVER_PASSWORD=" not in body


def test_wait_http_sends_the_daemon_basic_header():
    header = _auth_header(SECRET)
    assert header.startswith("Basic ")
    with _health_server("auth", header) as httpd:
        code, out, _elapsed = _ps_file(
            str(WIN / "Wait-Http.ps1"),
            "-Url",
            f"http://127.0.0.1:{httpd.server_address[1]}/global/health",
            "-TimeoutSec",
            "10",
            "-OkPattern",
            "healthy",
            "-Authorization",
            header,
            "-FailOnUnauthorized",
            env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
        )
    assert code == 0, out
    assert httpd.seen  # type: ignore[attr-defined]
    assert httpd.seen[0] == header  # type: ignore[attr-defined]
    assert SECRET not in out
    assert header not in out


def test_wait_http_401_is_not_a_timeout():
    header = _auth_header(SECRET)
    with _health_server("auth", header) as httpd:
        code, out, elapsed = _ps_file(
            str(WIN / "Wait-Http.ps1"),
            "-Url",
            f"http://127.0.0.1:{httpd.server_address[1]}/global/health",
            "-TimeoutSec",
            "30",
            "-OkPattern",
            "healthy",
            "-Authorization",
            "Basic d3Jvbmc6d3Jvbmc=",
            "-FailOnUnauthorized",
            env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
            timeout=15,
        )
    assert code == 3, out
    assert elapsed < 4, out
    assert "HTTP 401" in out
    assert SECRET not in out
    assert "TIMEOUT" not in out


def test_wait_http_without_serve_auth_still_accepts_an_open_url():
    with _health_server("absent") as httpd:
        code, out, _elapsed = _ps_file(
            str(WIN / "Wait-Http.ps1"),
            "-Url",
            f"http://127.0.0.1:{httpd.server_address[1]}/global/health",
            "-TimeoutSec",
            "10",
            "-OkPattern",
            "healthy",
            env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
        )
    assert code == 0, out
    assert httpd.seen  # type: ignore[attr-defined]
    assert httpd.seen[0] in (None, "")  # type: ignore[attr-defined]


def test_wait_http_once_does_not_retry_a_closed_port():
    code, out, elapsed = _ps_file(
        str(WIN / "Wait-Http.ps1"),
        "-Url",
        "http://127.0.0.1:1/global/health",
        "-TimeoutSec",
        "30",
        "-Once",
        env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
        timeout=15,
    )
    assert code == 1, out
    assert "NOT READY" in out
    assert elapsed < 4, out


def test_ensure_loads_dotenv_password_and_sends_it(tmp_path: Path):
    header = _auth_header(SECRET, "yaver-bot")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENCODE_SERVER_USERNAME=yaver-bot\n"
        f"OPENCODE_SERVER_PASSWORD={SECRET}\n",
        encoding="utf-8",
    )
    with _health_server("auth", header) as httpd:
        port = httpd.server_address[1]
        if not _port_is_listening(port):
            pytest.skip("health port is not visible to the serve launcher")
        code, out, _elapsed = _ps_file(
            str(WIN / "Ensure-OpencodeServe.ps1"),
            "-ProjectDir",
            str(tmp_path),
            "-ServeHost",
            "127.0.0.1",
            "-ServePort",
            str(port),
            "-TimeoutSec",
            "8",
            env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
            timeout=20,
        )
    assert code == 0, out
    assert httpd.seen[0] == header  # type: ignore[attr-defined]
    assert SECRET not in out
    assert header not in out


def test_ensure_prefers_process_password_over_dotenv(tmp_path: Path):
    header = _auth_header(SECRET)
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"OPENCODE_SERVER_PASSWORD={OTHER_SECRET}\n",
        encoding="utf-8",
    )
    with _health_server("auth", header) as httpd:
        port = httpd.server_address[1]
        if not _port_is_listening(port):
            pytest.skip("health port is not visible to the serve launcher")
        code, out, _elapsed = _ps_file(
            str(WIN / "Ensure-OpencodeServe.ps1"),
            "-ProjectDir",
            str(tmp_path),
            "-ServePort",
            str(port),
            "-TimeoutSec",
            "8",
            env=_env(
                "OPENCODE_SERVER_USERNAME",
                OPENCODE_SERVER_PASSWORD=SECRET,
            ),
            timeout=20,
        )
    assert code == 0, out
    assert httpd.seen[0] == header  # type: ignore[attr-defined]
    assert SECRET not in out
    assert OTHER_SECRET not in out


def test_ensure_wrong_password_fails_fast(tmp_path: Path):
    header = _auth_header(SECRET)
    with _health_server("auth", header) as httpd:
        port = httpd.server_address[1]
        if not _port_is_listening(port):
            pytest.skip("health port is not visible to the serve launcher")
        code, out, elapsed = _ps_file(
            str(WIN / "Ensure-OpencodeServe.ps1"),
            "-ProjectDir",
            str(tmp_path),
            "-ServePort",
            str(port),
            "-TimeoutSec",
            "30",
            env=_env(
                "OPENCODE_SERVER_USERNAME",
                OPENCODE_SERVER_PASSWORD="wrong-secret",
            ),
            timeout=15,
        )
    assert code == 1, out
    assert elapsed < 4, out
    assert "401" in out
    assert "wrong-secret" not in out
    assert SECRET not in out


def test_ensure_open_serve_sends_no_authorization(tmp_path: Path):
    with _health_server("absent") as httpd:
        port = httpd.server_address[1]
        if not _port_is_listening(port):
            pytest.skip("health port is not visible to the serve launcher")
        code, out, _elapsed = _ps_file(
            str(WIN / "Ensure-OpencodeServe.ps1"),
            "-ProjectDir",
            str(tmp_path),
            "-ServePort",
            str(port),
            "-TimeoutSec",
            "8",
            env=_env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
            timeout=20,
        )
    assert code == 0, out
    assert httpd.seen[0] in (None, "")  # type: ignore[attr-defined]


def test_imported_password_reaches_a_child_process(tmp_path: Path):
    ps = _powershell()
    if ps is None:
        pytest.skip("powershell is not available")
    (tmp_path / ".env").write_text(
        f"OPENCODE_SERVER_PASSWORD={SECRET}\nOPENCODE_SERVER_USERNAME=yaver-bot\n",
        encoding="utf-8",
    )
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{(WIN / 'ServeAuth.ps1').as_posix()}'\n"
        "Remove-Item Env:\\OPENCODE_SERVER_PASSWORD -ErrorAction SilentlyContinue\n"
        "Remove-Item Env:\\OPENCODE_SERVER_USERNAME -ErrorAction SilentlyContinue\n"
        f"Import-OpencodeServeAuth -ProjectDir '{tmp_path}'\n"
        "$code = '[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:OPENCODE_SERVER_PASSWORD))'\n"
        "$child = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))\n"
        "$got = & powershell -NoProfile -EncodedCommand $child\n"
        "Write-Output $got\n"
        "Write-Output (Get-OpencodeServeAuthorization)\n",
        encoding="utf-8",
    )
    code, out, _elapsed = _run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        _env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
        20,
    )
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    assert code == 0, out
    assert lines[0] == base64.b64encode(SECRET.encode("utf-8")).decode("ascii")
    assert lines[1] == _auth_header(SECRET, "yaver-bot")
    assert SECRET not in out


def test_windows_dotenv_reader_matches_python_dotenv(tmp_path: Path):
    pytest.importorskip("dotenv")
    from dotenv import dotenv_values

    ps = _powershell()
    if ps is None:
        pytest.skip("powershell is not available")
    quoted = _dotenv_quote('a b"\\#')
    samples = [
        "OPENCODE_SERVER_PASSWORD=review-secret\n",
        'OPENCODE_SERVER_PASSWORD="review secret"\n',
        "OPENCODE_SERVER_PASSWORD='review secret'\n",
        "export OPENCODE_SERVER_PASSWORD=review-secret\n",
        "OPENCODE_SERVER_PASSWORD=a=b=c\n",
        "OPENCODE_SERVER_PASSWORD=secret\r\n",
        "OPENCODE_SERVER_PASSWORD=\n",
        "# c\nOPENCODE_SERVER_PASSWORD=second\nOPENCODE_SERVER_PASSWORD=last\n",
        'OPENCODE_SERVER_PASSWORD="a\\"b"\n',
        'OPENCODE_SERVER_PASSWORD="a\\\\b"\n',
        'OPENCODE_SERVER_PASSWORD="a#b"\n',
        "OPENCODE_SERVER_PASSWORD=value # comment\n",
        "OPENCODE_SERVER_PASSWORD = spaced\n",
        "OPENCODE_SERVER_PASSWORD=ğüş\n",
        "\ufeffOPENCODE_SERVER_PASSWORD=bom\n",
        f"OPENCODE_SERVER_PASSWORD={quoted}\n",
        "OPENCODE_SERVER_USERNAME=only\n",
    ]
    probe = (
        "$ErrorActionPreference = 'Stop'\n"
        f". '{(WIN / 'ServeAuth.ps1').as_posix()}'\n"
        "$got = Read-DotEnvKey -Path $args[0] -Key 'OPENCODE_SERVER_PASSWORD'\n"
        "if (-not $got.Found) { Write-Output 'MISSING' } else {\n"
        "  Write-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$got.Value)))\n"
        "}\n"
    )
    script = tmp_path / "read.ps1"
    script.write_text(probe, encoding="utf-8")
    for index, text in enumerate(samples):
        path = tmp_path / f"case{index}.env"
        path.write_bytes(text.encode("utf-8"))
        expected = dotenv_values(path).get("OPENCODE_SERVER_PASSWORD")
        code, out, _elapsed = _run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(path)],
            _env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"),
            20,
        )
        assert code == 0, out
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        got = lines[-1] if lines else ""
        if expected is None:
            assert got == "MISSING", text
        else:
            want = base64.b64encode(expected.encode("utf-8")).decode("ascii")
            assert got == want, text


def test_linux_wait_sends_the_daemon_basic_header():
    header = _auth_header(SECRET, "yaver-bot")
    with _health_server("auth", header) as httpd:
        port = httpd.server_address[1]
        code, out, _elapsed = _bash_script(
            'export OPENCODE_SERVER_PASSWORD="$VD_SECRET"\n'
            'export OPENCODE_SERVER_USERNAME="$VD_USER"\n'
            'vd_wait_serve_health "$VD_URL" 8\n'
            "printf '%s' $?\n",
            {
                "VD_SECRET": SECRET,
                "VD_USER": "yaver-bot",
                "VD_URL": f"http://127.0.0.1:{port}/global/health",
                "OPENCODE_SERVER_PASSWORD": SECRET,
                "OPENCODE_SERVER_USERNAME": "yaver-bot",
            },
        )
    assert code == 0, out
    assert out.strip().endswith("0"), out
    assert httpd.seen[0] == header  # type: ignore[attr-defined]
    assert SECRET not in out


def test_linux_401_is_not_a_timeout():
    header = _auth_header(SECRET)
    with _health_server("auth", header) as httpd:
        port = httpd.server_address[1]
        code, out, elapsed = _bash_script(
            'export OPENCODE_SERVER_PASSWORD=wrong-secret\n'
            'vd_wait_serve_health "$VD_URL" 30\n'
            "printf '%s' $?\n",
            {
                "VD_URL": f"http://127.0.0.1:{port}/global/health",
                "OPENCODE_SERVER_PASSWORD": "wrong-secret",
            },
            timeout=15,
        )
    assert code == 0, out
    # The status is printed on stdout and the 401 line on stderr. Git Bash
    # can deliver stderr first, so either end of the combined text may hold it.
    assert out.lstrip().startswith("3") or out.rstrip().endswith("3"), out
    assert elapsed < 4, out
    assert "HTTP 401" in out
    assert "wrong-secret" not in out
    assert SECRET not in out


def test_linux_dotenv_reader_matches_python_dotenv(tmp_path: Path):
    pytest.importorskip("dotenv")
    from dotenv import dotenv_values

    text = 'export OPENCODE_SERVER_PASSWORD="a\\"b"\nOPENCODE_SERVER_PASSWORD=last\n'
    path = tmp_path / ".env"
    path.write_bytes(text.encode("utf-8"))
    expected = dotenv_values(path).get("OPENCODE_SERVER_PASSWORD")
    code, out, _elapsed = _bash_script(
        'if val="$(vd_dotenv_key "$VD_ENV_FILE" OPENCODE_SERVER_PASSWORD)"; then\n'
        "  printf '%s' \"$val\" | base64 | tr -d '\\n\\r'\n"
        "else\n"
        "  printf MISSING\n"
        "fi\n",
        {"VD_ENV_FILE": str(path)},
    )
    assert code == 0, out
    want = base64.b64encode((expected or "").encode("utf-8")).decode("ascii")
    assert out.strip().splitlines()[-1].strip() == want


def test_linux_import_prefers_an_empty_process_password(tmp_path: Path):
    (tmp_path / ".env").write_text(
        f"OPENCODE_SERVER_PASSWORD={SECRET}\n",
        encoding="utf-8",
    )
    code, out, _elapsed = _bash_script(
        'export OPENCODE_SERVER_PASSWORD=\n'
        'vd_import_opencode_serve_auth "$VD_ENV_FILE"\n'
        'if [[ -z "${OPENCODE_SERVER_PASSWORD}" ]]; then printf EMPTY; else printf NONEMPTY; fi\n',
        {
            "VD_ENV_FILE": str(tmp_path / ".env"),
            "OPENCODE_SERVER_PASSWORD": "",
        },
    )
    assert code == 0, out
    assert out.strip().endswith("EMPTY"), out
    assert SECRET not in out


def _dotenv_cases() -> list[tuple[str, str, str | None]]:
    """Forms python-dotenv expands. The launchers must return the same bytes.

    ``required`` is the daemon value where both dotenv 1.2.3 and 1.2.4 agree.
    Inline ``KEY= # comment`` is left to ``dotenv_values`` because 1.2.4 treats
    it as empty and 1.2.3 keeps the comment text.
    """
    return [
        ("inline-comment", "OPENCODE_SERVER_PASSWORD= # comment\n", None),
        (
            "file-interpolation",
            f"VD_DOTENV_FILE={EXPAND}\nOPENCODE_SERVER_PASSWORD=${{VD_DOTENV_FILE}}\n",
            EXPAND,
        ),
        (
            "process-interpolation",
            "OPENCODE_SERVER_PASSWORD=${VD_DOTENV_OTHER}\n",
            EXPAND,
        ),
        (
            "double-quoted",
            'OPENCODE_SERVER_PASSWORD="${VD_DOTENV_OTHER}"\n',
            EXPAND,
        ),
        (
            "single-quoted",
            "OPENCODE_SERVER_PASSWORD='${VD_DOTENV_OTHER}'\n",
            EXPAND,
        ),
        (
            "default",
            f"OPENCODE_SERVER_PASSWORD=${{VD_DOTENV_MISSING:-{EXPAND}}}\n",
            EXPAND,
        ),
        ("trailing-newline", 'OPENCODE_SERVER_PASSWORD="end\\n"\n', "end\n"),
    ]


def _prepare_dotenv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VD_DOTENV_OTHER", EXPAND)
    monkeypatch.delenv("VD_DOTENV_FILE", raising=False)
    monkeypatch.delenv("VD_DOTENV_MISSING", raising=False)


def _daemon_password(path: Path, required: str | None) -> str:
    pytest.importorskip("dotenv")
    from dotenv import dotenv_values

    value = dotenv_values(path).get("OPENCODE_SERVER_PASSWORD")
    assert value is not None, path.read_text(encoding="utf-8")
    if required is not None:
        assert value == required
    return value


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def test_windows_dotenv_reader_matches_daemon_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The serve launcher must load the same password the daemon will send.

    python-dotenv expands ``${NAME}`` from the file and the process environment,
    including ``${NAME:-default}`` and both quote styles. A launcher that keeps
    the literal starts serve with a different secret. The daemon then replaces
    that process, and the next start-backend health check is HTTP 401.
    """
    ps = _powershell()
    if ps is None:
        pytest.skip("powershell is not available")
    _prepare_dotenv_env(monkeypatch)
    probe = (
        "$ErrorActionPreference = 'Stop'\n"
        f". '{(WIN / 'ServeAuth.ps1').as_posix()}'\n"
        "$got = Read-DotEnvKey -Path $args[0] -Key 'OPENCODE_SERVER_PASSWORD'\n"
        "if (-not $got.Found) { Write-Output 'MISSING' } else {\n"
        "  Write-Output ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$got.Value)))\n"
        "}\n"
    )
    script = tmp_path / "read.ps1"
    script.write_text(probe, encoding="utf-8")
    mismatches: list[str] = []
    for label, text, required in _dotenv_cases():
        path = tmp_path / f"{label}.env"
        path.write_bytes(text.encode("utf-8"))
        expected = _daemon_password(path, required)
        code, out, _elapsed = _run(
            [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(path)],
            _env("OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME", VD_DOTENV_OTHER=EXPAND),
            20,
        )
        assert code == 0, out
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        got = lines[-1] if lines else ""
        want = _b64(expected)
        if got != want:
            mismatches.append(label)
    assert not mismatches, mismatches


def test_linux_import_matches_daemon_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Linux import must keep the daemon's password, including a trailing newline.

    ``vd_import_opencode_serve_auth`` reads the value with ``$(...)``, which
    strips trailing newlines before the serve process inherits it.
    """
    if _bash() is None:
        pytest.skip("bash is not available")
    _prepare_dotenv_env(monkeypatch)
    mismatches: list[str] = []
    for label, text, required in _dotenv_cases():
        path = tmp_path / f"{label}.env"
        path.write_bytes(text.encode("utf-8"))
        expected = _daemon_password(path, required)
        code, out, _elapsed = _bash_script(
            "unset OPENCODE_SERVER_PASSWORD\n"
            "unset OPENCODE_SERVER_USERNAME\n"
            'vd_import_opencode_serve_auth "$VD_ENV_FILE"\n'
            "printf '%s' \"$OPENCODE_SERVER_PASSWORD\" | base64 | tr -d '\\n\\r'\n",
            {"VD_ENV_FILE": str(path)},
        )
        assert code == 0, out
        got = out.strip().splitlines()[-1].strip()
        want = _b64(expected)
        if got != want:
            mismatches.append(label)
        assert EXPAND not in out
    assert not mismatches, mismatches


@contextmanager
def _real_password_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, password: str):
    """One OpenCode serve bound to a free port, with the daemon's password."""
    binary = resolve_opencode_binary()
    if not binary:
        pytest.skip("opencode is not installed")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    monkeypatch.setattr(settings, "opencode_serve_url", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", password)
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    work = tmp_path / "serve-cwd"
    work.mkdir()
    proc = subprocess.Popen(
        serve_command(binary, "127.0.0.1", port),
        cwd=str(work),
        env=serve_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 | 0x00000200 if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if probe_healthy():
                break
            if proc.poll() is not None:
                pytest.fail("opencode serve exited before it accepted the daemon password")
            time.sleep(0.2)
        else:
            pytest.fail("opencode serve did not accept the daemon password")
        yield port
    finally:
        kill_pid(int(proc.pid))
        for pid in default_listener_pids(port):
            if pid != int(proc.pid):
                kill_pid(pid)


def test_real_serve_accepts_the_launcher_password_from_the_same_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """start-backend must accept the serve the daemon would start from this .env.

    The file stores the password as a ``${}`` reference. The daemon expands it.
    Ensure-OpencodeServe.ps1 and ``vd_wait_serve_health`` send whatever they
    parsed. A 401 makes start-backend exit before the dashboard process starts.
    """
    if _powershell() is None and _bash() is None:
        pytest.skip("no launcher shell")
    _prepare_dotenv_env(monkeypatch)
    text = f"VD_DOTENV_FILE={EXPAND}\nOPENCODE_SERVER_PASSWORD=${{VD_DOTENV_FILE}}\n"
    env_file = tmp_path / ".env"
    env_file.write_bytes(text.encode("utf-8"))
    password = _daemon_password(env_file, EXPAND)
    failures: list[str] = []
    with _real_password_serve(tmp_path, monkeypatch, password) as port:
        ps = _powershell()
        if ps is not None:
            launcher_env = _env(
                "OPENCODE_SERVER_PASSWORD",
                "OPENCODE_SERVER_USERNAME",
                VD_DOTENV_OTHER=EXPAND,
            )
            code, out, _elapsed = _run(
                [
                    ps,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(WIN / "Ensure-OpencodeServe.ps1"),
                    "-ProjectDir",
                    str(tmp_path),
                    "-ServeHost",
                    "127.0.0.1",
                    "-ServePort",
                    str(port),
                    "-TimeoutSec",
                    "15",
                ],
                launcher_env,
                25,
            )
            if code != 0:
                failures.append(f"windows-exit-{code}")
            assert EXPAND not in out
        if _bash() is not None:
            code, out, _elapsed = _bash_script(
                "unset OPENCODE_SERVER_PASSWORD\n"
                "unset OPENCODE_SERVER_USERNAME\n"
                'vd_import_opencode_serve_auth "$VD_ENV_FILE"\n'
                'vd_wait_serve_health "$VD_URL" 8\n'
                "printf 'RC:%s\\n' $?\n",
                {
                    "VD_ENV_FILE": str(env_file),
                    "VD_URL": f"http://127.0.0.1:{port}/global/health",
                },
                timeout=20,
            )
            status = ""
            for line in out.splitlines():
                if line.startswith("RC:"):
                    status = line[3:].strip()
            if code != 0 or status != "0":
                failures.append(f"linux-status-{status or code}")
            assert EXPAND not in out
    assert not failures, failures
