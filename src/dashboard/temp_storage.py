"""Temp-clone disk usage and force-delete for the ops dashboard."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.config import settings
from src.logger import logger
from src.temp_fs import (
    disk_usage_for,
    force_rmtree,
    format_bytes,
    volume_label,
)


class TempStorageError(Exception):
    """User-facing storage operation failure."""

    def __init__(self, message: str, *, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def resolve_temp_base() -> Path:
    """Absolute ``TEMP_DIR_BASE`` (relative paths are against process cwd)."""
    raw = getattr(settings, "temp_dir_base", None) or Path(".temp")
    base = Path(raw)
    if not base.is_absolute():
        base = Path.cwd() / base
    try:
        return base.resolve()
    except OSError:
        return base.absolute()


def _safe_child(base: Path, name: str) -> Path:
    folder = (name or "").strip()
    if not folder or folder in {".", ".."}:
        raise TempStorageError("Folder name is required")
    if "/" in folder or "\\" in folder or "\x00" in folder:
        raise TempStorageError("Folder name must be a single directory")
    if folder.startswith("."):
        # Allow .git-looking clones but reject path tricks
        if folder in {".", ".."} or folder.startswith(".."):
            raise TempStorageError("Invalid folder name")
    candidate = (base / folder)
    try:
        resolved = candidate.resolve()
        resolved.relative_to(base.resolve())
    except (ValueError, OSError) as e:
        raise TempStorageError("Folder is outside the temp base") from e
    if resolved == base.resolve():
        raise TempStorageError("Refusing to delete the temp base itself")
    return resolved


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for name in files:
                fp = os.path.join(root, name)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _in_use_paths() -> Set[Path]:
    found: Set[Path] = set()
    try:
        from src.git_manager import session_bound_workspace_paths

        for p in session_bound_workspace_paths():
            try:
                found.add(Path(p).resolve())
            except (OSError, TypeError):
                continue
    except Exception:
        pass
    try:
        from src.git_manager import GitManager

        live = getattr(GitManager, "_live_by_issue", None) or {}
        for gm in list(live.values()):
            td = getattr(gm, "temp_dir", None)
            if td:
                try:
                    found.add(Path(td).resolve())
                except OSError:
                    continue
    except Exception:
        pass
    return found


def build_storage_view() -> Dict[str, Any]:
    base = resolve_temp_base()
    try:
        usage = disk_usage_for(base if base.exists() else base.parent)
    except OSError as e:
        logger.warning(f"disk_usage failed for {base}: {e}")
        raise TempStorageError(f"Could not read disk usage: {e}", status_code=500)
    volume = volume_label(base if base.exists() else base.parent)
    used_pct = 0.0
    if usage.total > 0:
        used_pct = round(100.0 * float(usage.used) / float(usage.total), 1)
    in_use = _in_use_paths()
    folders: List[Dict[str, Any]] = []
    folders_bytes = 0
    if base.is_dir():
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            logger.warning(f"Cannot list temp base {base}: {e}")
            entries = []
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                resolved = entry
            size = _dir_size_bytes(entry)
            folders_bytes += size
            try:
                mtime = entry.stat().st_mtime
                modified = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except OSError:
                modified = None
            folders.append(
                {
                    "name": entry.name,
                    "path": str(resolved),
                    "size_bytes": size,
                    "size_label": format_bytes(size),
                    "modified_at": modified,
                    "in_use": resolved in in_use,
                }
            )
    return {
        "disk": {
            "volume": volume,
            "path": str(base),
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
            "total_label": format_bytes(int(usage.total)),
            "used_label": format_bytes(int(usage.used)),
            "free_label": format_bytes(int(usage.free)),
            "used_percent": used_pct,
        },
        "folders": folders,
        "folder_count": len(folders),
        "folders_bytes": folders_bytes,
        "folders_label": format_bytes(folders_bytes),
    }


def force_delete_temp_folder(name: str) -> Dict[str, Any]:
    """Hard-delete one clone directory under the temp base."""
    base = resolve_temp_base()
    target = _safe_child(base, name)
    if not target.exists():
        raise TempStorageError(f"Folder not found: {name}", status_code=404)
    if not target.is_dir():
        raise TempStorageError("Only directories can be deleted", status_code=400)
    try:
        force_rmtree(target)
    except OSError as e:
        logger.warning(f"Force delete failed for {target}: {e}")
        raise TempStorageError(
            f"Could not force-delete {name}: {e}", status_code=500
        ) from e
    if target.exists():
        raise TempStorageError(
            f"Force delete left remnants in {name}", status_code=500
        )
    logger.info(f"Dashboard force-deleted temp folder {target}")
    return {"ok": True, "name": name, "path": str(target)}
