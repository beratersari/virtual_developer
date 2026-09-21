"""Durable on-disk locations for clones and agent state.

One operator setting, ``YAVER_BASE_DIR``. Yaver creates two folders under it:

* ``{base}/yaver`` — sessions, jobs, state, plans, logs
* ``{base}/t`` — temp git clones (short name for Windows MAX_PATH)

Defaults follow other local tools, in a folder the user can write without
administrator rights:

* Windows: ``%LOCALAPPDATA%\\Yaver`` (same place as Docker and other
  per-user app data; not the roaming profile)
* Linux and WSL: ``$XDG_DATA_HOME/yaver`` or ``~/.local/share/yaver``
  (same place as OpenCode's ``~/.local/share/opencode``)

Plans are ``{base}/yaver/plans/{ISSUE_KEY}.md`` (not inside the clone).

An existing ``.env`` that still sets ``YAVER_DATA_DIR`` or ``TEMP_DIR_BASE``
keeps those exact paths so months of data are not moved. Those two variables
override the base. Legacy ``.jira-agent/`` next to the repo is only a
migrate/read fallback.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional


_LEGACY_NAME = ".jira-agent"
_DATA_FOLDER = "yaver"
_TEMP_FOLDER = "t"


def coerce_win_path(path: Path | str) -> Path:
    """Map a Windows path onto this host.

    WSL (``/mnt/c`` present): ``C:\\vd\\yaver`` → ``/mnt/c/vd/yaver``.
    Native Linux: ``C:\\vd\\yaver`` → ``/vd/yaver`` (or ``~/vd/yaver``).
    """
    raw = str(path).strip()
    if os.name != "nt" and len(raw) >= 2 and raw[1] == ":":
        drive = raw[0].lower()
        rest = raw[2:].replace("\\", "/").lstrip("/")
        mnt = Path(f"/mnt/{drive}")
        try:
            if mnt.is_dir():
                return Path(f"/mnt/{drive}/{rest}")
        except OSError:
            pass
        return _linux_path_from_win_rest(rest)
    return Path(raw).expanduser()


def _linux_path_from_win_rest(rest: str) -> Path:
    """``vd/yaver`` / ``vd/t`` from a ``C:\\…`` path when ``/mnt/c`` is absent."""
    low = (rest or "").strip("/").lower()
    if low in {"vd/yaver", "vd/yaver/"}:
        return default_linux_data_dir()
    if low in {"vd/t", "vd/t/"}:
        return default_linux_temp_dir()
    if low.startswith("vd/"):
        preferred = Path("/") / rest
        if _dir_usable(preferred):
            return preferred
        return Path.home() / rest
    return Path("/") / rest if rest else default_linux_data_dir()


def _dir_usable(path: Path) -> bool:
    """True when ``path`` exists and is writable, or can be created."""
    try:
        if path.exists():
            return path.is_dir() and os.access(path, os.W_OK)
        cur = path.parent
        while not cur.exists() and cur != cur.parent:
            cur = cur.parent
        return cur.is_dir() and os.access(cur, os.W_OK)
    except OSError:
        return False


def default_windows_base_dir() -> Path:
    """``%LOCALAPPDATA%\\Yaver``. Falls back to the profile AppData path."""
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "Yaver"
    return Path.home() / "AppData" / "Local" / "Yaver"


def default_linux_base_dir() -> Path:
    """``$XDG_DATA_HOME/yaver`` or ``~/.local/share/yaver``."""
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    if xdg:
        return Path(xdg).expanduser() / "yaver"
    return Path.home() / ".local" / "share" / "yaver"


def linux_home_data_dir() -> Path:
    return default_linux_base_dir() / _DATA_FOLDER


def linux_home_temp_dir() -> Path:
    return default_linux_base_dir() / _TEMP_FOLDER


def default_linux_data_dir() -> Path:
    """``~/.local/share/yaver/yaver`` unless ``XDG_DATA_HOME`` is set."""
    return default_linux_base_dir() / _DATA_FOLDER


def default_linux_temp_dir() -> Path:
    """``~/.local/share/yaver/t`` unless ``XDG_DATA_HOME`` is set."""
    return default_linux_base_dir() / _TEMP_FOLDER


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


def default_base_dir() -> Path:
    """Parent of ``yaver`` and ``t`` when ``YAVER_BASE_DIR`` is not set."""
    if os.name == "nt":
        return default_windows_base_dir()
    return default_linux_base_dir()


def default_data_dir() -> Path:
    if _under_pytest():
        return Path.cwd() / _LEGACY_NAME
    return default_base_dir() / _DATA_FOLDER


def default_temp_dir() -> Path:
    if _under_pytest():
        return Path(".temp")
    return default_base_dir() / _TEMP_FOLDER


def configured_base_dir() -> Optional[Path]:
    """The one folder from ``YAVER_BASE_DIR``, or the shared parent of the legacy pair.

    Returns None when data and clones were pointed at unrelated paths, or
    when tests left both variables unset (pytest defaults are not ``yaver``/``t``).
    """
    base = _env_path("YAVER_BASE_DIR")
    if base is not None:
        return base
    data = _env_path("YAVER_DATA_DIR", "VD_DATA_DIR")
    temp = _env_path("TEMP_DIR_BASE")
    if data is not None and temp is not None:
        try:
            same_parent = data.parent.resolve() == temp.parent.resolve()
        except OSError:
            same_parent = data.parent == temp.parent
        if (
            data.name.lower() == _DATA_FOLDER
            and temp.name.lower() == _TEMP_FOLDER
            and same_parent
        ):
            return data.parent
    if data is None and temp is None and not _under_pytest():
        return default_base_dir()
    return None


def resolve_temp_dir_base(raw: Path | str | None = None) -> Path:
    """Absolute temp-clone root (relative values are against process cwd)."""
    if raw is None:
        from src.config import settings

        raw = getattr(settings, "temp_dir_base", None) or default_temp_dir()
    p = coerce_win_path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def agent_data_dir() -> Path:
    """``{YAVER_BASE_DIR}/yaver``. An explicit ``YAVER_DATA_DIR`` still wins."""
    override = _env_path("YAVER_DATA_DIR", "VD_DATA_DIR")
    if override is not None:
        return override
    base = _env_path("YAVER_BASE_DIR")
    if base is not None:
        return base / _DATA_FOLDER
    return default_data_dir()


def agent_temp_dir() -> Path:
    """``{YAVER_BASE_DIR}/t``. An explicit absolute ``TEMP_DIR_BASE`` still wins."""
    override = _env_path("TEMP_DIR_BASE")
    if override is not None and override.is_absolute():
        return override
    base = _env_path("YAVER_BASE_DIR")
    if base is not None:
        return base / _TEMP_FOLDER
    return default_temp_dir()


def plans_dir() -> Path:
    """Durable plan files: ``{YAVER_DATA_DIR}/plans/{ISSUE_KEY}.md``."""
    return agent_data_dir() / "plans"


def logs_dir() -> Path:
    """Daemon logs: ``{YAVER_DATA_DIR}/logs``."""
    return agent_data_dir() / "logs"


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
        (dest / "plans").mkdir(parents=True, exist_ok=True)
        (dest / "logs").mkdir(parents=True, exist_ok=True)
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
