"""Persistent work queue for Jira issues, GitLab MR comments, and Azure PR comments.

FIFO per workspace lock (repo + work branch + target). Dashboard lists these
rows so operators can see what is waiting.
"""

from __future__ import annotations

import copy
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.logger import logger
from src.state.record_db import connect, database_path, dumps, import_json_once, loads

_OPEN = frozenset({"queued", "running"})
_TERMINAL = frozenset({"completed", "cancelled", "error", "skipped"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue_items (
    queue_id TEXT PRIMARY KEY,
    issue_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    repository_url TEXT NOT NULL DEFAULT '',
    merge_request_url TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    gitlab_note_id TEXT NOT NULL DEFAULT '',
    azure_comment_id TEXT NOT NULL DEFAULT '',
    jira_event_id TEXT NOT NULL DEFAULT '',
    document TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_queue_issue ON queue_items(issue_key, status);
CREATE INDEX IF NOT EXISTS idx_queue_status_created
    ON queue_items(status, created_at);
CREATE INDEX IF NOT EXISTS idx_queue_repo ON queue_items(repository_url);
CREATE INDEX IF NOT EXISTS idx_queue_mr ON queue_items(merge_request_url);
CREATE INDEX IF NOT EXISTS idx_queue_workdir ON queue_items(working_directory);
CREATE INDEX IF NOT EXISTS idx_queue_note ON queue_items(gitlab_note_id);
CREATE INDEX IF NOT EXISTS idx_queue_azure ON queue_items(azure_comment_id);
CREATE INDEX IF NOT EXISTS idx_queue_jira_event ON queue_items(jira_event_id);
"""

_UPSERT = """
INSERT INTO queue_items (
    queue_id, issue_key, status, source, repository_url, merge_request_url,
    working_directory, created_at, updated_at, gitlab_note_id, azure_comment_id,
    jira_event_id, document
) VALUES (
    :queue_id, :issue_key, :status, :source, :repository_url, :merge_request_url,
    :working_directory, :created_at, :updated_at, :gitlab_note_id,
    :azure_comment_id, :jira_event_id, :document
)
ON CONFLICT(queue_id) DO UPDATE SET
    issue_key=excluded.issue_key,
    status=excluded.status,
    source=excluded.source,
    repository_url=excluded.repository_url,
    merge_request_url=excluded.merge_request_url,
    working_directory=excluded.working_directory,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at,
    gitlab_note_id=excluded.gitlab_note_id,
    azure_comment_id=excluded.azure_comment_id,
    jira_event_id=excluded.jira_event_id,
    document=excluded.document
"""


def _default_queue_dir() -> Path:
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("queue")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def queue_lock_keys(rec: Dict[str, Any]) -> List[str]:
    """Every workspace lock this row holds. ``lock_key`` stays the primary."""
    found: List[str] = []
    seen: set[str] = set()
    primary = str((rec or {}).get("lock_key") or "").strip()
    extra = (rec or {}).get("lock_keys")
    raws: List[Any] = [primary]
    if isinstance(extra, list):
        raws.extend(extra)
    for raw in raws:
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(key)
    return found


def workspace_lock_key(
    repository_url: str, work_branch: str, target_branch: str = ""
) -> str:
    """Same identity as the OpenCode session bind (repo + work + target)."""
    from src.state.session_bind_store import bind_id_for, normalize_branch, normalize_repo_key

    repo = normalize_repo_key(repository_url or "")
    work = normalize_branch(work_branch or "")
    tgt = normalize_branch(target_branch or "")
    if not repo or not work:
        return ""
    if tgt:
        return bind_id_for(repository_url, work, tgt)
    return f"lock_{repo}::{work.lower()}"


def _col(rec: Dict[str, Any], key: str) -> str:
    return str(rec.get(key) or "").strip()


class WorkQueueStore:
    """Queue rows in yaver.sqlite. Open rows stay cached in memory.

    Leftover ``q_*.json`` is imported on the first read or write, then
    deleted. Later calls do not scan that folder.
    """

    def __init__(self, queue_dir: Optional[Path] = None) -> None:
        self.queue_dir = queue_dir or _default_queue_dir()
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Queued and running rows only. Finished history stays in the database.
        self._open: Dict[str, Dict[str, Any]] = {}
        self._open_loaded = False
        self._imported = False
        self._epoch = 0
        self._listeners: List[Callable[[], None]] = []
        self._conn = None
        try:
            conn = connect(database_path(self.queue_dir))
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        except Exception as exc:
            logger.warning(f"Queue database unavailable: {exc}")
            self._conn = None

    def _path(self, queue_id: str) -> Path:
        safe = (queue_id or "").replace("/", "_").replace("\\", "_")
        return self.queue_dir / f"{safe}.json"

    def _params(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "queue_id": _col(rec, "queue_id"),
            "issue_key": _col(rec, "issue_key"),
            "status": _col(rec, "status"),
            "source": _col(rec, "source"),
            "repository_url": _col(rec, "repository_url"),
            "merge_request_url": _col(rec, "merge_request_url"),
            "working_directory": _col(rec, "working_directory"),
            "created_at": _col(rec, "created_at"),
            "updated_at": _col(rec, "updated_at"),
            "gitlab_note_id": _col(rec, "gitlab_note_id"),
            "azure_comment_id": _col(rec, "azure_comment_id"),
            "jira_event_id": _col(rec, "jira_event_id"),
            "document": dumps(rec),
        }

    def _upsert_locked(self, rec: Dict[str, Any]) -> None:
        """Caller holds ``_lock``. Does not import."""
        if self._conn is None:
            raise RuntimeError(
                f"queue database unavailable for {rec.get('queue_id')}"
            )
        params = self._params(rec)
        if not params["queue_id"]:
            raise RuntimeError("queue row is missing queue_id")
        self._conn.execute(_UPSERT, params)
        self._conn.commit()
        if not self._open_loaded:
            return
        qid = params["queue_id"]
        if rec.get("status") in _OPEN:
            self._open[qid] = copy.deepcopy(rec)
        else:
            self._open.pop(qid, None)

    def _consume_file(self, path: Path) -> str:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning(f"Queue import skip {path.name}: {exc}")
            return "bad"
        if not isinstance(raw, dict):
            logger.warning(f"Queue import skip {path.name}: not an object")
            return "bad"
        rec = dict(raw)
        qid = str(rec.get("queue_id") or "").strip() or path.stem
        if not qid.startswith("q_"):
            return "skip"
        rec["queue_id"] = qid
        self._upsert_locked(rec)
        return "ok"

    def _ensure_imported_locked(self) -> None:
        """One-shot import. Caller holds ``_lock``. A glob error retries later."""
        if self._imported or self._conn is None:
            return
        import_json_once(
            self._conn,
            self.queue_dir,
            flag="queue_json_imported",
            pattern="q_*.json",
            consume=self._consume_file,
        )
        self._imported = True

    def _write(self, rec: Dict[str, Any]) -> None:
        """Caller holds ``_lock``."""
        self._ensure_imported_locked()
        self._upsert_locked(rec)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` after a row is queued, claimed, finished, or cancelled."""
        with self._lock:
            self._listeners.append(callback)

        def _unsub() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return _unsub

    def epoch(self) -> int:
        """Increments on each change so a live socket can refresh without a timer."""
        with self._lock:
            return self._epoch

    def _notify(self) -> None:
        """Caller must not hold ``_lock``. Listeners may list rows."""
        with self._lock:
            self._epoch += 1
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception as exc:
                logger.debug(f"Queue listener failed: {exc}")

    def _ensure_open_locked(self) -> None:
        """Load queued and running rows once. Caller holds ``_lock``."""
        if self._open_loaded:
            return
        self._ensure_imported_locked()
        found: Dict[str, Dict[str, Any]] = {}
        if self._conn is not None:
            rows = self._conn.execute(
                "SELECT document FROM queue_items "
                "WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                rec = loads(row["document"])
                if not rec or rec.get("status") not in _OPEN:
                    continue
                qid = str(rec.get("queue_id") or "")
                if qid:
                    found[qid] = rec
        self._open = found
        self._open_loaded = True

    def _load_one_locked(self, queue_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_imported_locked()
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT document FROM queue_items WHERE queue_id = ?",
            (queue_id,),
        ).fetchone()
        if row is None:
            return None
        rec = loads(row["document"])
        return copy.deepcopy(rec) if rec else None

    def get(self, queue_id: str) -> Optional[Dict[str, Any]]:
        qid = (queue_id or "").strip()
        if not qid:
            return None
        with self._lock:
            try:
                return self._load_one_locked(qid)
            except Exception as exc:
                logger.debug(f"Could not read queue item {queue_id}: {exc}")
                return None

    def enqueue(
        self,
        *,
        source: str,
        issue_key: str,
        summary: str = "",
        message: str = "",
        repository_url: str = "",
        source_branch: str = "",
        work_branch: str = "",
        target_branch: str = "",
        lock_key: str = "",
        lock_keys: Optional[List[str]] = None,
        job_id: Optional[str] = None,
        gitlab_note_id: str = "",
        azure_comment_id: str = "",
        merge_request_url: str = "",
        jira_event_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        qid = f"q_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        rec: Dict[str, Any] = {
            "queue_id": qid,
            "status": "queued",
            "source": (source or "jira").strip().lower() or "jira",
            "issue_key": (issue_key or "").strip(),
            "summary": (summary or "")[:500],
            "message": (message or "")[:8000],
            # Intentional: store the inbound URL as received. Clone/push
            # strip userinfo in GitManager.normalize_remote_url and auth
            # with the settings PAT. Do not rewrite here.
            "repository_url": repository_url or "",
            "source_branch": source_branch or "",
            "work_branch": work_branch or "",
            "target_branch": target_branch or "",
            "lock_key": lock_key or "",
            "lock_keys": [
                str(k).strip()
                for k in (lock_keys or [])
                if str(k or "").strip()
            ],
            "job_id": job_id,
            "gitlab_note_id": gitlab_note_id or "",
            "azure_comment_id": azure_comment_id or "",
            "jira_event_id": jira_event_id or "",
            "merge_request_url": merge_request_url or "",
            "payload": payload if isinstance(payload, dict) else {},
            "error_message": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
        }
        with self._lock:
            self._write(rec)
        logger.info(
            f"Queue enqueue {qid} source={rec['source']} "
            f"issue={rec['issue_key']} lock={rec['lock_key'] or '-'} "
            f"job_id={rec.get('job_id') or '-'}"
        )
        self._notify()
        return rec

    def list_items(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        limit_n = max(1, int(limit))
        with self._lock:
            if status in _OPEN:
                self._ensure_open_locked()
                items = [
                    copy.deepcopy(rec)
                    for rec in self._open.values()
                    if rec.get("status") == status
                ]
            else:
                items = []
                for rec in self._iter_records():
                    if status and rec.get("status") != status:
                        continue
                    items.append(rec)
        items.sort(key=lambda r: (r.get("created_at") or "", r.get("queue_id") or ""))
        return items[:limit_n]

    def _iter_records(self) -> List[Dict[str, Any]]:
        """Every queue document. Takes the store lock (callers may not)."""
        with self._lock:
            try:
                self._ensure_imported_locked()
            except OSError as exc:
                logger.warning(f"Queue import failed: {exc}")
                return []
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT document FROM queue_items"
                ).fetchall()
            except Exception as exc:
                logger.warning(f"Queue list failed: {exc}")
                return []
        items: List[Dict[str, Any]] = []
        for row in rows:
            rec = loads(row["document"])
            if rec and rec.get("queue_id"):
                items.append(rec)
        return items

    def _newest_document(self, sql: str, args: tuple) -> Optional[Dict[str, Any]]:
        with self._lock:
            try:
                self._ensure_imported_locked()
            except OSError as exc:
                logger.warning(f"Queue import failed: {exc}")
                return None
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(sql, args).fetchone()
            except Exception as exc:
                logger.debug(f"Queue lookup failed: {exc}")
                return None
        if row is None:
            return None
        rec = loads(row["document"])
        return copy.deepcopy(rec) if rec else None

    def find_open_jira(self, issue_key: str) -> Optional[Dict[str, Any]]:
        key = (issue_key or "").strip().upper()
        if not key:
            return None
        return self._newest_document(
            "SELECT document FROM queue_items "
            "WHERE status IN ('queued', 'running') AND source = 'jira' "
            "AND UPPER(issue_key) = ? "
            "ORDER BY created_at DESC, queue_id DESC LIMIT 1",
            (key,),
        )

    def find_note(self, note_id: str) -> Optional[Dict[str, Any]]:
        nid = (note_id or "").strip()
        if not nid:
            return None
        return self._newest_document(
            "SELECT document FROM queue_items "
            "WHERE gitlab_note_id = ? OR azure_comment_id = ? "
            "ORDER BY created_at DESC, queue_id DESC LIMIT 1",
            (nid, nid),
        )

    def find_jira_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        """Return a queue row for this Jira webhook event id (dedup retries)."""
        eid = (event_id or "").strip()
        if not eid:
            return None
        return self._newest_document(
            "SELECT document FROM queue_items WHERE jira_event_id = ? "
            "ORDER BY created_at DESC, queue_id DESC LIMIT 1",
            (eid,),
        )

    def claim_next(
        self,
        *,
        blocked_issue_keys: Optional[set] = None,
        blocked_locks: Optional[set] = None,
        max_running: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """FIFO claim of the next item whose workspace/issue is free.

        Scans up to 1000 queued rows (oldest first) so a long blocked
        backlog on one MR/PR does not hide a free workspace.
        """
        blocked = {(k or "").strip().upper() for k in (blocked_issue_keys or set()) if k}
        extra_locks = {(k or "").strip() for k in (blocked_locks or set()) if k}
        claimed: Optional[Dict[str, Any]] = None
        with self._lock:
            running = [
                r
                for r in self.list_items(status="running", limit=200)
            ]
            if len(running) >= max(1, int(max_running)):
                return None
            blocked_locks = set()
            for running_row in running:
                blocked_locks.update(queue_lock_keys(running_row))
            blocked_locks |= extra_locks
            blocked_issues = {
                (r.get("issue_key") or "").strip().upper() for r in running
            } | blocked
            for rec in self.list_items(status="queued", limit=1000):
                ik = (rec.get("issue_key") or "").strip().upper()
                if ik and ik in blocked_issues:
                    continue
                if any(key in blocked_locks for key in queue_lock_keys(rec)):
                    continue
                # Re-read under lock in case status changed
                live = self.get(rec["queue_id"])
                if not live or live.get("status") != "queued":
                    continue
                now = _now_iso()
                live["status"] = "running"
                live["started_at"] = now
                live["updated_at"] = now
                self._write(live)
                logger.info(
                    f"Queue claim {live['queue_id']} issue={live.get('issue_key')} "
                    f"job_id={live.get('job_id') or '-'}"
                )
                claimed = live
                break
        if claimed is not None:
            self._notify()
        return claimed

    def update(self, queue_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        updated: Optional[Dict[str, Any]] = None
        with self._lock:
            rec = self.get(queue_id)
            if not rec:
                return None
            for k, v in fields.items():
                if k == "queue_id":
                    continue
                rec[k] = v
            rec["updated_at"] = _now_iso()
            self._write(rec)
            updated = rec
        self._notify()
        return updated

    def finish(
        self,
        queue_id: str,
        *,
        status: str = "completed",
        error_message: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if status not in _TERMINAL:
            status = "completed"
        rec: Optional[Dict[str, Any]] = None
        with self._lock:
            current = self.get(queue_id)
            if current is None:
                return None
            held = (current.get("status") or "").strip().lower()
            if held in _TERMINAL:
                return current
            # The terminal check and the write share this hold so a second
            # finish cannot land in between.
            current["status"] = status
            current["finished_at"] = _now_iso()
            current["updated_at"] = current["finished_at"]
            if error_message is not None:
                current["error_message"] = (error_message or "")[:2000]
            if job_id:
                current["job_id"] = job_id
            self._write(current)
            rec = current
        if rec:
            logger.info(
                f"Queue finish {queue_id} status={status} "
                f"issue={rec.get('issue_key') or '-'} "
                f"job_id={rec.get('job_id') or job_id or '-'}"
            )
            self._notify()
        return rec

    def finish_open_for_issue(
        self,
        issue_key: str,
        *,
        status: str = "cancelled",
        error_message: Optional[str] = None,
        job_id: Optional[str] = None,
        sources: Optional[set] = None,
        include_queued: bool = True,
        include_running: bool = True,
        queued_sources: Optional[set] = None,
        started_before: Optional[str] = None,
    ) -> int:
        """Terminal-finish open rows for one issue.

        Dashboard Stop must close the leftover ``running`` claim and any
        Jira poller leftover. GitLab/Azure follow-ups that are still
        ``queued`` stay unless they are in *queued_sources* (or
        *include_queued* is true and *queued_sources* is unset).

        *started_before* leaves a row that began after Stop wrote its
        terminal time, so a schedule fired during the kill cannot be
        closed by the same Stop.
        """
        key = (issue_key or "").strip().upper()
        if not key:
            return 0
        if status not in _TERMINAL:
            status = "cancelled"
        cutoff = (started_before or "").strip()
        n = 0
        with self._lock:
            for rec in list(self._iter_records()):
                if rec.get("status") not in _OPEN:
                    continue
                if (rec.get("issue_key") or "").strip().upper() != key:
                    continue
                src = (rec.get("source") or "jira").strip().lower()
                if sources is not None and src not in sources:
                    continue
                row_status = rec.get("status")
                if row_status == "queued" and not include_queued:
                    continue
                if row_status == "running" and not include_running:
                    continue
                if (
                    row_status == "queued"
                    and queued_sources is not None
                    and src not in queued_sources
                ):
                    continue
                if cutoff:
                    stamp = (
                        rec.get("started_at")
                        if row_status == "running"
                        else rec.get("created_at")
                    )
                    if str(stamp or "") > cutoff:
                        continue
                qid = rec.get("queue_id")
                if not qid:
                    continue
                rec["status"] = status
                rec["finished_at"] = _now_iso()
                rec["updated_at"] = rec["finished_at"]
                if error_message is not None:
                    rec["error_message"] = (error_message or "")[:2000]
                if job_id:
                    rec["job_id"] = job_id
                self._write(rec)
                n += 1
                logger.info(
                    f"Queue finish {qid} status={status} issue={key} "
                    f"job_id={rec.get('job_id') or job_id or '-'} "
                    f"(open-for-issue)"
                )
        if n:
            self._notify()
        return n

    def recover_stuck_running(self, *, reason: str = "startup: orphaned running") -> int:
        """Re-queue durable ``running`` rows after a crash (no live worker)."""
        n = 0
        # Accepted: startup requeue covers the oldest 500 running rows.
        for rec in list(self.list_items(status="running", limit=500)):
            qid = rec.get("queue_id")
            if not qid:
                continue
            if self.requeue(str(qid), reason=reason):
                n += 1
        if n:
            logger.info(f"Re-queued {n} orphaned running queue item(s)")
        return n

    def requeue(self, queue_id: str, *, reason: str = "") -> Optional[Dict[str, Any]]:
        """Put a running item back to queued (in-flight collision).

        Only ``running`` rows move. A cancelled/completed/skipped row must
        not come back to life if the worker loses a race with Stop.
        """
        updated: Optional[Dict[str, Any]] = None
        with self._lock:
            rec = self.get(queue_id)
            if not rec or rec.get("status") != "running":
                return None
            rec["status"] = "queued"
            rec["started_at"] = None
            rec["error_message"] = (reason or "")[:500] or None
            rec["updated_at"] = _now_iso()
            self._write(rec)
            logger.info(f"Queue requeue {queue_id}: {reason or 'retry later'}")
            updated = rec
        self._notify()
        return updated

    def cancel(self, queue_id: str) -> bool:
        """Cancel a row that is still queued.

        The status check and the write share the lock. ``finish`` would
        also cancel a row that ``claim_next`` already marked running.
        """
        cancelled = False
        with self._lock:
            rec = self.get(queue_id)
            if not rec or rec.get("status") != "queued":
                return False
            now = _now_iso()
            rec["status"] = "cancelled"
            rec["finished_at"] = now
            rec["updated_at"] = now
            self._write(rec)
            cancelled = True
        if cancelled:
            self._notify()
        return cancelled


work_queue_store = WorkQueueStore()
