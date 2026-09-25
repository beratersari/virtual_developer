"""Local SQLite index of schedule JSON files (Scheduled list and due checks).

One file per data dir, created on first open. JSON remains the full record.
List, count, and "is this issue already scheduled?" read this table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.logger import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    issue_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schedules_status_when
    ON schedules(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_schedules_issue
    ON schedules(issue_key, status);
"""

_ORDER = (
    "CASE WHEN TRIM(IFNULL(scheduled_at,'')) != '' THEN scheduled_at "
    "ELSE IFNULL(created_at,'') END DESC, schedule_id DESC"
)


def default_index_path(schedules_dir: Path) -> Path:
    return schedules_dir.parent / "schedules.sqlite"


def _text(rec: Dict[str, Any], key: str) -> str:
    return str(rec.get(key) or "").strip()


class ScheduleIndex:
    """WAL SQLite next to ``schedules/``. Safe for one daemon per data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def upsert(self, rec: Dict[str, Any]) -> None:
        sid = _text(rec, "schedule_id")
        if not sid:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                """
                INSERT INTO schedules (
                    schedule_id, status, scheduled_at, created_at, updated_at, issue_key
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    status=excluded.status,
                    scheduled_at=excluded.scheduled_at,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    issue_key=excluded.issue_key
                """,
                (
                    sid,
                    _text(rec, "status"),
                    _text(rec, "scheduled_at"),
                    _text(rec, "created_at"),
                    _text(rec, "updated_at"),
                    _text(rec, "issue_key").upper(),
                ),
            )
            self._conn.commit()

    def list_ids(
        self,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = 200,
        offset: int = 0,
    ) -> List[str]:
        where = ""
        args: List[Any] = []
        if status:
            where = "WHERE status = ?"
            args.append(status)
        sql = f"SELECT schedule_id FROM schedules {where} ORDER BY {_ORDER}"
        start = max(0, int(offset or 0))
        if limit is None:
            if start:
                sql += " LIMIT -1 OFFSET ?"
                args.append(start)
        else:
            sql += " LIMIT ? OFFSET ?"
            args.extend([max(0, int(limit)), start])
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, args).fetchall()
        return [str(r["schedule_id"]) for r in rows]

    def count(self, *, status: Optional[str] = None) -> int:
        with self._lock:
            assert self._conn is not None
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM schedules WHERE status = ?",
                    (status,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM schedules"
                ).fetchone()
        return int(row["n"] if row else 0)

    def has_issue_status(self, issue_key: str, statuses: Set[str]) -> bool:
        key = (issue_key or "").strip().upper()
        want = sorted({s.strip().lower() for s in statuses if s and s.strip()})
        if not key or not want:
            return False
        marks = ",".join("?" for _ in want)
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                f"SELECT 1 FROM schedules WHERE issue_key = ? "
                f"AND LOWER(status) IN ({marks}) LIMIT 1",
                [key, *want],
            ).fetchone()
        return row is not None

    def ids_with_status(self, status: str) -> List[str]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT schedule_id FROM schedules WHERE status = ?",
                (status,),
            ).fetchall()
        return [str(r["schedule_id"]) for r in rows]

    def all_ids(self) -> set[str]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT schedule_id FROM schedules").fetchall()
        return {str(r["schedule_id"]) for r in rows}

    def delete(self, schedule_id: str) -> None:
        sid = (schedule_id or "").strip()
        if not sid:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (sid,))
            self._conn.commit()

    def reconcile(self, schedules_dir: Path) -> int:
        """Reload every schedule JSON and drop index rows whose file is gone."""
        if not schedules_dir.is_dir():
            return 0
        try:
            paths = list(schedules_dir.glob("sched_*.json"))
        except OSError as e:
            logger.warning(f"Schedule index glob failed: {e}")
            raise
        kept: set[str] = set()
        unread: set[str] = set()
        upserted = 0
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError) as e:
                logger.warning(f"Schedule index skip {path.name}: {e}")
                unread.add(path.stem)
                continue
            if not isinstance(raw, dict):
                unread.add(path.stem)
                continue
            rec = dict(raw)
            sid = str(rec.get("schedule_id") or "").strip() or path.stem
            if not sid.startswith("sched_"):
                continue
            rec["schedule_id"] = sid
            self.upsert(rec)
            kept.add(sid)
            upserted += 1
        for sid in self.all_ids() - kept - unread:
            self.delete(sid)
        return upserted
