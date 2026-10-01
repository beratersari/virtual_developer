"""Schedule rows in yaver.sqlite.

The document column is the full record. Indexed columns serve list, count,
and "is this issue already scheduled?". Leftover sched_*.json is imported
once on reconcile, then those files are deleted and not scanned again.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.logger import logger
from src.state.record_db import (
    connect,
    database_path,
    dumps,
    ensure_column,
    import_json_once,
    loads,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT '',
    scheduled_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    issue_key TEXT NOT NULL DEFAULT '',
    repository_url TEXT NOT NULL DEFAULT '',
    merge_request_url TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    document TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_schedules_status_when
    ON schedules(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_schedules_issue
    ON schedules(issue_key, status);
"""

_EXTRA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_schedules_repo ON schedules(repository_url);
CREATE INDEX IF NOT EXISTS idx_schedules_mr ON schedules(merge_request_url);
CREATE INDEX IF NOT EXISTS idx_schedules_workdir ON schedules(working_directory);
"""

_ORDER = (
    "CASE WHEN TRIM(IFNULL(scheduled_at,'')) != '' THEN scheduled_at "
    "ELSE IFNULL(created_at,'') END DESC, schedule_id DESC"
)

_UPSERT = """
INSERT INTO schedules (
    schedule_id, status, scheduled_at, created_at, updated_at, issue_key,
    repository_url, merge_request_url, working_directory, document
) VALUES (
    :schedule_id, :status, :scheduled_at, :created_at, :updated_at, :issue_key,
    :repository_url, :merge_request_url, :working_directory, :document
)
ON CONFLICT(schedule_id) DO UPDATE SET
    status=excluded.status,
    scheduled_at=excluded.scheduled_at,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at,
    issue_key=excluded.issue_key,
    repository_url=excluded.repository_url,
    merge_request_url=excluded.merge_request_url,
    working_directory=excluded.working_directory,
    document=excluded.document
"""


def default_index_path(schedules_dir: Path) -> Path:
    return database_path(schedules_dir)


def _text(rec: Dict[str, Any], key: str) -> str:
    return str(rec.get(key) or "").strip()


def row_params(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schedule_id": _text(rec, "schedule_id"),
        "status": _text(rec, "status"),
        "scheduled_at": _text(rec, "scheduled_at"),
        "created_at": _text(rec, "created_at"),
        "updated_at": _text(rec, "updated_at"),
        "issue_key": _text(rec, "issue_key").upper(),
        "repository_url": _text(rec, "repository_url"),
        "merge_request_url": _text(rec, "merge_request_url"),
        "working_directory": _text(rec, "working_directory"),
        "document": dumps(rec),
    }


def row_to_schedule(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    if "document" in keys:
        doc = loads(row["document"])
        if doc is not None:
            out = dict(doc)
            if not str(out.get("schedule_id") or "").strip():
                out["schedule_id"] = row["schedule_id"]
            return out
    return {
        "schedule_id": row["schedule_id"],
        "status": row["status"] or "",
        "scheduled_at": row["scheduled_at"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
        "issue_key": row["issue_key"] or "",
        "repository_url": row["repository_url"] if "repository_url" in keys else "",
        "merge_request_url": (
            row["merge_request_url"] if "merge_request_url" in keys else ""
        ),
        "working_directory": (
            row["working_directory"] if "working_directory" in keys else ""
        ),
    }


class ScheduleIndex:
    """WAL SQLite for schedule rows. Safe for one daemon per data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        conn = connect(self.path)
        conn.executescript(_SCHEMA)
        ensure_column(conn, "schedules", "repository_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "schedules", "merge_request_url", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "schedules", "working_directory", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "schedules", "document", "TEXT NOT NULL DEFAULT ''")
        conn.executescript(_EXTRA_INDEXES)
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
        params = row_params(rec)
        if not params["schedule_id"]:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(_UPSERT, params)
            self._conn.commit()

    def get(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        sid = (schedule_id or "").strip()
        if not sid:
            return None
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE schedule_id = ?", (sid,)
            ).fetchone()
        return row_to_schedule(row) if row else None

    def list_records(
        self,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = ""
        args: List[Any] = []
        if status:
            where = "WHERE status = ?"
            args.append(status)
        sql = f"SELECT * FROM schedules {where} ORDER BY {_ORDER}"
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
        return [row_to_schedule(r) for r in rows]

    def list_ids(
        self,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = 200,
        offset: int = 0,
    ) -> List[str]:
        return [
            str(rec["schedule_id"])
            for rec in self.list_records(status=status, limit=limit, offset=offset)
        ]

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

    def records_with_status(self, status: str) -> List[Dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM schedules WHERE status = ?",
                (status,),
            ).fetchall()
        return [row_to_schedule(r) for r in rows]

    def ids_with_status(self, status: str) -> List[str]:
        return [str(rec["schedule_id"]) for rec in self.records_with_status(status)]

    def delete(self, schedule_id: str) -> bool:
        sid = (schedule_id or "").strip()
        if not sid:
            return False
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM schedules WHERE schedule_id = ?", (sid,))
            changed = self._conn.execute("SELECT changes()").fetchone()
            self._conn.commit()
        return bool(changed and int(changed[0]) > 0)

    def reconcile(self, schedules_dir: Path) -> int:
        """Import leftover ``sched_*.json`` once. Later calls do not scan.

        A file that omits ``schedule_id`` takes the filename. Rows already in
        the database stay when their file is already gone.
        """
        with self._lock:
            assert self._conn is not None
            try:
                return import_json_once(
                    self._conn,
                    schedules_dir,
                    flag="schedules_json_imported",
                    pattern="sched_*.json",
                    consume=self._consume_schedule_file,
                )
            except OSError as exc:
                logger.warning(f"Schedule import glob failed: {exc}")
                raise

    def _consume_schedule_file(self, path: Path) -> str:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning(f"Schedule import skip {path.name}: {exc}")
            return "bad"
        if not isinstance(raw, dict):
            logger.warning(f"Schedule import skip {path.name}: not an object")
            return "bad"
        rec = dict(raw)
        sid = str(rec.get("schedule_id") or "").strip() or path.stem
        if not sid.startswith("sched_"):
            return "skip"
        rec["schedule_id"] = sid
        self.upsert(rec)
        return "ok"
