"""Temp-clone disk usage and force-delete for the ops dashboard."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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

# Live GitLab MR state for Storage (old jobs often have a URL, no status).
_mr_state_lock = threading.Lock()
_mr_state_cache: Dict[str, str] = {}
_mr_scan_lock = threading.Lock()
_mr_scan_wanted = False
_mr_scan_thread: threading.Thread | None = None

# Folder→issue map walks every job + bind. Cache so GET /api/storage (and
# overlapping Storage polls) do not rescan the store on every click.
_index_cache_lock = threading.Lock()
_index_cache: Optional[Dict[str, Dict[str, Any]]] = None
_index_cache_at = 0.0
_INDEX_CACHE_SECONDS = 1.5


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
    global _scan_wanted, _index_cache, _index_cache_at
    with _size_lock:
        _size_cache.clear()
    with _scan_lock:
        _scan_wanted = False
    with _index_cache_lock:
        _index_cache = None
        _index_cache_at = 0.0
    reset_mr_state_cache()


def reset_mr_state_cache() -> None:
    """Drop cached GitLab MR states so Storage Refresh fetches them again."""
    global _mr_scan_wanted
    with _mr_state_lock:
        _mr_state_cache.clear()
    with _mr_scan_lock:
        _mr_scan_wanted = False


def _cached_mr_state(url: str) -> Optional[str]:
    key = _norm_mr_url(url)
    if not key:
        return None
    with _mr_state_lock:
        return _mr_state_cache.get(key)


def remember_mr_state(url: str, state: str) -> None:
    """Record a live GitLab MR state for Storage rows that share this URL."""
    key = _norm_mr_url(url)
    st = (state or "").strip().lower()
    if not key or not st:
        return
    with _mr_state_lock:
        _mr_state_cache[key] = st


def _ensure_mr_state_scan() -> None:
    global _mr_scan_wanted, _mr_scan_thread
    with _mr_scan_lock:
        _mr_scan_wanted = True
        alive = _mr_scan_thread is not None and _mr_scan_thread.is_alive()
        if alive:
            return
        _mr_scan_thread = threading.Thread(
            target=_mr_state_scan_loop, name="temp-mr-state-scan", daemon=True
        )
        _mr_scan_thread.start()


def _mr_state_scan_loop() -> None:
    global _mr_scan_wanted
    while True:
        with _mr_scan_lock:
            if not _mr_scan_wanted:
                return
            _mr_scan_wanted = False
        try:
            _scan_mr_states_once()
        except Exception as e:
            logger.warning(f"Storage MR status scan failed: {e}")


def _azure_pr_state_from_status(status: str) -> str:
    raw = (status or "").strip().lower()
    if raw in {"active", "opened", "open"}:
        return "open"
    if raw in {"completed", "merged"}:
        return "completed"
    if raw in {"abandoned", "closed"}:
        return "abandoned"
    return raw or "unknown"


def _lookup_review_state(url: str) -> str:
    """Live GitLab MR or Azure PR status for a Storage review URL."""
    from src.gitlab.client import GitlabClient, parse_merge_request_url

    parsed_gl = parse_merge_request_url(url)
    if parsed_gl:
        host, project, iid = parsed_gl
        info = GitlabClient(host=host).get_merge_request(project, iid)
        if not info:
            return "unknown"
        return str(info.get("state") or "").strip().lower() or "unknown"

    from src.azure.client import AzureDevOpsClient
    from src.azure.webhook import parse_azure_git_url, parse_pull_request_url

    parsed_az = parse_pull_request_url(url)
    if not parsed_az:
        return "unknown"
    _host, _path, pr_id = parsed_az
    git_url = re.sub(
        r"/(?:pullrequest|pullRequest)/\d+/?$", "", url, flags=re.IGNORECASE
    )
    parsed = parse_azure_git_url(git_url)
    if not parsed:
        return "unknown"
    info = AzureDevOpsClient(
        host=str(parsed.get("host") or ""),
        collection_url=str(parsed.get("collection_url") or ""),
    ).get_pull_request(
        str(parsed.get("project") or ""),
        str(parsed.get("repository") or ""),
        pr_id,
    )
    if not info:
        return "unknown"
    return _azure_pr_state_from_status(str(info.get("status") or ""))


def _scan_mr_states_once() -> None:
    urls: List[str] = []
    seen: Set[str] = set()
    try:
        for rec in _clone_issue_index().values():
            url = str(rec.get("merge_request_url") or "").strip()
            key = _norm_mr_url(url)
            if not key or key in seen:
                continue
            seen.add(key)
            urls.append(url)
    except Exception as e:
        logger.debug(f"Storage MR index failed: {e}")
        return
    for url in urls:
        if _cached_mr_state(url):
            continue
        try:
            state = _lookup_review_state(url)
        except Exception as e:
            logger.debug(f"Storage review status {url!r} failed: {e}")
            state = "unknown"
        remember_mr_state(url, state)
        if state != "unknown":
            _persist_job_mr_state(url, state)


def _persist_job_mr_state(url: str, state: str) -> None:
    want = _norm_mr_url(url)
    if not want:
        return
    try:
        from src.state.job_store import job_store

        n = job_store.count_jobs()
        for job in job_store.list_jobs(limit=max(int(n or 0), 1)):
            if _norm_mr_url(str(job.get("merge_request_url") or "")) != want:
                continue
            jid = str(job.get("job_id") or "")
            if jid:
                job_store.update_job(jid, merge_request_state=state)
    except Exception as e:
        logger.debug(f"Could not persist MR state for {want}: {e}")


def _apply_live_mr_state(row: Dict[str, Any]) -> bool:
    """Overlay cached GitLab state. Returns True if a fetch is still needed."""
    url = str(row.get("merge_request_url") or "").strip()
    if not url:
        return False
    live = _cached_mr_state(url)
    if live:
        row["merge_request_state"] = live
        return False
    return True


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


def _live_git_paths() -> Set[Path]:
    """Clone dirs owned by an in-flight GitManager (job still running)."""
    found: Set[Path] = set()
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


def _forget_binds_for_clone(clone: Path) -> None:
    """Drop OpenCode resume pointers so a later job does not reuse a deleted dir."""
    try:
        want = Path(clone).resolve()
    except OSError:
        return
    try:
        from src.state.session_bind_store import session_bind_store
    except Exception:
        return
    try:
        recs = session_bind_store.list_binds(limit=500)
    except Exception:
        return
    for rec in recs:
        raw = rec.get("working_directory")
        bid = str(rec.get("bind_id") or "").strip()
        if not bid or not raw:
            continue
        try:
            if Path(str(raw)).resolve() != want:
                continue
        except OSError:
            continue
        try:
            session_bind_store.forget_session(bid, reason="mr-merged")
        except Exception as e:
            logger.debug(f"Could not forget session bind {bid}: {e}")


def _in_use_paths() -> Set[Path]:
    """Clone dirs a live job still owns (same rule as Storage Delete).

    Session binds persist after the job ends so the next run can resume.
    Those must not mark the folder In use — that left Delete disabled forever.
    """
    return _live_git_paths()


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
    global _index_cache, _index_cache_at
    now = time.monotonic()
    with _index_cache_lock:
        cached = _index_cache
        cached_at = _index_cache_at
    if cached is not None and now - cached_at < _INDEX_CACHE_SECONDS:
        return {k: dict(v) for k, v in cached.items()}
    index = _build_clone_issue_index()
    with _index_cache_lock:
        _index_cache = index
        _index_cache_at = time.monotonic()
    return index


def _build_clone_issue_index() -> Dict[str, Dict[str, Any]]:
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
    """Copy review *state* onto a folder that already has that review URL.

    Do not copy the issue's merge_request_url onto every clone for that
    Jira key. A GitLab/Azure comment job for ``feat(KAN-12)`` would
    otherwise paint the Jira KAN-12 plan folder as that PR and sweep
    would delete it.
    """
    try:
        from src.state.manager import JiraStateManager

        sm = JiraStateManager()
        for rec in index.values():
            url = str(rec.get("merge_request_url") or "").strip()
            if not url or rec.get("merge_request_state"):
                continue
            ik = (rec.get("issue_key") or "").strip().upper()
            if not ik:
                continue
            st = sm.get_state(ik)
            meta = (st.metadata or {}) if st else {}
            meta_url = str(meta.get("merge_request_url") or "").strip()
            state = str(meta.get("merge_request_state") or "").strip()
            if state and _same_review_url(url, meta_url):
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
    mr_states_pending = False
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
            if _apply_live_mr_state(row):
                mr_states_pending = True
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
    if mr_states_pending:
        _ensure_mr_state_scan()
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
        "mr_states_pending": mr_states_pending,
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


def _raise_if_clone_in_use(target: Path) -> None:
    """Refuse Storage delete while a live job still owns this clone."""
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if resolved in _live_git_paths():
        raise TempStorageError(
            "Cannot delete a clone while a job is using it; stop the job first",
            status_code=409,
        )


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
    if kind == "temp":
        _raise_if_clone_in_use(target)
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


def _existing_temp_names() -> Set[str]:
    base = resolve_temp_base()
    if not base.is_dir():
        return set()
    try:
        return {p.name for p in base.iterdir() if p.is_dir()}
    except OSError:
        return set()


def _same_review_url(left: str, right: str) -> bool:
    """True when two URLs are the same GitLab MR or Azure PR.

    Azure compares host + project/repo path + id. PR numbers restart per
    repo, so host+id alone would treat ``repoA/pullrequest/4`` as
    ``repoB/pullrequest/4``.
    """
    a = _norm_mr_url(left)
    b = _norm_mr_url(right)
    if a and b and a == b:
        return True
    if not a or not b:
        return False
    from src.gitlab.client import parse_merge_request_url

    ga = parse_merge_request_url(left)
    gb = parse_merge_request_url(right)
    if ga and gb:
        return (
            ga[0] == gb[0]
            and str(ga[1] or "").lower() == str(gb[1] or "").lower()
            and int(ga[2]) == int(gb[2])
        )
    from src.azure.webhook import parse_pull_request_url

    aa = parse_pull_request_url(left)
    ab = parse_pull_request_url(right)
    if aa and ab:
        return (
            aa[0] == ab[0]
            and str(aa[1] or "").lower() == str(ab[1] or "").lower()
            and int(aa[2]) == int(ab[2])
        )
    return False


def _review_git_url(url: str) -> str:
    """Clone URL for a review page (PR/MR suffix stripped)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    return re.sub(
        r"/(?:-/)?(?:pullrequest|pullRequest|merge_requests)/\d+/?$",
        "",
        raw,
        flags=re.IGNORECASE,
    )


def _review_project_iid(
    *,
    url: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    repository_url: str = "",
) -> Optional[tuple[str, int]]:
    """Identity for delete: ``(normalized project/repo, mr/pr iid)``."""
    from src.azure.webhook import parse_azure_git_url, parse_pull_request_url
    from src.gitlab.client import parse_merge_request_url, project_from_repo_url
    from src.state.session_bind_store import normalize_repo_key

    iid = 0
    try:
        iid = int(mr_iid or 0)
    except (TypeError, ValueError):
        iid = 0
    parsed_gl = parse_merge_request_url(url) if url else None
    if parsed_gl:
        host, project, found = parsed_gl
        key = normalize_repo_key(f"https://{host}/{project}")
        if key and int(found) > 0:
            return key, int(found)
    parsed_az = parse_pull_request_url(url) if url else None
    if parsed_az:
        key = normalize_repo_key(_review_git_url(url))
        if key and int(parsed_az[2]) > 0:
            return key, int(parsed_az[2])
    git = (repository_url or "").strip() or _review_git_url(url)
    if git and iid > 0:
        pair = project_from_repo_url(git)
        if pair:
            key = normalize_repo_key(f"https://{pair[0]}/{pair[1]}")
            if key:
                return key, iid
        parsed = parse_azure_git_url(git)
        if parsed:
            key = normalize_repo_key(git)
            if key:
                return key, iid
        key = normalize_repo_key(git)
        if key:
            return key, iid
    path = (project_path or "").strip().strip("/")
    if path and iid > 0:
        key = path.lower()
        return key, iid
    return None


def _same_project_iid(
    left: Optional[tuple[str, int]], right: Optional[tuple[str, int]]
) -> bool:
    """True when both sides are the same git project and the same MR/PR id."""
    if not left or not right:
        return False
    if int(left[1]) <= 0 or int(left[1]) != int(right[1]):
        return False
    a = (left[0] or "").strip().lower().strip("/")
    b = (right[0] or "").strip().lower().strip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if "/" in shorter and longer.endswith("/" + shorter):
        return True
    return False


def _remote_matches_review(remote_url: str, review_url: str) -> bool:
    """True when *remote_url* is the git remote for *review_url*."""
    from src.state.session_bind_store import normalize_repo_key

    review = _review_git_url(review_url)
    left = normalize_repo_key(remote_url or "")
    right = normalize_repo_key(review)
    return bool(left and right and left == right)


def _job_matches_review(
    job: Dict[str, Any],
    *,
    mr_url: str,
    project_path: str = "",
    mr_iid: int = 0,
    repository_url: str = "",
) -> bool:
    """True when *job* is this project URL + MR/PR iid (not merely the Jira key)."""
    want = _review_project_iid(
        url=mr_url,
        project_path=project_path,
        mr_iid=mr_iid,
        repository_url=repository_url,
    )
    if not want:
        return False
    job_iid = int(job.get("gitlab_mr_iid") or job.get("azure_pr_id") or 0)
    got = _review_project_iid(
        url=str(job.get("merge_request_url") or ""),
        project_path=str(
            job.get("gitlab_project")
            or job.get("azure_project")
            or ""
        ),
        mr_iid=job_iid,
        repository_url=str(job.get("repository_url") or ""),
    )
    return _same_project_iid(want, got)


def clone_folder_names_for_mr(
    *,
    mr_url: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    issue_key: str = "",
    source_branch: str = "",
    repository_url: str = "",
) -> List[str]:
    """Temp-clone folder names for this project URL + MR/PR iid."""
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
    want = _review_project_iid(
        url=want_url,
        project_path=want_path,
        mr_iid=mr_iid,
        repository_url=repository_url,
    )

    try:
        from src.state.job_store import job_store

        n = job_store.count_jobs()
        for job in job_store.list_jobs(limit=max(int(n or 0), 1)):
            if _job_matches_review(
                job,
                mr_url=want_url,
                project_path=want_path,
                mr_iid=mr_iid,
                repository_url=repository_url,
            ):
                _add(job.get("working_directory"))
    except Exception as e:
        logger.debug(f"MR clone lookup from jobs failed: {e}")

    try:
        exist = _existing_temp_names()
        for lookup, rec in _clone_issue_index().items():
            rec_url = str(rec.get("merge_request_url") or "")
            rec_ident = _review_project_iid(url=rec_url)
            if want and rec_ident and _same_project_iid(want, rec_ident):
                name = Path(str(lookup).replace("\\", "/")).name
                if name in exist or lookup in exist:
                    _add(lookup)
                continue
            if _same_review_url(want_url, rec_url):
                name = Path(str(lookup).replace("\\", "/")).name
                if name in exist or lookup in exist:
                    _add(lookup)
    except Exception as e:
        logger.debug(f"MR clone lookup from storage index failed: {e}")
    return names


def delete_clones_for_merge_request(
    *,
    mr_url: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    issue_key: str = "",
    source_branch: str = "",
    repository_url: str = "",
) -> List[str]:
    """Queue force-delete of temp clones for a merged or closed MR.

    Skips only an in-flight GitManager clone (job still running). Session
    binds stay after the job finishes — treating those as in-use made
    merge/close delete a no-op for every real job.
    """
    names = clone_folder_names_for_mr(
        mr_url=mr_url,
        project_path=project_path,
        mr_iid=mr_iid,
        issue_key=issue_key,
        source_branch=source_branch,
        repository_url=repository_url,
    )
    exist = _existing_temp_names()
    deleted: List[str] = []
    live = _live_git_paths()
    for name in names:
        if name not in exist:
            logger.debug(f"Skip MR-merge delete of {name}: already gone")
            continue
        try:
            target = _validate_delete_target(name, area="temp")
        except TempStorageError as e:
            logger.debug(f"Skip MR-merge delete of {name}: {e}")
            continue
        try:
            resolved = target.resolve()
        except OSError:
            resolved = target
        if resolved in live:
            logger.info(f"Skip MR-merge delete of {name}: clone is in flight")
            continue
        _forget_binds_for_clone(resolved)
        try:
            queue_delete_temp_folder(name, area="temp")
            deleted.append(name)
        except TempStorageError as e:
            logger.warning(f"Could not queue MR-merge delete of {name}: {e}")
    return deleted


def sweep_merged_storage_clones() -> List[str]:
    """Delete temp clones whose GitLab MR or Azure PR is done.

    Walks folders that exist on disk (not stale job-store names). GitLab.com
    cannot POST to a LAN daemon, so merge webhooks often never arrive.
    """
    deleted: List[str] = []
    base = resolve_temp_base()
    if not base.is_dir():
        return deleted
    try:
        index = _build_clone_issue_index()
    except Exception as e:
        logger.debug(f"MR sweep index failed: {e}")
        return deleted
    live = _live_git_paths()
    seen_urls: Set[str] = set()
    try:
        children = list(base.iterdir())
    except OSError:
        return deleted
    for child in children:
        if not child.is_dir():
            continue
        fields = _issue_fields_for(child, child.name, index)
        url = str(fields.get("merge_request_url") or "").strip()
        if not url:
            continue
        norm = _norm_mr_url(url)
        if norm in seen_urls:
            state = _cached_mr_state(url) or ""
        else:
            seen_urls.add(norm)
            state = _cached_mr_state(url) or ""
            if not state:
                try:
                    state = _lookup_review_state(url)
                except Exception as e:
                    logger.debug(f"Storage sweep status {url!r} failed: {e}")
                    state = "unknown"
                remember_mr_state(url, state)
                if state != "unknown":
                    _persist_job_mr_state(url, state)
        if state not in {"merged", "closed", "completed", "abandoned"}:
            continue
        try:
            resolved = child.resolve()
        except OSError:
            resolved = child
        if resolved in live:
            logger.debug(f"Skip MR-merge delete of {child.name}: clone is in flight")
            continue
        _forget_binds_for_clone(resolved)
        try:
            queue_delete_temp_folder(child.name, area="temp")
            deleted.append(child.name)
            logger.info(
                f"Storage review {url} is {state} — deleted clone {child.name}"
            )
        except TempStorageError as e:
            logger.debug(f"Could not queue MR-merge delete of {child.name}: {e}")
    return deleted


def queue_delete_temp_folder(name: str, *, area: str = "temp") -> Dict[str, Any]:
    """Start a background force-delete and return immediately."""
    kind = (area or "temp").strip().lower() or "temp"
    target = _validate_delete_target(name, area=kind)
    if kind == "temp":
        _raise_if_clone_in_use(target)
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
