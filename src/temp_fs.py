"""Force-delete clone trees, including Windows reserved names such as ``nul``.

Windows treats ``nul``, ``con``, ``prn``, ``aux``, ``com1``… as devices.
``del folder\\nul`` and ``shutil.rmtree`` fail. The working commands are::

    rd /s /q \\\\?\\C:\\full\\path\\to\\folder
    del /f /q /a \\\\.\\C:\\full\\path\\to\\nul

``\\\\?\\`` is the Win32 long-path prefix; ``\\\\.\\`` is the device namespace.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from src.logger import logger

# Device names (with or without an extension) that Windows will not unlink
# through the normal Win32 path.
_WIN_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }
)


def is_windows() -> bool:
    return os.name == "nt"


def win_long_path(path: Path | str) -> str:
    """``\\\\?\\C:\\abs\\path`` so reserved names and long paths are reachable."""
    raw = str(Path(path))
    if raw.startswith("\\\\?\\"):
        return raw
    abs_path = os.path.abspath(raw)
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


def win_device_path(path: Path | str) -> str:
    """``\\\\.\\C:\\abs\\path`` — required for ``del`` of a ``nul`` file."""
    raw = str(Path(path))
    if raw.startswith("\\\\.\\"):
        return raw
    abs_path = os.path.abspath(raw)
    if abs_path.startswith("\\\\"):
        return "\\\\.\\UNC\\" + abs_path[2:]
    return "\\\\.\\" + abs_path


def _stem_reserved(name: str) -> bool:
    base = (name or "").split(".", 1)[0].strip().lower()
    return base in _WIN_RESERVED


def _chmod_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def _rmtree_onerror(func, path, exc_info) -> None:  # noqa: ARG001
    p = Path(path)
    _chmod_writable(p)
    try:
        func(path)
    except OSError:
        if is_windows():
            _win_unlink_one(p)


def _win_cmd(args: List[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _win_unlink_one(path: Path) -> None:
    """Delete one reserved-name file or empty dir via ``del`` / ``rd`` + prefixes."""
    long = win_long_path(path)
    device = win_device_path(path)
    _chmod_writable(path)
    # Long-path unlink first (works for most stubborn files).
    try:
        if path.is_dir() and not path.is_symlink():
            os.rmdir(long)
        else:
            os.unlink(long)
        return
    except OSError:
        pass
    # Device-namespace ``del`` — this is the command that removes ``nul``.
    _win_cmd(["cmd.exe", "/c", "del", "/f", "/q", "/a", device], timeout=60)
    if path.exists():
        _win_cmd(["cmd.exe", "/c", "rd", "/s", "/q", device], timeout=60)
    if path.exists():
        _win_cmd(["cmd.exe", "/c", "del", "/f", "/q", "/a", long], timeout=60)
    if path.exists():
        _win_cmd(["cmd.exe", "/c", "rd", "/s", "/q", long], timeout=60)


def _win_rd_tree(path: Path) -> None:
    """``rd /s /q \\\\?\\…`` — force-remove a whole tree, including ``nul``."""
    long = win_long_path(path)
    device = win_device_path(path)
    _win_cmd(["cmd.exe", "/c", "rd", "/s", "/q", long], timeout=600)
    if path.exists():
        _win_cmd(["cmd.exe", "/c", "rd", "/s", "/q", device], timeout=600)


def _walk_reserved(root: Path) -> Iterable[Path]:
    """Yield reserved-name entries bottom-up (files first)."""
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for ent in entries:
        child = Path(ent.path)
        if ent.is_dir(follow_symlinks=False):
            yield from _walk_reserved(child)
        if _stem_reserved(ent.name):
            yield child


def force_rmtree(path: Path | str) -> None:
    """Hard-delete ``path``. Raises ``OSError`` if anything remains."""
    target = Path(path)
    if not target.exists():
        return
    if is_windows():
        # Whole-tree ``rd`` with the long-path prefix.
        _win_rd_tree(target)
        if target.exists():
            for reserved in list(_walk_reserved(target)):
                try:
                    _win_unlink_one(reserved)
                except OSError as e:
                    logger.warning(f"force delete reserved {reserved}: {e}")
            _win_rd_tree(target)
        if target.exists():
            shutil.rmtree(target, onerror=_rmtree_onerror)
    else:
        shutil.rmtree(target, onerror=_rmtree_onerror)
    if target.exists():
        raise OSError(f"force delete left remnants at {target}")


def _collect_tree_entries(root: Path) -> List[Path]:
    """Files then child dirs (deepest first), then ``root``."""
    found: List[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
            dp = Path(dirpath)
            for name in filenames:
                found.append(dp / name)
            for name in dirnames:
                found.append(dp / name)
    except OSError:
        pass
    found.append(root)
    return found


def _remove_one(path: Path) -> None:
    """Unlink a file / reserved name, or rmdir an empty directory."""
    _chmod_writable(path)
    try:
        if path.is_symlink() or path.is_file():
            if is_windows():
                try:
                    os.unlink(win_long_path(path))
                    return
                except OSError:
                    _win_unlink_one(path)
                    return
            os.unlink(path)
            return
        if path.is_dir():
            if is_windows():
                try:
                    os.rmdir(win_long_path(path))
                    return
                except OSError:
                    _win_unlink_one(path)
                    return
            os.rmdir(path)
            return
    except OSError:
        if is_windows():
            _win_unlink_one(path)
        elif _stem_reserved(path.name):
            try:
                os.unlink(path)
            except OSError:
                pass


def force_rmtree_progress(
    path: Path | str,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Hard-delete ``path``, calling ``on_progress(done, total)`` as entries go.

    Entry-by-entry so the dashboard can show a percent. Remnants fall back to
    ``force_rmtree`` (Windows ``rd /s /q`` + reserved-name unlink).
    """
    target = Path(path)
    if not target.exists():
        if on_progress is not None:
            on_progress(1, 1)
        return
    entries = _collect_tree_entries(target)
    total = max(1, len(entries))
    if on_progress is not None:
        on_progress(0, total)
    done = 0
    for entry in entries:
        try:
            if entry.exists() or entry.is_symlink():
                _remove_one(entry)
        except OSError as e:
            logger.warning(f"force delete entry {entry}: {e}")
        done += 1
        if on_progress is not None:
            on_progress(done, total)
    if target.exists():
        force_rmtree(target)
    if on_progress is not None:
        on_progress(total, total)


def format_bytes(n: int) -> str:
    """Human size for dashboard DTOs (KiB/MiB/GiB)."""
    value = float(max(0, int(n)))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    idx = 0
    while value >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def volume_label(path: Path) -> str:
    """Drive letter on Windows (``C:``), otherwise the mount point."""
    resolved = path
    try:
        resolved = path.resolve()
    except OSError:
        pass
    if is_windows():
        drive = getattr(resolved, "drive", "") or ""
        if drive:
            return drive.upper() if drive.endswith(":") else f"{drive.upper()}:"
        text = str(resolved)
        if len(text) >= 2 and text[1] == ":":
            return text[:2].upper()
        return str(resolved)
    return _posix_mount(resolved)


def _posix_mount(path: Path) -> str:
    best = "/"
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as fh:
            mounts = []
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mounts.append(parts[1])
    except OSError:
        return str(path)
    text = str(path)
    for mount in sorted(mounts, key=len, reverse=True):
        if text == mount or text.startswith(mount.rstrip("/") + "/"):
            return mount
    return best


def disk_usage_for(path: Path):
    probe = path
    while True:
        try:
            return shutil.disk_usage(probe)
        except OSError:
            parent = probe.parent
            if parent == probe:
                raise
            probe = parent
