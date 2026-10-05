"""Replace an installed Yaver tree after this process has exited.

Stdlib only. The daemon copies this file outside the install and runs:

    python update_helper.py --apply PLAN.json

A git checkout is refused. ``.env`` is copied back after the new files
are in place. The data folder is not inside the install, so it stays.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path


class UpdateError(Exception):
    """The published package cannot be applied."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


_MAX_MEMBERS = 200_000
_MAX_UNCOMPRESSED = 16 * 1024 * 1024 * 1024
_PRESERVE = {".env", ".venv", ".git"}


def classify_members(names: list[str]) -> str:
    """Classify a zip listing. One wrapping folder is unwrapped."""
    cleaned: list[str] = []
    for name in names:
        parts = _member_parts(name)
        if parts:
            cleaned.append("/".join(parts))
    prefixes = {item.split("/", 1)[0] for item in cleaned if "/" in item}
    root_files = [item for item in cleaned if "/" not in item]
    relative = cleaned
    if len(prefixes) == 1 and not root_files:
        prefix = next(iter(prefixes)) + "/"
        relative = [item[len(prefix):] for item in cleaned if item.startswith(prefix)]
    joined = set(relative)
    has_internal = any(item == "_internal" or item.startswith("_internal/") for item in joined)
    has_exe = "yaver.exe" in joined or "yaver" in joined
    has_source = "src/daemon.py" in joined and "VERSION" in joined
    if has_exe and has_internal:
        return "frozen"
    if has_source:
        return "source"
    return "unknown"


def classify_tree(root: Path) -> str:
    has_internal = (root / "_internal").is_dir()
    has_exe = (root / "yaver.exe").is_file() or (root / "yaver").is_file()
    has_source = (root / "src" / "daemon.py").is_file() and (root / "VERSION").is_file()
    if has_exe and has_internal:
        return "frozen"
    if has_source:
        return "source"
    return "unknown"


def safe_extract(zip_path: Path, dest: Path) -> Path:
    """Extract *zip_path* into *dest* and return the payload root."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > _MAX_MEMBERS:
            raise UpdateError("The release zip has too many files.")
        total = 0
        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            if _is_link(info):
                raise UpdateError("The release zip contains a symlink.")
            parts = _member_parts(info.filename)
            total += max(0, int(info.file_size))
            if total > _MAX_UNCOMPRESSED:
                raise UpdateError("The release zip expands to more than 16 GB.")
            if not parts:
                continue
            target = dest_root.joinpath(*parts).resolve()
            if dest_root != target and dest_root not in target.parents:
                raise UpdateError("The release zip escapes its folder.")
            planned.append((info, target))
        for info, target in planned:
            if info.is_dir() or str(info.filename).replace("\\", "/").endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            _apply_zip_mode(info, target)
    return payload_root(dest_root)


def _apply_zip_mode(info: zipfile.ZipInfo, target: Path) -> None:
    """Keep the Unix permission bits stored on a zip member.

    ``open`` creates a normal file and drops the executable bit. A Linux
    ``yaver`` with no mode in the zip is still marked executable.
    """
    mode = (int(info.external_attr) >> 16) & 0o7777
    if not mode and target.name.lower() in {"yaver", "yaver.exe"}:
        mode = 0o755
    if not mode:
        return
    try:
        os.chmod(target, mode)
    except OSError:
        return


def payload_root(dest: Path) -> Path:
    children = [path for path in dest.iterdir()]
    dirs = [path for path in children if path.is_dir()]
    files = [path for path in children if path.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return dest


def assert_install_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.parent == resolved:
        raise UpdateError("Refusing to replace a drive root.")
    return resolved


def apply_source_tree(staging: Path, install: Path) -> None:
    """Copy a full install zip onto *install*. Keep ``.env`` and ``.venv``."""
    install = assert_install_dir(install)
    _refuse_checkout(install)
    assert_no_symlinks(staging)
    if not staging.is_dir():
        raise UpdateError("The staged release is missing.")
    for child in staging.iterdir():
        if child.name in _PRESERVE:
            continue
        dest = install / child.name
        if child.is_dir():
            _mirror_dir(child, dest)
        else:
            _copy_file(child, dest)


def _env_value(env_bytes: bytes | None, name: str) -> str:
    if not env_bytes:
        return ""
    found = ""
    for raw in env_bytes.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            found = value.strip().strip('"').strip("'")
    return found


def _userdata_rel(install: Path, env_bytes: bytes | None) -> Path | None:
    """Relative data folder when it lives strictly inside the install."""
    raw = _env_value(env_bytes, "YAVER_BASE_DIR")
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = install / candidate
    if candidate.is_symlink():
        return None
    try:
        if not candidate.is_dir():
            return None
        root = install.resolve()
        resolved = candidate.resolve()
        rel = resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved == root or not rel.parts or "_internal" in rel.parts:
        return None
    if any(part in {"", ".", ".."} for part in rel.parts):
        return None
    return rel


def _userdata_stash(install: Path) -> Path:
    return install.parent / f"{install.name}.userdata"


def _park_userdata(install: Path, env_bytes: bytes | None) -> Path | None:
    rel = _userdata_rel(install, env_bytes)
    if rel is None:
        return None
    stash = _userdata_stash(install)
    if stash.exists():
        raise UpdateError(
            "A previous update left the data folder beside Yaver. "
            "Move that folder back before updating again."
        )
    rename_retry(install / rel, stash)
    return rel


def _unpark_userdata(install: Path, rel: Path) -> None:
    stash = _userdata_stash(install)
    if not stash.exists():
        return
    dest = install / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if dest.is_symlink():
            raise UpdateError("The data folder in the new copy is a link.")
        _rmtree(dest)
    rename_retry(stash, dest)


def apply_frozen_tree(staging: Path, install: Path) -> None:
    """Swap an executable folder. The previous folder is kept beside it."""
    install = assert_install_dir(install)
    _refuse_checkout(install)
    assert_no_symlinks(staging)
    if classify_tree(staging) != "frozen":
        raise UpdateError("The staged release is not a Yaver executable.")
    env_bytes = None
    env_path = install / ".env"
    if env_path.is_file():
        env_bytes = env_path.read_bytes()
    # The executable folder is replaced as a whole. A data folder inside
    # it would leave with the old tree, so park it and put it back.
    rel = _park_userdata(install, env_bytes)
    previous = install.parent / f"{install.name}.previous"
    try:
        if previous.exists():
            _rmtree(previous)
        rename_retry(install, previous)
        shutil.copytree(staging, install)
        if env_bytes is not None:
            (install / ".env").write_bytes(env_bytes)
        if rel is not None:
            _unpark_userdata(install, rel)
            rel = None
    except Exception:
        _restore_frozen(install, previous)
        if rel is not None:
            _unpark_userdata(install, rel)
        raise


def run_plan(path: Path) -> None:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise UpdateError("The update plan is not a JSON object.")
    result_path = Path(str(plan.get("result") or ""))
    version = str(plan.get("version") or "")
    try:
        _run(plan)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if result_path:
            write_result(result_path, ok=False, version=version, error=message)
        _log(plan, message)
        raise
    if result_path:
        write_result(result_path, ok=True, version=version, error="")
    _log(plan, f"started {version}")


def write_result(path: Path, *, ok: bool, version: str, error: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"ok": bool(ok), "version": version, "error": error},
        ensure_ascii=False,
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--apply":
        print("usage: update_helper.py --apply PLAN.json", file=sys.stderr)
        return 2
    try:
        run_plan(Path(args[1]))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _run(plan: dict) -> None:
    created = float(plan.get("created") or 0)
    if created <= 0 or time.time() - created > 6 * 3600:
        raise UpdateError("This update plan is too old.")
    install = assert_install_dir(Path(str(plan.get("install_root") or "")))
    staging = Path(str(plan.get("staging") or "")).resolve()
    if not staging.is_dir():
        raise UpdateError("The staged release is missing.")
    if install == staging or install in staging.parents or staging in install.parents:
        raise UpdateError("The staging folder overlaps the install.")
    layout = str(plan.get("layout") or "")
    if layout not in {"frozen", "source"}:
        raise UpdateError("The update plan has no install kind.")
    if classify_tree(staging) != layout:
        raise UpdateError("The staged release does not match this install.")
    _refuse_checkout(install)
    pid = int(plan.get("pid") or 0)
    wait_seconds = float(plan.get("wait_seconds") or 90)
    if pid > 0 and not wait_dead(pid, wait_seconds):
        _log(plan, "stopping the running Yaver")
        terminate_pid(pid)
        if not wait_dead(pid, 15):
            raise UpdateError("Yaver is still running.")
    port = int(plan.get("port") or 0)
    probe = str(plan.get("probe_host") or "127.0.0.1")
    wait_port_closed(port, 30, probe)
    _log(plan, f"applying {layout}")
    if layout == "frozen":
        apply_frozen_tree(staging, install)
    else:
        apply_source_tree(staging, install)
    argv = _argv(plan)
    cwd = str(plan.get("cwd") or install)
    log_path = str(plan.get("log") or "")
    try:
        proc = start_logged(argv, cwd, log_path)
    except OSError as exc:
        if layout == "frozen" and _restart_previous_frozen(install, argv, cwd, log_path):
            raise UpdateError(
                "The new Yaver could not be started. The previous copy was restored."
            ) from exc
        raise UpdateError("The new Yaver could not be started.") from exc
    health_seconds = float(plan.get("health_seconds") if plan.get("health_seconds") is not None else 45)
    if port > 0 and health_seconds > 0 and not wait_port_open(port, health_seconds, probe):
        if proc.poll() is None:
            terminate_pid(proc.pid)
            wait_dead(proc.pid, 10)
        if layout == "frozen":
            _restart_previous_frozen(install, argv, cwd, log_path)
            raise UpdateError(
                "The new Yaver did not open. The previous copy was restored."
            )
        raise UpdateError(
            "The new files are in place, but Yaver did not open the dashboard port."
        )


def _restart_previous_frozen(install: Path, argv: list[str], cwd: str, log_path: str) -> bool:
    """Put the previous executable folder back and start it."""
    previous = install.parent / f"{install.name}.previous"
    if not previous.is_dir():
        return False
    env_path = install / ".env"
    env_bytes = env_path.read_bytes() if env_path.is_file() else None
    rel = _park_userdata(install, env_bytes)
    try:
        _restore_frozen(install, previous)
    finally:
        if rel is not None:
            _unpark_userdata(install, rel)
    start_logged(argv, cwd, log_path)
    return True


def _argv(plan: dict) -> list[str]:
    raw = plan.get("argv")
    if not isinstance(raw, list) or not raw:
        raise UpdateError("The update plan has no start command.")
    if not all(isinstance(item, str) and item.strip() for item in raw):
        raise UpdateError("The update plan has no start command.")
    return [str(item) for item in raw]


def _refuse_checkout(install: Path) -> None:
    if (install / ".git").exists():
        raise UpdateError(
            "This folder is a git checkout. Update replaces an installed copy: "
            "the executable folder, or an extracted release zip."
        )


def assert_no_symlinks(root: Path) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + list(files):
            if (Path(current) / name).is_symlink():
                raise UpdateError("Refusing a symlink in the release.")


def _mirror_dir(source: Path, dest: Path) -> None:
    if dest.exists() and not dest.is_dir():
        dest.unlink()
    dest.mkdir(parents=True, exist_ok=True)
    incoming = {child.name for child in source.iterdir()}
    for existing in list(dest.iterdir()):
        if existing.name in incoming:
            continue
        if existing.is_dir() and not existing.is_symlink():
            _rmtree(existing)
        else:
            existing.unlink()
    for child in source.iterdir():
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            _mirror_dir(child, target)
        else:
            _copy_file(child, target)


def _copy_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def rename_retry(source: Path, dest: Path, attempts: int = 40) -> None:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            os.rename(source, dest)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.25)
    raise UpdateError(f"Could not move {source.name}: {last}")


def _restore_frozen(install: Path, previous: Path) -> None:
    if not previous.is_dir():
        return
    broken = install.parent / f"{install.name}.broken"
    if broken.exists():
        _rmtree(broken)
    if install.exists():
        rename_retry(install, broken)
    rename_retry(previous, install)


def _rmtree(path: Path) -> None:
    def _onexc(func, target, exc) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=_onexc)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, 0, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_dead(pid: int, seconds: float) -> bool:
    if pid <= 0:
        return True
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.4)
    return not pid_alive(pid)


def _windows_snapshot() -> tuple[dict[int, list[int]], dict[int, int]]:
    """Child lists and parent pids. Empty when the process list cannot be read."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snap = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snap or int(snap) == -1:
        return {}, {}
    children: dict[int, list[int]] = {}
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            parent = int(entry.th32ParentProcessID)
            if pid > 0:
                parents[pid] = parent
                children.setdefault(parent, []).append(pid)
            ok = kernel.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snap)
    return children, parents


def _descendants(root: int, children: dict[int, list[int]]) -> list[int]:
    found: list[int] = []
    seen = {int(root)}
    stack = [int(root)]
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in seen or child <= 0:
                continue
            seen.add(child)
            found.append(child)
            stack.append(child)
    return found


def _ancestors(pid: int, parents: dict[int, int]) -> list[int]:
    found: list[int] = []
    seen = {int(pid)}
    current = int(pid)
    while len(found) < 64:
        parent = int(parents.get(current, 0) or 0)
        if parent <= 0 or parent in seen:
            break
        seen.add(parent)
        found.append(parent)
        current = parent
    return found


def _terminate_one(pid: int) -> None:
    """Stop one process and return. Do not stop its children.

    ``taskkill /T`` kills this helper: it is inside the running Yaver
    tree. ``taskkill`` without ``/T`` does not return when the target is
    the Python launcher that is waiting on this process, and stopping
    that launcher stops the helper before the new files are in place.
    """
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0001, False, int(pid))
    if not handle:
        return
    try:
        kernel.TerminateProcess(handle, 1)
    finally:
        kernel.CloseHandle(handle)


def terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    if sys.platform == "win32":
        me = os.getpid()
        if pid == me:
            return
        children, parents = _windows_snapshot()
        # The process we were asked to stop is often this helper's child
        # (the new Yaver, whose cwd keeps the folder locked) or an ancestor
        # (the old Yaver). Protecting every descendant would refuse both.
        target_tree = {int(pid), *_descendants(pid, children)}
        protected = {me}
        for child in _descendants(me, children):
            if child not in target_tree:
                protected.add(child)
        # The launcher that started this script dies with us. Leave it
        # running. Still stop the requested pid when that pid is Yaver.
        for ancestor in _ancestors(me, parents):
            if ancestor != pid:
                protected.add(ancestor)
        victims = [item for item in _descendants(pid, children) if item not in protected]
        for victim in reversed(victims):
            _terminate_one(victim)
        if pid not in protected:
            _terminate_one(pid)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if not wait_dead(pid, 5):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    if port <= 0:
        return False
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            return sock.connect_ex((host or "127.0.0.1", int(port))) == 0
        except OSError:
            return False


def wait_port_closed(port: int, seconds: float, host: str = "127.0.0.1") -> bool:
    if port <= 0:
        return True
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if not port_open(port, host):
            return True
        time.sleep(0.4)
    return not port_open(port, host)


def wait_port_open(port: int, seconds: float, host: str = "127.0.0.1") -> bool:
    if port <= 0 or seconds <= 0:
        return True
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port, host):
            return True
        time.sleep(0.4)
    return port_open(port, host)


def start_logged(argv: list[str], cwd: str, log_path: str) -> subprocess.Popen:
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
    else:
        handle = subprocess.DEVNULL
    try:
        kwargs: dict = {
            "args": argv,
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        env = os.environ.copy()
        # A new yaver.exe must not stay tied to the executable that spawned us.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        kwargs["env"] = env
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
            try:
                return subprocess.Popen(**kwargs)
            except OSError:
                kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(**kwargs)
    finally:
        if handle is not subprocess.DEVNULL:
            handle.close()


def _log(plan: dict, message: str) -> None:
    path = str(plan.get("log") or "")
    if not path:
        return
    dest = Path(path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S") + " " + message + "\n")
    except OSError:
        return


def _member_parts(name: str) -> list[str]:
    import re

    text = (name or "").replace("\\", "/").strip()
    if not text:
        return []
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise UpdateError("The release zip contains an absolute path.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UpdateError("The release zip contains a parent path.")
    return parts


def _is_link(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr) >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


if __name__ == "__main__":
    raise SystemExit(main())
