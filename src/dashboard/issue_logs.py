"""In-memory ring + durable per-job system logs for the ops dashboard.

Live lines go into a process-wide ring (recent activity / task filters).
When a ``job_id`` is present, the same line is **appended** to
``.jira-agent/jobs/{job_id}.system.log`` so job detail survives daemon restarts.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


def _default_jobs_dir() -> Path:
    return Path.cwd() / ".jira-agent" / "jobs"


def job_system_log_path(job_id: str, *, jobs_dir: Optional[Path] = None) -> Optional[Path]:
    """Path for durable system log of one job (``job_*.system.log``)."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    safe = jid.replace("/", "_").replace("\\", "_")
    # Only allow job_* style ids to avoid path abuse
    if not re.match(r"^job_[A-Za-z0-9_.\-]+$", safe):
        return None
    root = jobs_dir or _default_jobs_dir()
    return root / f"{safe}.system.log"


class IssueLogRing:
    """Keeps recent log lines; filter by issue key or job_id for detail views."""

    def __init__(
        self,
        maxlen: int = 5000,
        *,
        jobs_dir: Optional[Path] = None,
        persist: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        # (iso_ts, message, job_id|None, issue_key|None)
        self._lines: Deque[Tuple[str, str, Optional[str], Optional[str]]] = deque(
            maxlen=maxlen
        )
        self._jobs_dir = jobs_dir  # None → resolve at write time from cwd
        self._persist = persist
        self._file_locks: Dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()

    def _file_lock(self, jid: str) -> threading.Lock:
        with self._file_locks_guard:
            lock = self._file_locks.get(jid)
            if lock is None:
                lock = threading.Lock()
                self._file_locks[jid] = lock
            return lock

    def append(
        self,
        message: str,
        *,
        job_id: Optional[str] = None,
        issue_key: Optional[str] = None,
    ) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        jid = (job_id or "").strip() or None
        ikey = (issue_key or "").strip() or None
        with self._lock:
            self._lines.append((ts, message, jid, ikey))
        if self._persist and jid:
            self._append_job_file(jid, ts, message, issue_key=ikey)

    def _append_job_file(
        self,
        job_id: str,
        ts: str,
        message: str,
        *,
        issue_key: Optional[str] = None,
    ) -> None:
        path = job_system_log_path(job_id, jobs_dir=self._jobs_dir)
        if path is None:
            return
        # One JSON-ish plain line: timestamp\tmessage  (message already includes job_id tag)
        line = message if message.endswith("\n") else f"{message}\n"
        # Prefer storing the ring timestamp prefix for stable parse on read
        if not message.startswith(ts[:10]):
            # message already has its own clock from logger; keep as-is
            pass
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lock(job_id):
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            # Never fail the main logger path; disk persistence is best-effort
            pass

    def for_issue(self, issue_key: str, *, limit: int = 500) -> List[Dict[str, str]]:
        key = (issue_key or "").strip()
        if not key:
            return []
        key_u = key.upper()
        with self._lock:
            matched = []
            for ts, msg, jid, ikey in self._lines:
                if (ikey and ikey.upper() == key_u) or key in msg or key_u in msg:
                    row: Dict[str, str] = {"timestamp": ts, "message": msg}
                    if jid:
                        row["job_id"] = jid
                    matched.append(row)
        return matched[-limit:]

    def for_job_memory(self, job_id: str, *, limit: int = 500) -> List[Dict[str, str]]:
        """In-memory lines only (current process)."""
        jid = (job_id or "").strip()
        if not jid:
            return []
        needle = f"job_id={jid}"
        with self._lock:
            matched = []
            for ts, msg, line_jid, ikey in self._lines:
                if line_jid == jid or needle in msg or jid in msg:
                    row: Dict[str, str] = {
                        "timestamp": ts,
                        "message": msg,
                        "job_id": jid,
                    }
                    if ikey:
                        row["issue_key"] = ikey
                    matched.append(row)
        return matched[-limit:]

    def for_job_disk(
        self,
        job_id: str,
        *,
        limit: int = 2000,
        jobs_dir: Optional[Path] = None,
    ) -> List[Dict[str, str]]:
        """Load durable system log file for a job (survives restarts)."""
        jid = (job_id or "").strip()
        if not jid:
            return []
        path = job_system_log_path(jid, jobs_dir=jobs_dir or self._jobs_dir)
        if path is None or not path.is_file():
            return []
        rows: List[Dict[str, str]] = []
        try:
            with self._file_lock(jid):
                text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for raw in text.splitlines():
            msg = raw.rstrip("\n")
            if not msg.strip():
                continue
            ts = _extract_timestamp(msg) or ""
            rows.append({"timestamp": ts, "message": msg, "job_id": jid})
        if limit > 0:
            rows = rows[-limit:]
        return rows

    def for_job(self, job_id: str, *, limit: int = 500) -> List[Dict[str, str]]:
        """Disk first (full history), then any live ring lines not already present.

        Dedupes by exact message text so a running daemon does not double-show
        the same line from memory + file.
        """
        jid = (job_id or "").strip()
        if not jid:
            return []
        disk = self.for_job_disk(jid, limit=max(limit, 2000))
        mem = self.for_job_memory(jid, limit=limit)
        if not disk:
            return mem[-limit:] if limit else mem
        if not mem:
            return disk[-limit:] if limit else disk

        seen = {r.get("message") for r in disk}
        merged = list(disk)
        for r in mem:
            msg = r.get("message")
            if msg and msg not in seen:
                seen.add(msg)
                merged.append(r)
        if limit > 0:
            merged = merged[-limit:]
        return merged


_TS_PREFIX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
)


def _extract_timestamp(message: str) -> Optional[str]:
    m = _TS_PREFIX.match(message or "")
    if not m:
        return None
    # Normalize space to ISO-ish for UI sort display
    return m.group(1).replace(" ", "T")


# Process-wide buffer (logger + dashboard share this)
issue_log_ring = IssueLogRing()
