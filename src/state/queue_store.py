"""Persistent work queue for Jira issues and GitLab MR comments.

FIFO per workspace lock (repo + work branch + target). Dashboard lists these
rows so operators can see what is waiting.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger

_OPEN = frozenset({"queued", "running"})
_TERMINAL = frozenset({"completed", "cancelled", "error", "skipped"})


def _default_queue_dir() -> Path:
    return Path.cwd() / ".jira-agent" / "queue"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


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


class WorkQueueStore:
    """One JSON file per queue item (``q_<12 hex>.json``)."""

    def __init__(self, queue_dir: Optional[Path] = None) -> None:
        self.queue_dir = queue_dir or _default_queue_dir()
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, queue_id: str) -> Path:
        safe = (queue_id or "").replace("/", "_").replace("\\", "_")
        return self.queue_dir / f"{safe}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        path = self._path(rec["queue_id"])
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        tmp.replace(path)

    def get(self, queue_id: str) -> Optional[Dict[str, Any]]:
        path = self._path((queue_id or "").strip())
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
            return rec if isinstance(rec, dict) else None
        except Exception as e:
            logger.debug(f"Could not read queue item {queue_id}: {e}")
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
        job_id: Optional[str] = None,
        gitlab_note_id: str = "",
        merge_request_url: str = "",
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
            "repository_url": repository_url or "",
            "source_branch": source_branch or "",
            "work_branch": work_branch or "",
            "target_branch": target_branch or "",
            "lock_key": lock_key or "",
            "job_id": job_id,
            "gitlab_note_id": gitlab_note_id or "",
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
            f"issue={rec['issue_key']} lock={rec['lock_key'] or '-'}"
        )
        return rec

    def list_items(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not self.queue_dir.is_dir():
            return items
        with self._lock:
            for path in self.queue_dir.glob("q_*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if not isinstance(rec, dict) or not rec.get("queue_id"):
                    continue
                if status and rec.get("status") != status:
                    continue
                items.append(rec)
        items.sort(key=lambda r: (r.get("created_at") or "", r.get("queue_id") or ""))
        return items[: max(1, int(limit))]

    def _iter_records(self) -> List[Dict[str, Any]]:
        """All queue JSON rows (no oldest-N cap). Caller should hold ``_lock``."""
        items: List[Dict[str, Any]] = []
        if not self.queue_dir.is_dir():
            return items
        for path in self.queue_dir.glob("q_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rec = json.load(f)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("queue_id"):
                items.append(rec)
        return items

    def find_open_jira(self, issue_key: str) -> Optional[Dict[str, Any]]:
        key = (issue_key or "").strip().upper()
        if not key:
            return None
        best: Optional[Dict[str, Any]] = None
        with self._lock:
            for rec in self._iter_records():
                if rec.get("status") not in _OPEN:
                    continue
                if rec.get("source") != "jira":
                    continue
                if (rec.get("issue_key") or "").strip().upper() != key:
                    continue
                if best is None or (rec.get("created_at") or "") >= (
                    best.get("created_at") or ""
                ):
                    best = rec
        return best

    def find_note(self, note_id: str) -> Optional[Dict[str, Any]]:
        nid = (note_id or "").strip()
        if not nid:
            return None
        best: Optional[Dict[str, Any]] = None
        with self._lock:
            for rec in self._iter_records():
                if str(rec.get("gitlab_note_id") or "") != nid:
                    continue
                if best is None or (rec.get("created_at") or "") >= (
                    best.get("created_at") or ""
                ):
                    best = rec
        return best

    def claim_next(
        self,
        *,
        blocked_issue_keys: Optional[set] = None,
        blocked_locks: Optional[set] = None,
        max_running: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """FIFO claim of the next item whose workspace/issue is free."""
        blocked = {(k or "").strip().upper() for k in (blocked_issue_keys or set()) if k}
        extra_locks = {(k or "").strip() for k in (blocked_locks or set()) if k}
        with self._lock:
            running = [
                r
                for r in self.list_items(status="running", limit=200)
            ]
            if len(running) >= max(1, int(max_running)):
                return None
            blocked_locks = {
                (r.get("lock_key") or "") for r in running if r.get("lock_key")
            } | extra_locks
            blocked_issues = {
                (r.get("issue_key") or "").strip().upper() for r in running
            } | blocked
            for rec in self.list_items(status="queued", limit=300):
                ik = (rec.get("issue_key") or "").strip().upper()
                if ik and ik in blocked_issues:
                    continue
                lk = rec.get("lock_key") or ""
                if lk and lk in blocked_locks:
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
                    f"Queue claim {live['queue_id']} issue={live.get('issue_key')}"
                )
                return live
        return None

    def update(self, queue_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
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
            return rec

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
        patch: Dict[str, Any] = {
            "status": status,
            "finished_at": _now_iso(),
        }
        if error_message is not None:
            patch["error_message"] = (error_message or "")[:2000]
        if job_id:
            patch["job_id"] = job_id
        rec = self.update(queue_id, **patch)
        if rec:
            logger.info(f"Queue finish {queue_id} status={status}")
        return rec

    def recover_stuck_running(self, *, reason: str = "startup: orphaned running") -> int:
        """Re-queue durable ``running`` rows after a crash (no live worker)."""
        n = 0
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
        """Put a running item back to queued (in-flight collision)."""
        rec = self.update(
            queue_id,
            status="queued",
            started_at=None,
            error_message=(reason or "")[:500] or None,
        )
        if rec:
            logger.info(f"Queue requeue {queue_id}: {reason or 'retry later'}")
        return rec

    def cancel(self, queue_id: str) -> bool:
        rec = self.get(queue_id)
        if not rec:
            return False
        if rec.get("status") != "queued":
            return False
        self.finish(queue_id, status="cancelled")
        return True


work_queue_store = WorkQueueStore()
