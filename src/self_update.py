"""Retired in-process installer. The operator path is the script.

Run ``update.bat`` on Windows or ``./update.sh`` on Linux from the
install folder. ``cli.py`` must not call ``update_stopped_install``.
This module stays so the older helper tests still have a library.

The old command stopped a dashboard that was already listening, then a
helper copied outside the install swapped the files. The helper does not
start Yaver. An existing ``.env`` is left untouched, and the data folder
is kept. A git checkout is refused.

On Windows a frozen ``yaver.exe`` keeps ``_internal`` mapped until the
process exits. That command records its own pid and exits before the
helper moves the folder. A source install can stay open: its pid in the
plan is 0, and the helper swaps while the command waits.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from src.install_paths import install_root, is_frozen, resource_root
from src.logger import logger
from src.update_helper import UpdateError, classify_tree, safe_extract

_MAX_DOWNLOAD = 8 * 1024 * 1024 * 1024
_PLATFORMS = {
    "windows": "Windows",
    "ubuntu-18.04": "Ubuntu 18.04",
    "ubuntu-20.04": "Ubuntu 20.04",
    "ubuntu-22.04": "Ubuntu 22.04",
    "ubuntu-24.04": "Ubuntu 24.04",
}
_HOST = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)
_SHA = re.compile(r"^[0-9a-f]{64}$")
_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "phase": "idle",
    "error": "",
    "remote_version": "",
    "remote_notes": "",
    "remote_layout": "",
    "bytes_done": 0,
    "bytes_total": 0,
}
_BUSY = {"downloading", "verifying", "restarting"}


def release_base_url(host: str, port: int) -> str:
    """Build ``http://host:port``. The host is an IP or a hostname."""
    raw = (host or "").strip()
    if not raw:
        raise UpdateError("Set the release server address and port.")
    if any(char in raw for char in " /\\?@#"):
        raise UpdateError("Address must be an IP or a hostname, with no scheme or path.")
    inner = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
    try:
        parsed = ipaddress.ip_address(inner)
    except ValueError:
        parsed = None
        if not _HOST.fullmatch(inner):
            raise UpdateError("Address must be an IP or a hostname.") from None
    if isinstance(port, bool) or not isinstance(port, int) or port < 1 or port > 65535:
        raise UpdateError("Port must be from 1 to 65535.")
    if parsed is not None and parsed.version == 6:
        shown = f"[{parsed.compressed}]"
    elif parsed is not None:
        shown = str(parsed)
    else:
        shown = inner
    return f"http://{shown}:{port}"


def platform_from_os(system: str, os_release: str = "") -> str:
    if system == "Windows":
        return "windows"
    if system != "Linux":
        raise UpdateError(
            "Updates are published for Windows and Ubuntu 18.04, 20.04, 22.04, and 24.04."
        )
    info = _os_release_map(os_release)
    version = info.get("VERSION_ID", "").strip().strip('"')
    if info.get("ID", "").strip().strip('"') != "ubuntu":
        raise UpdateError(
            "Updates are published for Windows and Ubuntu 18.04, 20.04, 22.04, and 24.04."
        )
    for key in ("18.04", "20.04", "22.04", "24.04"):
        if version == key or version.startswith(key + "."):
            return f"ubuntu-{key}"
    label = version or "unknown"
    raise UpdateError(f"Ubuntu {label} does not have a published package.")


def local_platform() -> str:
    text = ""
    if sys.platform.startswith("linux"):
        try:
            text = Path("/etc/os-release").read_text(encoding="utf-8")
        except OSError as exc:
            raise UpdateError(
                "Updates are published for Windows and Ubuntu 18.04, 20.04, 22.04, and 24.04."
            ) from exc
    system = "Windows" if sys.platform == "win32" else ("Linux" if sys.platform.startswith("linux") else sys.platform)
    return platform_from_os(system, text)


def local_layout() -> str:
    return "frozen" if is_frozen() else "source"


def checkout_block() -> str:
    try:
        root = install_root()
    except OSError:
        return "The install folder could not be read."
    if (root / ".git").exists():
        return (
            "This folder is a git checkout. Update replaces an installed copy: "
            "the executable folder, or an extracted release zip."
        )
    return ""


def versions_equal(left: str, right: str) -> bool:
    """True when both names are the same release.

    Build metadata (``+...``) is ignored. A prerelease (``-rc1``) is a
    different package from the final release of the same numbers.
    """
    left_id = _version_identity(left)
    right_id = _version_identity(right)
    return bool(left_id) and left_id == right_id


def snapshot() -> dict[str, Any]:
    with _LOCK:
        job = dict(_JOB)
    return _view(job)


def check_for_update(host: str, port: int) -> dict[str, Any]:
    _reject_if_busy()
    base = release_base_url(host, port)
    save_release_target(host.strip(), int(port))
    platform = local_platform()
    _set(phase="idle", error="", bytes_done=0, bytes_total=0)
    try:
        remote = fetch_latest(base, platform)
    except UpdateError as exc:
        _set(phase="idle", error=str(exc), remote_version="", remote_notes="", remote_layout="")
        raise
    _set(
        phase="idle",
        error="",
        remote_version=str(remote.get("version") or ""),
        remote_notes=str(remote.get("notes") or ""),
        remote_layout=str(remote.get("layout") or ""),
    )
    logger.info(
        f"Update check {base} platform={platform} "
        f"remote={remote.get('version') or ''}"
    )
    return snapshot()


def start_apply(
    host: str,
    port: int,
    *,
    shutdown: Callable[[], None] | None,
) -> dict[str, Any]:
    _reject_if_busy()
    base = release_base_url(host, port)
    blocked = checkout_block()
    if blocked:
        raise UpdateError(blocked)
    try:
        local_platform()
    except UpdateError:
        raise
    if shutdown is None or not callable(shutdown):
        raise UpdateError("This process cannot restart itself.", status_code=503)
    # Claim before the settings write. That write is slow, and a second
    # click during it would otherwise start a second helper.
    _claim_busy("downloading")
    try:
        save_release_target(host.strip(), int(port))
    except Exception as exc:
        _set(phase="error", error=str(exc) or "The release server address could not be saved.")
        raise
    thread = threading.Thread(
        target=_apply_thread,
        args=(base, shutdown),
        name="yaver-update",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:
        _set(phase="error", error="The update could not start.")
        raise
    return snapshot()


def save_release_target(host: str, port: int) -> None:
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(
        SettingsUpdate(release_host=host, release_port=int(port))
    )


def updates_dir(data_dir: Path, install: Path) -> Path:
    """Keep the download outside the folder that the helper replaces."""
    data = data_dir.resolve()
    root = install.resolve()
    if data == root or root in data.parents:
        base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "yaver-update"
    else:
        base = data / "updates"
    base.mkdir(parents=True, exist_ok=True)
    return base


def fetch_latest(base: str, platform: str) -> dict[str, Any]:
    if platform not in _PLATFORMS:
        raise UpdateError("This computer does not match a published package.")
    url = f"{base}/api/latest?platform={quote(platform)}"
    try:
        with _client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise UpdateError("The release server could not be reached.") from exc
    if response.is_redirect:
        raise UpdateError("The release server sent a redirect. The download stays on that address.")
    if response.status_code == 404:
        raise UpdateError(f"No {_PLATFORMS[platform]} release is published on that server.")
    if response.status_code != 200:
        raise UpdateError(f"The release server answered HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpdateError("The release server did not return a release.") from exc
    if not isinstance(payload, dict):
        raise UpdateError("The release server did not return a release.")
    version = str(payload.get("version") or "").strip()
    sha = str(payload.get("sha256") or "").strip().lower()
    if not version or not _SHA.fullmatch(sha):
        raise UpdateError("The release server did not send a version and checksum.")
    payload["version"] = version
    payload["sha256"] = sha
    payload["layout"] = str(payload.get("layout") or "")
    return payload


def download_release(
    base: str,
    platform: str,
    dest: Path,
    *,
    expected_sha: str,
    expected_size: int,
) -> None:
    url = f"{base}/download/{quote(platform)}"
    digest = hashlib.sha256()
    done = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".partial")
    try:
        with _client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise UpdateError(
                        "The release server sent a redirect. The download stays on that address."
                    )
                if response.status_code != 200:
                    raise UpdateError(
                        f"The download answered HTTP {response.status_code}."
                    )
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(256 * 1024):
                        if not chunk:
                            continue
                        done += len(chunk)
                        if done > _MAX_DOWNLOAD:
                            raise UpdateError("The download is larger than 8 GB.")
                        if expected_size and done > expected_size + 1024:
                            raise UpdateError(
                                "The download is larger than the published file."
                            )
                        digest.update(chunk)
                        handle.write(chunk)
                        _set(bytes_done=done, bytes_total=expected_size or done)
        actual = digest.hexdigest()
        if actual != expected_sha.lower():
            raise UpdateError("The download did not match the published checksum.")
        os.replace(partial, dest)
    except UpdateError:
        try:
            partial.unlink()
        except OSError:
            pass
        raise
    except httpx.HTTPError as exc:
        try:
            partial.unlink()
        except OSError:
            pass
        raise UpdateError("The release server could not be reached.") from exc
    finally:
        if partial.exists() and not dest.exists():
            try:
                partial.unlink()
            except OSError:
                pass


def install_is_running() -> bool:
    """True when this install's dashboard port is already accepting connections."""
    from src.update_helper import port_open

    probe, port = _dashboard_probe()
    if port < 1:
        return False
    return port_open(port, probe)


def _endpoint_port(token: str) -> int | None:
    text = token.strip()
    if text.startswith("[") and "]:" in text:
        text = text.rsplit("]:", 1)[-1]
    else:
        text = text.rsplit(":", 1)[-1]
    if text.isdigit():
        return int(text)
    return None


def listener_pids_from_netstat(text: str, port: int) -> list[int]:
    """Listening PIDs from ``netstat -ano`` whose local port is exactly *port*."""
    want = int(port)
    found: list[int] = []
    for line in (text or "").splitlines():
        if "LISTEN" not in line.upper():
            continue
        parts = line.split()
        if len(parts) < 4 or _endpoint_port(parts[1]) != want:
            continue
        pid_text = parts[-1]
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid > 0 and pid not in found:
            found.append(pid)
    return found


def _dashboard_listener_pids(port: int) -> list[int]:
    """PIDs listening on the dashboard port. Empty when the list cannot be read."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            return listener_pids_from_netstat(result.stdout or "", port)
        from src.opencode_serve_supervisor import pids_from_ss

        result = subprocess.run(
            ["ss", "-ltnp", f"sport = :{int(port)}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        return pids_from_ss(result.stdout or "")
    except (OSError, subprocess.TimeoutExpired):
        return []


def _stop_listener(pid: int) -> None:
    """Stop the process that owns the dashboard socket. Leave its other children."""
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        from src.update_helper import _terminate_one

        _terminate_one(pid)
        return
    import signal

    from src.update_helper import wait_dead

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if not wait_dead(pid, 5):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _terminal(report: Callable[[str], None] | None, message: str) -> None:
    """Print one update step on this command's terminal.

    The line is paths, ports, and versions. ``.env`` contents stay out.
    """
    text = " ".join(str(message).splitlines()).strip()
    if not text:
        return
    if report is not None:
        report(text)
        return
    logger.info(text)


def _process_image(pid: int) -> str:
    """Executable name for a pid. Empty when it cannot be read.

    The command line is left unread. It can hold paths from ``.env``.
    """
    if pid <= 0 or pid == os.getpid():
        return ""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            line = (result.stdout or "").strip()
            if not line or str(int(pid)) not in line:
                return ""
            name = line.split(",")[0].strip().strip('"')
            if not name or name.lower().startswith("info:"):
                return ""
            return name
        comm = Path(f"/proc/{int(pid)}/comm")
        if comm.is_file():
            return comm.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    return ""


def stop_running_dashboard(report: Callable[[str], None] | None = None) -> None:
    """Stop the process listening on the dashboard port.

    The update command itself keeps running while that listener stops.
    The file swap still waits until this port is closed, and it does not
    start Yaver. On Windows the executable exits later, before
    ``_internal`` is moved.
    """
    from src.update_helper import pid_alive, port_open, wait_port_closed

    probe, port = _dashboard_probe()
    _terminal(report, f"dashboard probe={probe} port={port}")
    if port < 1 or not port_open(port, probe):
        _terminal(report, f"dashboard port {port} is closed")
        return
    if report is not None:
        report("Stopping Yaver.")
    logger.info(f"Stopping Yaver on port {port}")
    victims = [
        pid for pid in _dashboard_listener_pids(port) if pid > 0 and pid != os.getpid()
    ]
    if not victims:
        _terminal(report, "dashboard port is open and no listener pid was found")
        raise UpdateError(
            "The dashboard port is open, and its process could not be found."
        )
    for pid in victims:
        image = _process_image(pid)
        label = f"pid={pid} image={image}" if image else f"pid={pid}"
        _terminal(report, f"stopping listener {label}")
        _stop_listener(pid)
        _terminal(report, f"listener pid={pid} alive={str(pid_alive(pid)).lower()}")
    if not wait_port_closed(port, 15, probe):
        _terminal(report, f"dashboard port {port} still open")
        raise UpdateError("Yaver is still running.")
    _terminal(report, f"dashboard port {port} is closed")


def _dashboard_probe() -> tuple[str, int]:
    from src.config import settings

    raw_host = str(getattr(settings, "dashboard_host", "0.0.0.0") or "0.0.0.0").strip()
    probe = "127.0.0.1" if raw_host in {"0.0.0.0", "::", ""} else raw_host
    try:
        port = int(getattr(settings, "dashboard_port", 8080) or 8080)
    except (TypeError, ValueError):
        port = 8080
    return probe, port


def _chosen_release(host: str | None, port: int | None) -> tuple[str, int]:
    from src.config import settings

    chosen_host = host if host is not None else str(getattr(settings, "release_host", "") or "")
    chosen_host = chosen_host.strip()
    if port is None:
        try:
            chosen_port = int(getattr(settings, "release_port", 0) or 0)
        except (TypeError, ValueError):
            chosen_port = 0
    else:
        chosen_port = int(port)
    return chosen_host, chosen_port


def update_stopped_install(
    host: str | None,
    port: int | None,
    *,
    shutdown: Callable[[], None],
    report: Callable[[str], None] | None = None,
) -> None:
    """Download the published package and wait while the helper swaps files.

    A dashboard that is already listening is stopped first. The helper
    does not start Yaver. Each step is printed on this terminal.
    """
    _terminal(
        report,
        f"update pid={os.getpid()} frozen={is_frozen()} install={install_root()}",
    )
    try:
        from src import __version__

        _terminal(report, f"local version={__version__}")
    except Exception as exc:
        _terminal(report, f"local version unread: {type(exc).__name__}")
    stop_running_dashboard(report)
    chosen_host, chosen_port = _chosen_release(host, port)
    _terminal(report, f"release {chosen_host}:{chosen_port}")
    base = release_base_url(chosen_host, chosen_port)
    blocked = checkout_block()
    if blocked:
        raise UpdateError(blocked)
    save_release_target(chosen_host, chosen_port)
    run_apply_job(base, shutdown, report=report)


def run_apply_job(
    base: str,
    shutdown: Callable[[], None],
    *,
    spawn: Callable[[Path, str], None] | None = None,
    report: Callable[[str], None] | None = None,
) -> None:
    def say(text: str) -> None:
        _terminal(report, text)

    _run_apply_job(base, shutdown, say, spawn)


def _run_apply_job(
    base: str,
    shutdown: Callable[[], None],
    say: Callable[[str], None],
    spawn: Callable[[Path, str], None] | None,
) -> None:
    platform = local_platform()
    layout = local_layout()
    say(f"fetch {base} platform={platform} layout={layout}")
    remote = fetch_latest(base, platform)
    version = str(remote["version"])
    remote_layout = str(remote.get("layout") or "")
    size = int(remote.get("size") or 0)
    _set(remote_version=version, remote_notes=str(remote.get("notes") or ""), remote_layout=remote_layout)
    from src import __version__

    say(
        f"remote version={version} layout={remote_layout or 'unknown'} "
        f"size={size} local={__version__}"
    )
    if versions_equal(version, __version__):
        raise UpdateError(f"This install is already {version}.")
    if remote_layout in {"frozen", "source"} and remote_layout != layout:
        raise UpdateError(_layout_mismatch(layout, remote_layout))
    work = _work_dir()
    archive = work / f"yaver-{version}.zip"
    _set(phase="downloading", bytes_done=0, bytes_total=size, error="")
    say(f"Downloading {version}.")
    say(f"downloading {archive}")
    download_release(
        base,
        platform,
        archive,
        expected_sha=str(remote["sha256"]),
        expected_size=size,
    )
    say(f"downloaded {archive} bytes={archive.stat().st_size}")
    _set(phase="verifying")
    say("Checking the package.")
    staging_parent = work / "staging"
    if staging_parent.exists():
        shutil.rmtree(staging_parent, ignore_errors=True)
    payload = safe_extract(archive, staging_parent)
    found = classify_tree(payload)
    say(f"staging {payload} classified={found}")
    if found != layout:
        if found in {"frozen", "source"}:
            raise UpdateError(_layout_mismatch(layout, found))
        raise UpdateError("The published package is not a Yaver executable or install zip.")
    plan_path = work / "plan.json"
    plan = _plan(payload, version, layout)
    # A source install swaps while this command waits. Pid 0 tells the
    # helper the mover is already free. A Windows executable has
    # _internal mapped for the whole process, so the plan keeps this pid
    # and the process exits before the move.
    handoff = spawn is None and _windows_frozen_handoff(layout)
    if spawn is None and not handoff:
        plan["pid"] = 0
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    if layout == "frozen" and sys.platform != "win32":
        _write_shell_plan(work / "plan.env", plan)
    applier = _applier_kind()
    say(f"helper {applier} plan={plan_path}")
    if spawn is not None:
        spawn(plan_path, applier)
        _set(phase="restarting", error="")
        logger.info(f"Update staged {version}; replacing files")
        say("helper detached")
        say(
            "The package is ready. The files will be replaced after this process exits. "
            "Start Yaver after that."
        )
        shutdown()
        return
    if handoff:
        say("Closing Yaver so Windows can replace the files.")
        say(
            "The command prompt can return before the copy finishes. "
            "Wait for the line that begins with Updated to."
        )
        try:
            os.chdir(plan_path.parent)
        except OSError as exc:
            raise UpdateError("Could not leave the install folder.") from exc
        _spawn_console(_helper_argv(plan_path, applier), plan_path.parent)
        _set(phase="restarting", error="")
        logger.info(f"Update staged {version}; replacing files after this process exits")
        say("helper continues after this process exits")
        shutdown()
        return
    _set(phase="restarting", error="")
    logger.info(f"Update staged {version}; replacing files")
    say("Replacing the files.")
    try:
        code = _run_helper_foreground(plan_path, applier)
    except OSError as exc:
        logger.exception("Could not start the updater", exc)
        say(f"helper launch failed: {type(exc).__name__}: {exc}")
        plan["pid"] = os.getpid()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        if layout == "frozen" and sys.platform != "win32":
            _write_shell_plan(work / "plan.env", plan)
        spawn_helper(plan_path, applier)
        say("helper will run after this command exits")
        say(
            "The files will be replaced after this command exits. "
            "Start Yaver when the copy finishes."
        )
        shutdown()
        return
    say(f"helper exit={code}")
    if code != 0:
        message = _helper_failure(plan)
        _set(phase="error", error=message)
        raise UpdateError(message)
    shutdown()


def _helper_argv(plan_path: Path, kind: str) -> list[str]:
    work = plan_path.parent
    if kind == "python":
        interpreter = _interpreter()
        if not interpreter:
            raise UpdateError("Python was not found, so the files cannot be replaced.")
        script = work / "update_helper.py"
        shutil.copy2(_helper_file("update_helper.py"), script)
        return [interpreter, str(script), "--apply", str(plan_path)]
    if kind == "powershell":
        script = work / "update_helper.ps1"
        shutil.copy2(_helper_file("update_helper.ps1"), script)
        windir = os.environ.get("SystemRoot", r"C:\Windows")
        powershell = str(Path(windir) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PlanPath",
            str(plan_path),
        ]
    script = work / "update_helper.sh"
    shutil.copy2(_helper_file("update_helper.sh"), script)
    try:
        os.chmod(script, 0o755)
    except OSError:
        pass
    return ["sh", str(script), str(work / "plan.env")]


def spawn_helper(plan_path: Path, kind: str) -> None:
    work = plan_path.parent
    _spawn(_helper_argv(plan_path, kind), work, work / "update.log")


def _windows_frozen_handoff(layout: str) -> bool:
    """True when this process is the Windows executable that maps ``_internal``."""
    return sys.platform == "win32" and layout == "frozen" and is_frozen()


def _spawn_console(args: list[str], cwd: Path) -> None:
    """Start the helper on this console and return.

    The caller exits afterwards. ``CREATE_NO_WINDOW`` would hide the
    steps, and staying inside ``yaver.exe`` keeps ``_internal`` locked.
    """
    kwargs: dict[str, Any] = {
        "args": args,
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # Leave a kill-on-close job, and keep this console.
        kwargs["creationflags"] = 0x00000200 | 0x01000000
        try:
            subprocess.Popen(**kwargs)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                raise UpdateError(
                    "Windows would stop the updater when this Yaver closes. "
                    "Start yaver.exe on its own, then update again."
                ) from exc
            raise
    kwargs["start_new_session"] = True
    subprocess.Popen(**kwargs)


def _run_helper_foreground(plan_path: Path, kind: str) -> int:
    """Run the helper and wait. A source install stays open until the copy finishes.

    A Windows executable does not use this path. It maps ``_internal`` until
    it exits, so the helper runs only after that process is gone. Windows
    ``os.execv`` starts a second process and drops its exit code, so the
    console would return before the outcome is known. The helper replaces
    files in place and does not start Yaver.
    """
    argv = _helper_argv(plan_path, kind)
    try:
        os.chdir(plan_path.parent)
    except OSError as exc:
        raise UpdateError("Could not leave the install folder.") from exc
    program = argv[0]
    if program == "sh":
        resolved = shutil.which("sh") or "/bin/sh"
        argv = [resolved, *argv[1:]]
    completed = subprocess.run(
        argv,
        cwd=str(plan_path.parent),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return int(completed.returncode)


def _helper_failure(plan: dict[str, Any]) -> str:
    path = Path(str(plan.get("result") or ""))
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        text = str(data.get("error") or "").strip()
        if text:
            return text
    return "The files were not replaced. The update log has the details."


def _apply_thread(base: str, shutdown: Callable[[], None]) -> None:
    try:
        # A background thread must not replace the daemon process image.
        run_apply_job(base, shutdown, spawn=spawn_helper)
    except UpdateError as exc:
        logger.warning(f"Update stopped: {exc}")
        _set(phase="error", error=str(exc))
    except Exception as exc:
        logger.exception("Update failed", exc)
        _set(phase="error", error="The update could not finish. The log has the details.")


def _plan(staging: Path, version: str, layout: str) -> dict[str, Any]:
    root = install_root().resolve()
    probe, port = _dashboard_probe()
    return {
        "created": time.time(),
        "pid": os.getpid(),
        "port": port,
        "probe_host": probe,
        "layout": layout,
        "staging": str(staging),
        "install_root": str(root),
        "argv": _restart_argv(),
        "cwd": str(root),
        "version": version,
        "log": str(_work_dir() / "update.log"),
        "result": str(_work_dir() / "last_result.json"),
        "health_seconds": 45,
        "wait_seconds": 90,
    }


def _restart_argv() -> list[str]:
    if is_frozen():
        return [str(Path(sys.executable).resolve()), "start"]
    return [sys.executable, "-m", "src.daemon"]


def _write_shell_plan(path: Path, plan: dict[str, Any]) -> None:
    argv = list(plan["argv"])
    fields = {
        "PID": str(int(plan["pid"])),
        "PORT": str(int(plan["port"])),
        "LAYOUT": str(plan["layout"]),
        "STAGING": str(plan["staging"]),
        "INSTALL": str(plan["install_root"]),
        "VERSION": str(plan["version"]),
        "LOG": str(plan["log"]),
        "RESULT": str(plan["result"]),
        "HEALTH_SECONDS": str(int(plan["health_seconds"])),
        "PROBE": str(plan.get("probe_host") or "127.0.0.1"),
        "ARGV0": str(argv[0]),
        "ARGV1": str(argv[1]) if len(argv) > 1 else "",
    }
    lines = []
    for key, value in fields.items():
        if "'" in value or "\n" in value:
            raise UpdateError("A path in the update plan cannot be quoted for the shell helper.")
        lines.append(f"{key}='{value}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _applier_kind() -> str:
    if not is_frozen():
        return "python"
    if _interpreter():
        return "python"
    if sys.platform == "win32":
        return "powershell"
    return "bash"


def _host_executable(path: str) -> str:
    """Executable image that runs Python in this process.

    On Windows a venv or ``py`` launcher stays running and starts the
    real interpreter as its child. Stopping that launcher stops the
    child, so the helper has to be the real interpreter.
    """
    if sys.platform != "win32":
        return path
    name = Path(path).name.lower()
    probe = (
        "import ctypes, os\n"
        "from ctypes import wintypes\n"
        "kernel = ctypes.WinDLL('kernel32', use_last_error=True)\n"
        "kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]\n"
        "kernel.OpenProcess.restype = wintypes.HANDLE\n"
        "kernel.QueryFullProcessImageNameW.argtypes = "
        "[wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]\n"
        "kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL\n"
        "kernel.CloseHandle.argtypes = [wintypes.HANDLE]\n"
        "kernel.CloseHandle.restype = wintypes.BOOL\n"
        "handle = kernel.OpenProcess(0x1000, False, os.getpid())\n"
        "if not handle:\n"
        "    raise SystemExit(1)\n"
        "size = wintypes.DWORD(32768)\n"
        "buf = ctypes.create_unicode_buffer(32768)\n"
        "ok = kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))\n"
        "kernel.CloseHandle(handle)\n"
        "if not ok:\n"
        "    raise SystemExit(1)\n"
        "print(buf.value)\n"
    )
    command = [path, "-c", probe]
    if name in {"py", "py.exe"}:
        command = [path, "-3", "-c", probe]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            command,
            timeout=20,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return path
    if proc.returncode != 0:
        return path
    image = ""
    for line in (proc.stdout or "").splitlines():
        image = line.strip()
    if image and Path(image).is_file():
        return image
    return path


def _interpreter() -> str:
    if not is_frozen():
        return _host_executable(sys.executable)
    names = ("python", "python3", "py") if sys.platform == "win32" else ("python3", "python")
    for name in names:
        found = shutil.which(name)
        if not found:
            continue
        host = _host_executable(found)
        if not host or not _outside_install(host):
            continue
        if _python_ok(host):
            return host
    return ""


def _outside_install(path: str) -> bool:
    try:
        resolved = Path(path).resolve()
        root = install_root().resolve()
    except OSError:
        return False
    return resolved != root and root not in resolved.parents


def _python_ok(path: str) -> bool:
    name = Path(path).name.lower()
    command = [path, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"]
    if name in {"py", "py.exe"}:
        command = [path, "-3", "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"]
    try:
        proc = subprocess.run(
            command,
            timeout=20,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _helper_file(name: str) -> Path:
    bundled = resource_root() / name
    if bundled.is_file():
        return bundled
    local = Path(__file__).resolve().parent / name
    if local.is_file():
        return local
    raise UpdateError(f"The update helper {name} is not installed.")


def _spawn(args: list[str], cwd: Path, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    try:
        kwargs: dict[str, Any] = {
            "args": args,
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # Leave the parent's job. When yaver.exe exits, a job that
            # kills its members would stop this helper during the copy.
            # DETACHED_PROCESS does not leave that job. It also makes
            # powershell.exe exit before the script runs, so the
            # PowerShell helper keeps a windowless process group only.
            flags = 0x00000200 | 0x08000000 | 0x01000000
            if Path(args[0]).name.lower() not in {"powershell.exe", "pwsh.exe"}:
                flags |= 0x00000008
            kwargs["creationflags"] = flags
            try:
                subprocess.Popen(**kwargs)
                return
            except OSError as exc:
                # Access denied means this process is in a job that will
                # not let the helper out. Starting it inside that job
                # kills the helper when yaver.exe exits, during the copy.
                if getattr(exc, "winerror", None) == 5:
                    raise UpdateError(
                        "Windows would stop the updater when this Yaver closes. "
                        "Start yaver.exe on its own, then update again."
                    ) from exc
                raise
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(**kwargs)
    finally:
        handle.close()


def _work_dir() -> Path:
    from src.paths import agent_data_dir

    return updates_dir(agent_data_dir(), install_root())


def _layout_mismatch(local: str, remote: str) -> str:
    local_label = "the executable" if local == "frozen" else "the install zip"
    remote_label = "an executable zip" if remote == "frozen" else "a full install zip"
    return (
        f"This copy is {local_label}, and the published package is {remote_label}. "
        "Upload the matching package on the release server."
    )


def _view(job: dict[str, Any]) -> dict[str, Any]:
    from src import __version__
    from src.config import settings

    blocked = ""
    platform = ""
    label = ""
    try:
        platform = local_platform()
        label = _PLATFORMS.get(platform, platform)
    except UpdateError as exc:
        blocked = str(exc)
    checkout = checkout_block()
    if checkout:
        blocked = checkout
    host = str(getattr(settings, "release_host", "") or "")
    try:
        port = int(getattr(settings, "release_port", 0) or 0)
    except (TypeError, ValueError):
        port = 0
    phase = str(job.get("phase") or "idle")
    error = str(job.get("error") or "")
    remote_version = str(job.get("remote_version") or "")
    same = bool(remote_version) and versions_equal(remote_version, __version__)
    available = bool(remote_version) and not same and not blocked and not error
    if phase in _BUSY:
        available = False
    last = _last_result()
    message = _message(
        phase=phase,
        error=error,
        blocked=blocked,
        host=host,
        port=port,
        remote_version=remote_version,
        same=same,
        label=label,
        last=last,
        current=__version__,
    )
    return {
        "phase": phase,
        "message": message,
        "current_version": __version__,
        "platform": platform,
        "platform_label": label,
        "layout": local_layout(),
        "release_host": host,
        "release_port": port,
        "remote_version": remote_version,
        "remote_notes": str(job.get("remote_notes") or ""),
        "remote_layout": str(job.get("remote_layout") or ""),
        "update_available": available,
        "bytes_done": int(job.get("bytes_done") or 0),
        "bytes_total": int(job.get("bytes_total") or 0),
        "error": error,
        "can_apply": available and phase in {"idle", "error"},
        "block_reason": blocked,
    }


def _message(
    *,
    phase: str,
    error: str,
    blocked: str,
    host: str,
    port: int,
    remote_version: str,
    same: bool,
    label: str,
    last: dict[str, Any],
    current: str,
) -> str:
    if phase == "downloading":
        return f"Downloading {remote_version or 'the release'}."
    if phase == "verifying":
        return "Checking the download."
    if phase == "restarting":
        return (
            f"Replacing this copy with {remote_version}. "
            "Start Yaver after the files are in place."
        )
    if error:
        return error
    if blocked:
        return blocked
    if not host or port < 1:
        return "Set the release server address and port, then check."
    if same:
        return f"This install is already {current}."
    if remote_version:
        return f"{remote_version} is published for {label or 'this computer'}."
    if last.get("ok") and str(last.get("version") or "") == current:
        return f"Updated to {current}."
    if last.get("error"):
        return str(last.get("error"))
    return "Check for a published release."


def _last_result() -> dict[str, Any]:
    try:
        path = _work_dir() / "last_result.json"
    except OSError:
        return {}
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set(**kwargs: Any) -> None:
    with _LOCK:
        _JOB.update(kwargs)


def _reject_if_busy() -> None:
    with _LOCK:
        phase = str(_JOB.get("phase") or "idle")
    if phase in _BUSY:
        raise UpdateError("An update is already running.", status_code=409)


def _claim_busy(phase: str) -> None:
    """Mark an update running, or refuse when one is already running."""
    with _LOCK:
        current = str(_JOB.get("phase") or "idle")
        if current in _BUSY:
            raise UpdateError("An update is already running.", status_code=409)
        _JOB.update(phase=phase, error="", bytes_done=0, bytes_total=0)


def _version_identity(value: str) -> str:
    return (value or "").strip().split("+", 1)[0]


def _os_release_map(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, raw = line.split("=", 1)
        info[key.strip()] = raw.strip().strip('"').strip("'")
    return info


def _client(*, timeout: httpx.Timeout) -> httpx.Client:
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
    return httpx.Client(timeout=timeout, verify=False, follow_redirects=False)
