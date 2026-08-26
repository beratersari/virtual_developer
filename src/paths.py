"""Durable on-disk locations for clones and agent state.

The install folder is replaced on every Windows zip reinstall. Session logs,
job records, and OpenCode binds must live *outside* that folder — default
``C:\\vd\\yaver`` (WSL: ``/mnt/c/vd/yaver``). Temp clones default to
``C:\\vd\\t`` so they stay short (MAX_PATH) and also survive reinstall.

Override with ``YAVER_DATA_DIR`` / ``VD_DATA_DIR`` and ``TEMP_DIR_BASE``.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional


WIN_DATA_DIR = Path(r"C:\vd\yaver")
WIN_TEMP_DIR = Path(r"C:\vd\t")
WSL_DATA_DIR = Path("/mnt/c/vd/yaver")
WSL_TEMP_DIR = Path("/mnt/c/vd/t")
_LEGACY_NAME = ".jira-agent"


def coerce_win_path(path: Path | str) -> Path:
    """``C:\\vd\\yaver`` in WSL becomes ``/mnt/c/vd/yaver``."""
    raw = str(path).strip()
    if os.name != "nt" and len(raw) >= 2 and raw[1] == ":":
        drive = raw[0].lower()
        rest = raw[2:].replace("\\", "/").lstrip("/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(raw).expanduser()


def _env_path(*names: str) -> Optional[Path]:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            p = coerce_win_path(raw)
            return p if p.is_absolute() else Path.cwd() / p
    return None


def _under_pytest() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("PYTEST"):
        return True
    return "pytest" in sys.modules


def uses_windows_layout() -> bool:
    """True on native Windows, or WSL with the project on a Windows drive."""
    if _under_pytest():
        return False
    if os.name == "nt":
        return True
    try:
        text = str(Path.cwd().resolve()).replace("\\", "/").lower()
    except OSError:
        return False
    return text.startswith("/mnt/c/") or text.startswith("/mnt/c")


def default_data_dir() -> Path:
    if os.name == "nt":
        return WIN_DATA_DIR
    if uses_windows_layout():
        return WSL_DATA_DIR
    return Path.cwd() / _LEGACY_NAME


def default_temp_dir() -> Path:
    if os.name == "nt":
        return WIN_TEMP_DIR
    if uses_windows_layout():
        return WSL_TEMP_DIR
    return Path(".temp")


def agent_data_dir() -> Path:
    """Root for sessions, jobs, state, binds — not the install folder."""
    override = _env_path("YAVER_DATA_DIR", "VD_DATA_DIR")
    if override is not None:
        return override
    return default_data_dir()


def legacy_agent_data_dir() -> Path:
    return Path.cwd() / _LEGACY_NAME


def agent_data_roots() -> List[Path]:
    """Current data dir plus leftover install-local ``.jira-agent`` (read/delete)."""
    roots = [agent_data_dir()]
    legacy = legacy_agent_data_dir()
    try:
        if legacy.is_dir() and legacy.resolve() != roots[0].resolve():
            roots.append(legacy)
    except OSError:
        pass
    return roots


def agent_subdir(*parts: str) -> Path:
    return agent_data_dir().joinpath(*parts)


def under_agent_data(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in agent_data_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def ensure_agent_data_dir(*, migrate: bool = False) -> Path:
    """Create the durable data dir. Optionally copy leftover ``.jira-agent`` once."""
    dest = agent_data_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return dest
    if migrate:
        _migrate_legacy_if_empty(dest)
    return dest


def _migrate_legacy_if_empty(dest: Path) -> None:
    """Copy leftover install-local entries that are missing at ``dest``.

    Dest may already have empty dirs from mkdir-only startup; still bring
    over ``sessions/`` and ``runtime_settings.json``.
    """
    src = legacy_agent_data_dir()
    try:
        if not src.is_dir() or src.resolve() == dest.resolve():
            return
    except OSError:
        return
    copied = 0
    try:
        children = list(src.iterdir())
    except OSError:
        return
    for child in children:
        target = dest / child.name
        if target.exists():
            continue
        try:
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
            copied += 1
        except OSError as e:
            _log(f"Could not migrate {child} -> {target}: {e}")
    if copied:
        _log(f"Migrated {copied} agent data item(s) {src} -> {dest}")


def _log(message: str) -> None:
    try:
        from src.logger import logger

        logger.info(message)
    except Exception:
        pass
