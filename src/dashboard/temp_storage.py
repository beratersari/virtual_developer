"""Temp-clone disk usage and force-delete for the ops dashboard."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

from src.config import settings
from src.logger import logger
from src.temp_fs import (
    disk_usage_for,
    force_rmtree_progress,
    format_bytes,
    volume_label,
)

# In-flight / recent force-deletes (name → job). Process-local; lost on restart.
_jobs_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_DONE_KEEP_SECONDS = 2.0

# Folder sizes are walked off the request path — os.walk of git clones on
# Windows/WSL (drvfs) can take tens of seconds and used to block GET /api/storage.
_size_lock = threading.Lock()
_size_cache: Dict[str, Dict[str, Any]] = {}
_scan_lock = threading.Lock()
_scan_wanted = False
_scan_thread: threading.Thread | None = None


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


def reset_delete_jobs() -> None:
    """Test helper: drop in-memory delete jobs."""
    with _jobs_lock:
        _jobs.clear()


def reset_size_cache() -> None:
    """Test helper: drop cached folder sizes and stop a pending rescan flag."""
    global _scan_wanted
    with _size_lock:
        _size_cache.clear()
    with _scan_lock:
        _scan_wanted = False


def _cached_size(name: str, mtime: float | None) -> int | None:
    with _size_lock:
        row = _size_cache.get(name)
    if not row:
        return None
    if mtime is not None and row.get("mtime") != mtime:
        return None
    try:
        return int(row.get("bytes") or 0)
    except (TypeError, ValueError):
        return None


def _store_size(name: str, size: int, mtime: float | None) -> None:
    with _size_lock:
        _size_cache[name] = {"bytes": int(size), "mtime": mtime}


def _drop_size(name: str) -> None:
    with _size_lock:
        _size_cache.pop(name, None)


def _ensure_size_scan() -> None:
    global _scan_wanted, _scan_thread
    with _scan_lock:
        _scan_wanted = True
        alive = _scan_thread is not None and _scan_thread.is_alive()
        if alive:
            return
        _scan_thread = threading.Thread(
            target=_size_scan_loop, name="temp-size-scan", daemon=True
        )
        _scan_thread.start()


def _size_scan_loop() -> None:
    global _scan_wanted
    while True:
        with _scan_lock:
            if not _scan_wanted:
                return
            _scan_wanted = False
        try:
            _scan_folder_sizes_once()
        except Exception as e:
            logger.warning(f"temp folder size scan failed: {e}")


def _scan_folder_sizes_once() -> None:
    base = resolve_temp_base()
    if not base.is_dir():
        return
    jobs = list_delete_jobs()
    try:
        entries = list(base.iterdir())
    except OSError as e:
        logger.warning(f"Cannot list temp base {base} for size scan: {e}")
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        job = jobs.get(entry.name)
        if job and job.get("status") == "deleting":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            mtime = None
        if _cached_size(entry.name, mtime) is not None:
            continue
        size = _dir_size_bytes(entry)
        _store_size(entry.name, size, mtime)


def scan_folder_sizes_now() -> None:
    """Synchronous size walk (tests)."""
    _scan_folder_sizes_once()


def list_delete_jobs() -> Dict[str, Dict[str, Any]]:
    with _jobs_lock:
        return {k: dict(v) for k, v in _jobs.items()}


def list_delete_dtos() -> List[Dict[str, Any]]:
    """Cheap progress snapshot — no disk walk."""
    out: List[Dict[str, Any]] = []
    for name, job in list_delete_jobs().items():
        row = _delete_dto(job)
        row["name"] = name
        row["path"] = job.get("path")
        out.append(row)
    out.sort(key=lambda r: str(r.get("name") or "").lower())
    return out


def _set_job(name: str, **fields: Any) -> None:
    with _jobs_lock:
        cur = _jobs.get(name) or {"name": name}
        cur.update(fields)
        _jobs[name] = cur


def _pop_job(name: str) -> None:
    with _jobs_lock:
        _jobs.pop(name, None)


def _delete_dto(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": job.get("status") or "deleting",
        "percent": int(job.get("percent") or 0),
        "error": job.get("error"),
    }


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
    jobs = list_delete_jobs()
    folders: List[Dict[str, Any]] = []
    folders_bytes = 0
    sizes_pending = False
    listed: Set[str] = set()
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
            job = jobs.get(entry.name)
            deleting = bool(job and job.get("status") == "deleting")
            try:
                mtime = entry.stat().st_mtime
                modified = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except OSError:
                mtime = None
                modified = None
            if deleting:
                size = int(job.get("size_bytes") or 0) if job else 0
                size_pending = False
            else:
                cached = _cached_size(entry.name, mtime)
                if cached is None:
                    size = 0
                    size_pending = True
                    sizes_pending = True
                else:
                    size = cached
                    size_pending = False
            folders_bytes += size
            row: Dict[str, Any] = {
                "name": entry.name,
                "path": str(resolved),
                "size_bytes": size,
                "size_label": None if size_pending else format_bytes(size),
                "size_pending": size_pending,
                "modified_at": modified,
                "in_use": resolved in in_use,
            }
            if job:
                if job.get("status") == "done":
                    _pop_job(entry.name)
                else:
                    row["delete"] = _delete_dto(job)
            folders.append(row)
            listed.add(entry.name)
    for name, job in jobs.items():
        if name in listed:
            continue
        if job.get("status") not in {"deleting", "error", "done"}:
            continue
        folders.append(
            {
                "name": name,
                "path": str(job.get("path") or (base / name)),
                "size_bytes": int(job.get("size_bytes") or 0),
                "size_label": format_bytes(int(job.get("size_bytes") or 0)),
                "size_pending": False,
                "modified_at": None,
                "in_use": False,
                "delete": _delete_dto(job),
            }
        )
    folders.sort(key=lambda r: str(r.get("name") or "").lower())
    if sizes_pending:
        _ensure_size_scan()
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
        "sizes_pending": sizes_pending,
    }


def _validate_delete_target(name: str) -> Path:
    base = resolve_temp_base()
    target = _safe_child(base, name)
    if not target.exists():
        raise TempStorageError(f"Folder not found: {name}", status_code=404)
    if not target.is_dir():
        raise TempStorageError("Only directories can be deleted", status_code=400)
    return target


def force_delete_temp_folder(name: str) -> Dict[str, Any]:
    """Synchronous hard-delete (tests / callers that wait)."""
    target = _validate_delete_target(name)
    try:
        force_rmtree_progress(target)
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


def queue_delete_temp_folder(name: str) -> Dict[str, Any]:
    """Start a background force-delete and return immediately."""
    target = _validate_delete_target(name)
    with _jobs_lock:
        existing = _jobs.get(name)
        if existing and existing.get("status") == "deleting":
            raise TempStorageError(
                f"Delete already in progress for {name}", status_code=409
            )
        size_bytes = int((existing or {}).get("size_bytes") or 0)
        _jobs[name] = {
            "name": name,
            "path": str(target),
            "status": "deleting",
            "percent": 0,
            "error": None,
            "size_bytes": size_bytes,
        }
    worker = threading.Thread(
        target=_run_delete_job,
        args=(name, target),
        name=f"temp-del-{name}",
        daemon=True,
    )
    worker.start()
    logger.info(f"Dashboard queued force-delete of temp folder {target}")
    return {
        "ok": True,
        "accepted": True,
        "name": name,
        "path": str(target),
        "status": "deleting",
        "percent": 0,
    }


def _run_delete_job(name: str, target: Path) -> None:
    last_emit = [0.0]
    last_pct = [-1]

    def on_progress(done: int, total: int) -> None:
        if total <= 0:
            pct = 0
        else:
            pct = min(99, int(100.0 * float(done) / float(total)))
        now = time.monotonic()
        if pct != last_pct[0] and (pct >= 99 or now - last_emit[0] >= 0.05):
            last_emit[0] = now
            last_pct[0] = pct
            _set_job(name, percent=pct, status="deleting")

    try:
        force_rmtree_progress(target, on_progress=on_progress)
        if target.exists():
            raise OSError(f"force delete left remnants at {target}")
        _set_job(name, percent=100, status="done", error=None)
        _drop_size(name)
        logger.info(f"Dashboard force-deleted temp folder {target}")
        timer = threading.Timer(_DONE_KEEP_SECONDS, lambda: _pop_job(name))
        timer.daemon = True
        timer.start()
    except Exception as e:
        logger.warning(f"Force delete failed for {target}: {e}")
        _set_job(name, status="error", error=str(e))
