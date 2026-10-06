"""Update a real 0.9.71 executable with the package on the release site.

The 0.9.71 and 0.9.72 executables were built before ``yaver update``
existed, so this drives ``run_apply_job`` from the current tree. The
download, checksum, helper, file swap, and the restarted executable are
the real ones. Nothing here is the git checkout, and nothing uses port
8080.

Set YAVER_UPDATE_E2E_DIR to the folder that holds:

* yaver-windows-x64-0.9.71.zip
* yaver-linux-x64-ubuntu-22.04-0.9.71.zip

The release site must already be serving 0.9.72 for Windows on
http://127.0.0.1:8090. Ubuntu 22.04 is served from a second copy of that
site inside WSL, because this machine's firewall drops WSL connections
to the Windows listener.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_ZIP_DIR = Path(os.environ.get("YAVER_UPDATE_E2E_DIR", ""))
_SITE = "http://127.0.0.1:8090"
_BUSY = {8080, 5173, 4096, 8090}
_WIN_ZIP = "yaver-windows-x64-0.9.71.zip"
_LINUX_ZIP = "yaver-linux-x64-ubuntu-22.04-0.9.71.zip"

_DRIVER = r"""
import os
import sys
import traceback
from pathlib import Path

base, install, work, port, exe, root = sys.argv[1:7]
# The helper copies this environment into the restarted executable.
# A value inherited from the git checkout would override that install's .env.
for key in list(os.environ):
    if key.startswith((
        "DASHBOARD_", "JIRA_", "YAVER_", "RELEASE_", "GITLAB_",
        "AZURE_", "OPENCODE_", "TEMP_DIR",
    )):
        os.environ.pop(key, None)
root_path = Path(root)
sys.path.insert(0, str(root_path))
os.chdir(root_path)
work_path = Path(work)
work_path.mkdir(parents=True, exist_ok=True)
try:
    install_path = Path(install).resolve()
    if (install_path / ".git").exists():
        raise RuntimeError("refusing a git checkout")
    if root_path == install_path or root_path in install_path.parents:
        raise RuntimeError("refusing to update the git checkout")
    chosen = int(port)
    if chosen in {8080, 5173, 4096, 8090}:
        raise RuntimeError("refusing a live Yaver port")
    import src
    import src.self_update as su

    su.install_root = lambda: install_path
    su._work_dir = lambda: work_path
    su._dashboard_probe = lambda: ("127.0.0.1", chosen)
    su.local_layout = lambda: "frozen"
    su.checkout_block = lambda: ""
    su._restart_argv = lambda: [exe, "start"]
    real_plan = su._plan

    def _plan(*args, **kwargs):
        built = real_plan(*args, **kwargs)
        built["health_seconds"] = 120
        built["wait_seconds"] = 30
        return built

    su._plan = _plan
    src.__version__ = "0.9.71"
    su.run_apply_job(base, lambda: os._exit(0))
except SystemExit:
    raise
except Exception:
    (work_path / "driver-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
    os._exit(1)
"""


def _need_zips() -> Path:
    if not os.environ.get("YAVER_UPDATE_E2E_DIR"):
        pytest.skip("set YAVER_UPDATE_E2E_DIR to the folder of release zips")
    if not _ZIP_DIR.is_dir():
        pytest.skip("YAVER_UPDATE_E2E_DIR is not a directory of release zips")
    return _ZIP_DIR


def _get(url: str, timeout: float = 20) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _site_up(platform: str) -> dict:
    try:
        payload = json.loads(_get(f"{_SITE}/api/latest?platform={platform}"))
    except Exception as exc:
        pytest.skip(f"release site is not serving {platform}: {exc}")
    if str(payload.get("version") or "") != "0.9.72":
        pytest.skip(f"{platform} on the release site is {payload.get('version')!r}")
    return payload


def _free_port() -> int:
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in _BUSY:
            return port
    raise AssertionError("no free dashboard port")


def _listener(port: int) -> int:
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-NetTCPConnection -LocalPort "
                    + str(port)
                    + " -State Listen -ErrorAction SilentlyContinue | "
                    "Select-Object -ExpandProperty OwningProcess"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return 0


def _stop_port(port: int) -> None:
    if port in _BUSY or port <= 0:
        raise AssertionError(f"refusing to stop port {port}")
    pid = _listener(port)
    if pid <= 0:
        return
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _env(port: int, data: Path) -> str:
    return (
        "JIRA_HOST=https://jira.example.invalid\n"
        "JIRA_API_TOKEN=e2e-token\n"
        "DASHBOARD_HOST=127.0.0.1\n"
        f"DASHBOARD_PORT={port}\n"
        "DASHBOARD_ENABLED=true\n"
        f"YAVER_BASE_DIR={data}\n"
    )


def _run_driver(base: str, install: Path, work: Path, port: int, exe: str) -> subprocess.CompletedProcess[str]:
    script = work / "driver.py"
    script.write_text(_DRIVER, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys_exe(), str(script), base, str(install), str(work), str(port), exe, str(_ROOT)],
        cwd=str(_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def sys_exe() -> str:
    import sys

    return sys.executable


def _text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _details(proc: subprocess.CompletedProcess[str], work: Path) -> str:
    parts = [f"exit={proc.returncode}", proc.stdout or "", proc.stderr or ""]
    for name in ("driver-error.txt", "update.log", "last_result.json", "plan.json"):
        parts.append(f"--- {name} ---\n{_text(work / name)}")
    return "\n".join(parts)


def _wait_meta(port: int, seconds: float = 30) -> dict:
    import time

    deadline = time.time() + seconds
    last = ""
    url = f"http://127.0.0.1:{port}/api/meta"
    while time.time() < deadline:
        try:
            return json.loads(_get(url, timeout=2))
        except Exception as exc:
            last = str(exc)
            time.sleep(0.4)
    raise AssertionError(f"no meta on {url}: {last}")


def _install_root(extracted: Path, names: set[str]) -> Path:
    if any((extracted / name).exists() for name in names):
        return extracted
    children = [path for path in extracted.iterdir()]
    dirs = [path for path in children if path.is_dir()]
    files = [path for path in children if path.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    raise AssertionError(f"no executable at the top of {extracted}")


def test_install_page_commands_extract_the_published_windows_zip(tmp_path: Path) -> None:
    """The install page's Windows commands match the zip the site serves."""
    if os.name != "nt":
        pytest.skip("Windows tar.exe extract")
    _need_zips()
    _site_up("windows")
    page = _get(f"{_SITE}/install").decode("utf-8", errors="replace")
    assert "update.bat" in page
    assert "./update.sh" in page
    assert "setsid nohup ./yaver start" in page
    assert "chmod 755 yaver" in page
    assert "notepad .env" in page
    assert "Copy-Item .env.example .env" in page
    assert "tar.exe -xf yaver-windows.zip -C yaver" in page
    assert "unzip -o /tmp/yaver-22.04.zip" in page
    assert "--host" not in page
    assert "--port" not in page
    archive = tmp_path / "yaver-windows.zip"
    archive.write_bytes(_get(f"{_SITE}/download/windows", timeout=120))
    dest = tmp_path / "yaver"
    dest.mkdir()
    proc = subprocess.run(
        ["tar.exe", "-xf", str(archive), "-C", str(dest)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (dest / "yaver.exe").is_file()
    assert (dest / "_internal").is_dir()
    assert (dest / ".env.example").is_file()
    assert (dest / "VERSION").read_text(encoding="utf-8").strip() == "0.9.72"


def _stop_under(token: str) -> None:
    if not token or any(char in token for char in " *?[]'\"\\"):
        raise AssertionError(f"unsafe process token {token!r}")
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -like '*"
        + token
        + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_windows_071_updates_to_the_published_package() -> None:
    if os.name != "nt":
        pytest.skip("Windows executable")
    zips = _need_zips()
    old_zip = zips / _WIN_ZIP
    if not old_zip.is_file():
        pytest.skip(f"missing {old_zip}")
    _site_up("windows")
    port = _free_port()
    root = zips.parent / "run-win"
    _stop_under("run-win")
    if root.exists():
        shutil.rmtree(root)
    install_parent = root / "app"
    install_parent.mkdir(parents=True)
    proc = subprocess.run(
        ["tar.exe", "-xf", str(old_zip), "-C", str(install_parent)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    install = _install_root(install_parent, {"yaver.exe"})
    assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.9.71"
    data = root / "data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    env_bytes = _env(port, data).encode("utf-8")
    (install / ".env").write_bytes(env_bytes)
    (root / "canary.txt").write_text("canary", encoding="utf-8")
    work = root / "work"
    work.mkdir()
    try:
        driver = _run_driver(_SITE, install, work, port, str(install / "yaver.exe"))
        assert driver.returncode == 0, _details(driver, work)
        result_path = work / "last_result.json"
        deadline_text = _text(result_path)
        import time

        deadline = time.time() + 150
        while time.time() < deadline and not result_path.is_file():
            time.sleep(0.4)
        assert result_path.is_file(), _details(driver, work) + deadline_text
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        assert result["ok"] is True, _text(work / "update.log")
        assert (install / "VERSION").read_text(encoding="utf-8").strip() == "0.9.72"
        assert (install / ".env").read_bytes() == env_bytes
        assert (data / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
        assert (root / "canary.txt").read_text(encoding="utf-8") == "canary"
        previous = install.parent / f"{install.name}.previous"
        assert (previous / "VERSION").read_text(encoding="utf-8").strip() == "0.9.71"
    finally:
        _stop_port(port)
def test_wsl_ubuntu_22_071_updates_to_the_published_package() -> None:
    if os.name != "nt":
        pytest.skip("WSL is launched from Windows")
    zips = _need_zips()
    old_zip = zips / _LINUX_ZIP
    if not old_zip.is_file():
        pytest.skip(f"missing {old_zip}")
    published = _site_up("ubuntu-22.04")
    probe = subprocess.run(
        ["wsl", "-d", "Ubuntu-22.04", "--", "echo", "ok"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0 or "ok" not in (probe.stdout or ""):
        pytest.skip(f"Ubuntu-22.04 is not running: {probe.stderr}")
    releases = _ROOT.parent / "yaver-releases"
    files = releases / "data" / "files"
    matches = sorted(files.glob("ubuntu-22.04-0.9.72-*.zip"))
    assert matches, "the release site has no Ubuntu 22.04 zip on disk"
    e2e = zips.parent
    script = e2e / "wsl-update.sh"
    summary = e2e / "wsl-summary.json"
    driver = e2e / "wsl-driver.py"
    if summary.exists():
        summary.unlink()
    driver.write_bytes(_DRIVER.replace("\r\n", "\n").encode("utf-8"))
    body = _wsl_script().format(
        blob=_wsl_mnt(matches[0]),
        old=_wsl_mnt(old_zip),
        e2e=_wsl_mnt(e2e),
        repo=_wsl_mnt(_ROOT),
        releases=_wsl_mnt(releases),
        sha=str(published["sha256"]),
    )
    script.write_bytes(body.replace("\r\n", "\n").encode("utf-8"))
    proc = subprocess.run(
        ["wsl", "-d", "Ubuntu-22.04", "--", "bash", _wsl_mnt(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )
    reported = _text(summary)
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "") + "\n" + reported
    result = json.loads(reported)
    assert result["ok"] is True, result
    assert result["version"] == "0.9.72"
    assert result["previous"] == "0.9.71"
    assert result["env_same"] is True
    assert result["data_kept"] is True
    assert result["mode"] == "755"


def _wsl_mnt(path: Path) -> str:
    text = str(path.resolve())
    drive = text[0].lower()
    rest = text[2:].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _wsl_script() -> str:
    return """#!/bin/bash
set -eu
ROOT=/root/yaver-e2e
BLOB={blob}
OLD={old}
E2E={e2e}
REPO={repo}
RELEASES={releases}
export EXPECTED_SHA={sha}
export PYTHONIOENCODING=utf-8

stop_port() {{
  pid=$(ss -ltnp "sport = :$1" 2>/dev/null | sed -n 's/.*pid=\\([0-9]\\+\\).*/\\1/p' | head -n 1)
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    sleep 1
  fi
}}
cleanup() {{
  stop_port 18082
  stop_port 18090
}}
trap cleanup EXIT

rm -rf "$ROOT"
mkdir -p "$ROOT/data/yaver" "$ROOT/work" "$ROOT/site" "$ROOT/check" "$ROOT/old"
printf 'kept\\n' > "$ROOT/data/yaver/keep.txt"

PYTHONPATH="$RELEASES" python3 - <<'PY'
import hashlib, os, shutil
from pathlib import Path
import sys
sys.path.insert(0, os.environ.get("PYTHONPATH", ""))
from yaver_releases.platforms import flatten_wrapper, inspect_zip, version_from_zip
from yaver_releases.store import ReleaseStore
src = Path("{blob}")
blob = Path("/root/yaver-e2e/site/package.zip")
shutil.copyfile(src, blob)
flatten_wrapper(blob)
layout = inspect_zip(blob)
version = version_from_zip(blob)
digest = hashlib.sha256(blob.read_bytes()).hexdigest()
expected = os.environ["EXPECTED_SHA"]
if digest != expected or version != "0.9.72" or layout != "frozen":
    raise SystemExit(f"package {{version}} {{layout}} {{digest}}")
ReleaseStore(Path("/root/yaver-e2e/site")).publish(
    platform="ubuntu-22.04",
    version=version,
    filename="yaver-linux-x64-ubuntu-22.04-0.9.72.zip",
    sha256=digest,
    size=blob.stat().st_size,
    layout=layout,
    notes="",
    blob=blob,
)
PY

PYTHONPATH="$RELEASES" YAVER_RELEASE_DATA="$ROOT/site" YAVER_RELEASE_HOST=127.0.0.1 YAVER_RELEASE_PORT=18090 \\
  python3 -m yaver_releases > "$ROOT/site.log" 2>&1 &
echo $! > "$ROOT/site.pid"
ok=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  if curl -fsS -m 2 http://127.0.0.1:18090/api/health >/dev/null; then
    ok=1
    break
  fi
  sleep 0.3
done
if [ "$ok" != 1 ]; then
  echo "release site inside WSL did not start" >&2
  cat "$ROOT/site.log" >&2 || true
  exit 1
fi

unzip -qo "$BLOB" -d "$ROOT/check"
test -f "$ROOT/check/yaver"
test -d "$ROOT/check/_internal"
test "$(stat -c %a "$ROOT/check/yaver")" = "755"
test "$(tr -d '[:space:]' < "$ROOT/check/VERSION")" = "0.9.72"
command -v setsid >/dev/null
command -v unzip >/dev/null

unzip -qo "$OLD" -d "$ROOT/old"
found=$(find "$ROOT/old" -type f -name yaver ! -path '*/_internal/*' | head -n 1)
install=$(dirname "$found")
test -n "$found"
test "$(tr -d '[:space:]' < "$install/VERSION")" = "0.9.71"
chmod 755 "$install/yaver"
python3 - "$install" <<'PY'
import sys
from pathlib import Path
install = Path(sys.argv[1])
install.joinpath(".env").write_text(
    "JIRA_HOST=https://jira.example.invalid\\n"
    "JIRA_API_TOKEN=e2e-token\\n"
    "DASHBOARD_HOST=127.0.0.1\\n"
    "DASHBOARD_PORT=18082\\n"
    "DASHBOARD_ENABLED=true\\n"
    "YAVER_BASE_DIR=/root/yaver-e2e/data\\n",
    encoding="utf-8",
)
PY
cp "$E2E/wsl-driver.py" "$ROOT/work/driver.py"
set +e
python3 "$ROOT/work/driver.py" \\
  http://127.0.0.1:18090 \\
  "$install" \\
  "$ROOT/work" \\
  18082 \\
  "$install/yaver" \\
  "$REPO"
driver_code=$?
set -e
python3 - "$install" "$driver_code" <<'PY'
import json, sys, time, urllib.request
from pathlib import Path
install = Path(sys.argv[1])
driver_code = int(sys.argv[1 + 1])
work = Path("/root/yaver-e2e/work")
root = Path("/root/yaver-e2e")
deadline = time.time() + 150
result_path = work / "last_result.json"
while time.time() < deadline and not result_path.is_file():
    time.sleep(0.4)
result = {{}}
error = ""
if result_path.is_file():
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
else:
    for name in ("driver-error.txt", "update.log", "site.log"):
        path = work / name if name != "site.log" else root / name
        if path.is_file():
            error += "\\n" + path.read_text(encoding="utf-8", errors="replace")
version_file = ""
ver_path = install / "VERSION"
if ver_path.is_file():
    version_file = ver_path.read_text(encoding="utf-8").strip()
last = ""
expected = (
    "JIRA_HOST=https://jira.example.invalid\\n"
    "JIRA_API_TOKEN=e2e-token\\n"
    "DASHBOARD_HOST=127.0.0.1\\n"
    "DASHBOARD_PORT=18082\\n"
    "DASHBOARD_ENABLED=true\\n"
    "YAVER_BASE_DIR=/root/yaver-e2e/data\\n"
).encode()
env_now = (install / ".env").read_bytes() if (install / ".env").is_file() else b""
previous = install.parent / (install.name + ".previous")
prev = (previous / "VERSION").read_text(encoding="utf-8").strip() if (previous / "VERSION").is_file() else ""
mode = ""
if (install / "yaver").is_file():
    mode = format((install / "yaver").stat().st_mode & 0o777, "o")
data_kept = (root / "data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept\\n"
version = version_file
ok = (
    driver_code == 0
    and result.get("ok") is True
    and version == "0.9.72"
    and env_now == expected
    and data_kept
    and prev == "0.9.71"
    and mode == "755"
)
if not ok and not error:
    log = (work / "update.log").read_text(encoding="utf-8", errors="replace") if (work / "update.log").is_file() else ""
    error = f"driver={{driver_code}} result={{result}} version={{version}} last={{last}} prev={{prev}} mode={{mode}}\\n{{log}}"
Path("{e2e}/wsl-summary.json").write_text(json.dumps({{
    "ok": ok,
    "version": version,
    "previous": prev,
    "env_same": env_now == expected,
    "data_kept": data_kept,
    "mode": mode,
    "error": error,
}}), encoding="utf-8")
raise SystemExit(0 if ok else 1)
PY
"""
