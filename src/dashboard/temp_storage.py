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
    from src.paths import resolve_temp_dir_base

    return resolve_temp_dir_base(getattr(settings, "temp_dir_base", None))


def resolve_sessions_dir() -> Path:
    """Absolute session-log directory under the durable data dir."""
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("sessions")


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
    for key, job in list_delete_jobs().items():
        row = _delete_dto(job)
        row["name"] = job.get("name") or key
        row["area"] = job.get("area") or ("sessions" if str(key).startswith("sessions:") else "temp")
        row["path"] = job.get("path")
        out.append(row)
    out.sort(key=lambda r: (str(r.get("area") or ""), str(r.get("name") or "").lower()))
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


def _path_lookup_keys(raw: Any) -> List[str]:
    """Stable keys so Windows/WSL path spellings still match a clone folder."""
    if raw is None:
        return []
    text = str(raw).strip()
    if not text:
        return []
    keys: List[str] = []
    name = Path(text.replace("\\", "/")).name
    if name:
        keys.append(name.lower())
    try:
        resolved = Path(text).resolve()
        keys.append(str(resolved).replace("\\", "/").lower())
        if resolved.name:
            keys.append(resolved.name.lower())
    except (OSError, RuntimeError):
        keys.append(text.replace("\\", "/").lower())
    out: List[str] = []
    seen: Set[str] = set()
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _put_clone_issue(
    index: Dict[str, Dict[str, Any]],
    path: Any,
    *,
    issue_key: str,
    summary: str = "",
    job_id: str = "",
    merge_request_url: str = "",
    merge_request_state: str = "",
    when: str = "",
    prefer: bool = False,
) -> None:
    key = (issue_key or "").strip().upper()
    if not key:
        return
    rec = {
        "issue_key": key,
        "summary": (summary or "").strip(),
        "job_id": (job_id or "").strip() or None,
        "merge_request_url": (merge_request_url or "").strip() or None,
        "merge_request_state": (merge_request_state or "").strip() or None,
        "_when": (when or "").strip(),
    }
    for lookup in _path_lookup_keys(path):
        prev = index.get(lookup)
        newer = bool(
            prev is not None
            and rec["_when"]
            and rec["_when"] > str(prev.get("_when") or "")
        )
        if prev is None or prefer or newer:
            if prev:
                if not rec["summary"]:
                    rec["summary"] = prev.get("summary") or ""
                if not rec["job_id"]:
                    rec["job_id"] = prev.get("job_id")
                if not rec["merge_request_url"]:
                    rec["merge_request_url"] = prev.get("merge_request_url")
                if not rec["merge_request_state"]:
                    rec["merge_request_state"] = prev.get("merge_request_state")
                if not rec["_when"]:
                    rec["_when"] = prev.get("_when") or ""
            index[lookup] = rec
            continue
        if not prev.get("summary") and rec["summary"]:
            prev["summary"] = rec["summary"]
        if not prev.get("job_id") and rec["job_id"]:
            prev["job_id"] = rec["job_id"]
        if not prev.get("merge_request_url") and rec["merge_request_url"]:
            prev["merge_request_url"] = rec["merge_request_url"]
        if not prev.get("merge_request_state") and rec["merge_request_state"]:
            prev["merge_request_state"] = rec["merge_request_state"]
        if not prev.get("_when") and rec["_when"]:
            prev["_when"] = rec["_when"]


def _fill_missing_summaries(index: Dict[str, Dict[str, Any]]) -> None:
    """Best-effort title fill from issue state — only keys still missing a title."""
    need: Set[str] = set()
    for rec in index.values():
        ik = (rec.get("issue_key") or "").strip().upper()
        if ik and not (rec.get("summary") or "").strip():
            need.add(ik)
    if not need:
        return
    try:
        from src.state.manager import JiraStateManager

        sm = JiraStateManager()
        for ik in need:
            st = sm.get_state(ik)
            title = ((st.issue_summary if st else "") or "").strip()
            if not title:
                continue
            for rec in index.values():
                if (rec.get("issue_key") or "").strip().upper() == ik and not rec.get(
                    "summary"
                ):
                    rec["summary"] = title
    except Exception as e:
        logger.debug(f"storage issue index from state failed: {e}")


def _clone_issue_index() -> Dict[str, Dict[str, Any]]:
    """Map clone path / folder name → latest Jira key, title, and job id."""
    index: Dict[str, Dict[str, Any]] = {}

    try:
        from src.state.job_store import job_store

        n = job_store.count_jobs()
        for job in job_store.list_jobs(limit=max(int(n or 0), 1)):
            wd = (job.get("working_directory") or "").strip()
            if not wd:
                continue
            _put_clone_issue(
                index,
                wd,
                issue_key=str(job.get("issue_key") or ""),
                summary=str(job.get("summary") or ""),
                job_id=str(job.get("job_id") or ""),
                merge_request_url=str(job.get("merge_request_url") or ""),
                merge_request_state=str(job.get("merge_request_state") or ""),
                when=str(
                    job.get("started_at") or job.get("updated_at") or ""
                ),
            )
    except Exception as e:
        logger.debug(f"storage issue index from jobs failed: {e}")

    try:
        from src.state.session_bind_store import session_bind_store

        for rec in session_bind_store.list_binds(limit=500):
            wd = (rec.get("working_directory") or "").strip()
            if not wd:
                continue
            _put_clone_issue(
                index,
                wd,
                issue_key=str(rec.get("issue_key") or ""),
                summary="",
                job_id=str(rec.get("job_id") or ""),
            )
    except Exception as e:
        logger.debug(f"storage issue index from binds failed: {e}")

    try:
        from src.git_manager import GitManager

        live = getattr(GitManager, "_live_by_issue", None) or {}
        for ik, gm in list(live.items()):
            td = getattr(gm, "temp_dir", None)
            if not td:
                continue
            _put_clone_issue(
                index,
                td,
                issue_key=str(ik or ""),
                summary="",
                prefer=True,
            )
    except Exception as e:
        logger.debug(f"storage issue index from live git failed: {e}")

    _fill_missing_summaries(index)
    _fill_missing_mr(index)
    return index


def _fill_missing_mr(index: Dict[str, Dict[str, Any]]) -> None:
    """Fill MR url/state from issue metadata when the job row has none."""
    need: Set[str] = set()
    for rec in index.values():
        if rec.get("issue_key") and not rec.get("merge_request_url"):
            need.add(str(rec["issue_key"]).upper())
    if not need:
        return
    try:
        from src.state.manager import JiraStateManager

        sm = JiraStateManager()
        for ik in need:
            st = sm.get_state(ik)
            meta = (st.metadata or {}) if st else {}
            url = str(meta.get("merge_request_url") or "").strip()
            state = str(meta.get("merge_request_state") or "").strip()
            if not url and not state:
                continue
            for rec in index.values():
                if (rec.get("issue_key") or "").strip().upper() != ik:
                    continue
                if url and not rec.get("merge_request_url"):
                    rec["merge_request_url"] = url
                if state and not rec.get("merge_request_state"):
                    rec["merge_request_state"] = state
    except Exception as e:
        logger.debug(f"storage MR fill from state failed: {e}")


def _issue_fields_for(
    path: Any, name: str, index: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    for lookup in _path_lookup_keys(path) + _path_lookup_keys(name):
        hit = index.get(lookup)
        if hit:
            return {
                "issue_key": hit.get("issue_key"),
                "summary": hit.get("summary") or "",
                "job_id": hit.get("job_id"),
                "merge_request_url": hit.get("merge_request_url"),
                "merge_request_state": hit.get("merge_request_state"),
            }
    return {
        "issue_key": None,
        "summary": "",
        "job_id": None,
        "merge_request_url": None,
        "merge_request_state": None,
    }


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
    issue_index = _clone_issue_index()
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
                **_issue_fields_for(resolved, entry.name, issue_index),
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
        gone_path = job.get("path") or (base / name)
        folders.append(
            {
                "name": name,
                "path": str(gone_path),
                "size_bytes": int(job.get("size_bytes") or 0),
                "size_label": format_bytes(int(job.get("size_bytes") or 0)),
                "size_pending": False,
                "modified_at": None,
                "in_use": False,
                "delete": _delete_dto(job),
                **_issue_fields_for(gone_path, name, issue_index),
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
        "temp_dir": str(base),
        "folders": folders,
        "folder_count": len(folders),
        "folders_bytes": folders_bytes,
        "folders_label": format_bytes(folders_bytes),
        "sizes_pending": sizes_pending,
    }


def _list_session_files(*, limit: int = 400) -> List[Dict[str, Any]]:
    """Newest session/prompt files first (flat ``YAVER_DATA_DIR/sessions``)."""
    root = resolve_sessions_dir()
    out: List[Dict[str, Any]] = []
    if not root.is_dir():
        return out
    try:
        entries = list(root.iterdir())
    except OSError as e:
        logger.warning(f"Cannot list sessions dir {root}: {e}")
        return out
    rows: List[tuple] = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            st = entry.stat()
        except OSError:
            continue
        rows.append((st.st_mtime, entry, st.st_size))
    rows.sort(key=lambda r: r[0], reverse=True)
    for mtime, entry, size in rows[: max(1, int(limit))]:
        try:
            modified = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (OSError, OverflowError, ValueError):
            modified = None
        out.append(
            {
                "name": entry.name,
                "path": str(entry),
                "size_bytes": int(size),
                "size_label": format_bytes(int(size)),
                "size_pending": False,
                "modified_at": modified,
                "in_use": False,
                "kind": "file",
                "area": "sessions",
            }
        )
    return out


def _validate_delete_target(name: str, *, area: str = "temp") -> Path:
    kind = (area or "temp").strip().lower() or "temp"
    if kind != "temp":
        raise TempStorageError("Storage delete is only for temp clones", status_code=400)
    base = resolve_temp_base()
    target = _safe_child(base, name)
    if not target.exists():
        raise TempStorageError(f"Folder not found: {name}", status_code=404)
    if not target.is_dir():
        raise TempStorageError("Only directories can be deleted", status_code=400)
    return target


def _validate_session_target(name: str) -> Path:
    base = resolve_sessions_dir()
    try:
        base_res = base.resolve()
    except OSError:
        base_res = base
    target = _safe_child(base, name)
    if not target.exists():
        raise TempStorageError(f"Session file not found: {name}", status_code=404)
    if not target.is_file():
        raise TempStorageError("Only session files can be deleted here", status_code=400)
    try:
        target.resolve().relative_to(base_res)
    except (ValueError, OSError) as e:
        raise TempStorageError("Session file is outside the sessions dir") from e
    return target


def force_delete_temp_folder(name: str, *, area: str = "temp") -> Dict[str, Any]:
    """Synchronous hard-delete (tests / callers that wait)."""
    kind = (area or "temp").strip().lower() or "temp"
    target = _validate_delete_target(name, area=kind)
    try:
        if kind == "sessions":
            _delete_session_file(target)
        else:
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
    logger.info(f"Dashboard force-deleted {kind} {target}")
    return {"ok": True, "name": name, "path": str(target), "area": kind}


def _norm_mr_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def clone_folder_names_for_mr(
    *,
    mr_url: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    issue_key: str = "",
    source_branch: str = "",
) -> List[str]:
    """Temp-clone folder names tied to this merge request."""
    names: List[str] = []
    seen: Set[str] = set()

    def _add(path: Any) -> None:
        text = str(path or "").strip()
        if not text:
            return
        name = Path(text.replace("\\", "/")).name
        if not name or name in seen:
            return
        seen.add(name)
        names.append(name)

    want_url = _norm_mr_url(mr_url)
    want_path = (project_path or "").strip().lower()
    want_key = (issue_key or "").strip().upper()
    want_branch = (source_branch or "").strip()

    try:
        from src.state.job_store import job_store

        n = job_store.count_jobs()
        for job in job_store.list_jobs(limit=max(int(n or 0), 1)):
            job_url = _norm_mr_url(str(job.get("merge_request_url") or ""))
            same_url = bool(want_url and job_url == want_url)
            same_iid = (
                int(job.get("gitlab_mr_iid") or 0) == int(mr_iid or 0)
                and int(mr_iid or 0) > 0
                and (
                    not want_path
                    or str(job.get("gitlab_project") or "").strip().lower()
                    == want_path
                )
            )
            same_issue = bool(want_key) and str(job.get("issue_key") or "").upper() == want_key
            if same_url or same_iid or (same_issue and (same_url or not want_url)):
                _add(job.get("working_directory"))
    except Exception as e:
        logger.debug(f"MR clone lookup from jobs failed: {e}")

    try:
        from src.state.session_bind_store import session_bind_store

        for rec in session_bind_store.list_binds(limit=500):
            if want_key and str(rec.get("issue_key") or "").upper() == want_key:
                _add(rec.get("working_directory"))
                continue
            if want_branch and str(rec.get("branch") or "").strip() == want_branch:
                _add(rec.get("working_directory"))
    except Exception as e:
        logger.debug(f"MR clone lookup from binds failed: {e}")

    if want_key:
        try:
            from src.git_manager import GitManager

            live = getattr(GitManager, "_live_by_issue", None) or {}
            gm = live.get(want_key) or live.get(issue_key)
            if gm is not None:
                _add(getattr(gm, "temp_dir", None))
        except Exception as e:
            logger.debug(f"MR clone lookup from live git failed: {e}")
    return names


def delete_clones_for_merge_request(
    *,
    mr_url: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    issue_key: str = "",
    source_branch: str = "",
) -> List[str]:
    """Queue force-delete of temp clones for a merged MR. Skips in-use folders."""
    names = clone_folder_names_for_mr(
        mr_url=mr_url,
        project_path=project_path,
        mr_iid=mr_iid,
        issue_key=issue_key,
        source_branch=source_branch,
    )
    deleted: List[str] = []
    in_use = _in_use_paths()
    for name in names:
        try:
            target = _validate_delete_target(name, area="temp")
        except TempStorageError as e:
            logger.info(f"Skip MR-merge delete of {name}: {e}")
            continue
        try:
            resolved = target.resolve()
        except OSError:
            resolved = target
        if resolved in in_use:
            logger.info(f"Skip MR-merge delete of {name}: clone is in use")
            continue
        try:
            queue_delete_temp_folder(name, area="temp")
            deleted.append(name)
        except TempStorageError as e:
            logger.warning(f"Could not queue MR-merge delete of {name}: {e}")
    return deleted


def queue_delete_temp_folder(name: str, *, area: str = "temp") -> Dict[str, Any]:
    """Start a background force-delete and return immediately."""
    kind = (area or "temp").strip().lower() or "temp"
    target = _validate_delete_target(name, area=kind)
    job_key = name if kind == "temp" else f"sessions:{name}"
    with _jobs_lock:
        existing = _jobs.get(job_key)
        if existing and existing.get("status") == "deleting":
            raise TempStorageError(
                f"Delete already in progress for {name}", status_code=409
            )
        size_bytes = int((existing or {}).get("size_bytes") or 0)
        _jobs[job_key] = {
            "name": name,
            "area": kind,
            "path": str(target),
            "status": "deleting",
            "percent": 0,
            "error": None,
            "size_bytes": size_bytes,
        }
    worker = threading.Thread(
        target=_run_delete_job,
        args=(job_key, target, kind),
        name=f"stor-del-{job_key}",
        daemon=True,
    )
    worker.start()
    logger.info(f"Dashboard queued force-delete of {kind} {target}")
    return {
        "ok": True,
        "accepted": True,
        "name": name,
        "area": kind,
        "path": str(target),
        "status": "deleting",
        "percent": 0,
    }


def _delete_session_file(target: Path) -> None:
    """Unlink one session artifact (log / prompt / sid file)."""
    try:
        target.unlink()
    except FileNotFoundError:
        return


def _run_delete_job(name: str, target: Path, area: str = "temp") -> None:
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
        if area == "sessions":
            _delete_session_file(target)
            on_progress(1, 1)
        else:
            force_rmtree_progress(target, on_progress=on_progress)
        if target.exists():
            raise OSError(f"force delete left remnants at {target}")
        _set_job(name, percent=100, status="done", error=None)
        _drop_size(name)
        logger.info(f"Dashboard force-deleted {area} {target}")
        timer = threading.Timer(_DONE_KEEP_SECONDS, lambda: _pop_job(name))
        timer.daemon = True
        timer.start()
    except Exception as e:
        logger.warning(f"Force delete failed for {target}: {e}")
        _set_job(name, status="error", error=str(e))
