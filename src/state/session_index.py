"""Session-bind rows in yaver.sqlite.

The document column is the full record, including forgotten session ids.
Indexed columns serve the Sessions page, issue lookup, and workspace rollup.
Leftover osb_*.json is imported once, then those files are deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

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
CREATE TABLE IF NOT EXISTS session_binds (
    bind_id TEXT PRIMARY KEY,
    repository_url TEXT NOT NULL DEFAULT '',
    repository_key TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    target_branch TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    issue_key TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    forgotten_json TEXT NOT NULL DEFAULT '[]',
    reset_at TEXT NOT NULL DEFAULT '',
    forget_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_binds_live_updated
    ON session_binds(updated_at);
CREATE INDEX IF NOT EXISTS idx_binds_issue
    ON session_binds(issue_key, updated_at);
CREATE INDEX IF NOT EXISTS idx_binds_repo
    ON session_binds(repository_key, branch, target_branch);
"""

_UPSERT = """
INSERT INTO session_binds (
    bind_id, repository_url, repository_key, branch, target_branch,
    session_id, kind, backend, scope, issue_key, job_id, working_directory,
    forgotten_json, reset_at, forget_reason, created_at, updated_at,
    merge_request_url, document
) VALUES (
    :bind_id, :repository_url, :repository_key, :branch, :target_branch,
    :session_id, :kind, :backend, :scope, :issue_key, :job_id, :working_directory,
    :forgotten_json, :reset_at, :forget_reason, :created_at, :updated_at,
    :merge_request_url, :document
)
ON CONFLICT(bind_id) DO UPDATE SET
    repository_url=excluded.repository_url,
    repository_key=excluded.repository_key,
    branch=excluded.branch,
    target_branch=excluded.target_branch,
    session_id=excluded.session_id,
    kind=excluded.kind,
    backend=excluded.backend,
    scope=excluded.scope,
    issue_key=excluded.issue_key,
    job_id=excluded.job_id,
    working_directory=excluded.working_directory,
    forgotten_json=excluded.forgotten_json,
    reset_at=excluded.reset_at,
    forget_reason=excluded.forget_reason,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at,
    merge_request_url=excluded.merge_request_url,
    document=excluded.document
"""

_EXTRA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_binds_workdir ON session_binds(working_directory);
CREATE INDEX IF NOT EXISTS idx_binds_mr ON session_binds(merge_request_url);
"""


def default_index_path(binds_dir: Path) -> Path:
    return database_path(binds_dir)


def _text(rec: Dict[str, Any], key: str) -> str:
    return str(rec.get(key) or "").strip()


def row_params(rec: Dict[str, Any]) -> Dict[str, Any]:
    forgotten = rec.get("forgotten_session_ids") or []
    if not isinstance(forgotten, list):
        forgotten = []
    clean = [str(x).strip() for x in forgotten if str(x).strip()]
    return {
        "bind_id": _text(rec, "bind_id"),
        "repository_url": _text(rec, "repository_url"),
        "repository_key": _text(rec, "repository_key"),
        "branch": _text(rec, "branch"),
        "target_branch": _text(rec, "target_branch"),
        "session_id": _text(rec, "session_id"),
        "kind": _text(rec, "kind"),
        "backend": _text(rec, "backend"),
        "scope": _text(rec, "scope"),
        "issue_key": _text(rec, "issue_key").upper(),
        "job_id": _text(rec, "job_id"),
        "working_directory": _text(rec, "working_directory"),
        "forgotten_json": json.dumps(clean[-50:], ensure_ascii=False),
        "reset_at": _text(rec, "reset_at"),
        "forget_reason": _text(rec, "forget_reason"),
        "created_at": _text(rec, "created_at"),
        "updated_at": _text(rec, "updated_at"),
        "merge_request_url": _text(rec, "merge_request_url"),
        "document": dumps(rec),
    }


def row_to_bind(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    if "document" in keys:
        doc = loads(row["document"])
        if doc is not None:
            out = dict(doc)
            if not str(out.get("bind_id") or "").strip():
                out["bind_id"] = row["bind_id"]
            return out
    forgotten: List[str] = []
    try:
        parsed = json.loads(row["forgotten_json"] or "[]")
        if isinstance(parsed, list):
            forgotten = [str(x).strip() for x in parsed if str(x).strip()]
    except (TypeError, json.JSONDecodeError):
        forgotten = []
    rec: Dict[str, Any] = {
        "bind_id": row["bind_id"],
        "repository_url": row["repository_url"] or "",
        "repository_key": row["repository_key"] or "",
        "branch": row["branch"] or "",
        "target_branch": row["target_branch"] or "",
        "session_id": row["session_id"] or "",
        "kind": row["kind"] or "",
        "backend": row["backend"] or "",
        "scope": (row["scope"] if "scope" in row.keys() else "") or "",
        "issue_key": row["issue_key"] or "",
        "job_id": row["job_id"] or None,
        "working_directory": row["working_directory"] or None,
        "forgotten_session_ids": forgotten,
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }
    if not rec["job_id"]:
        rec["job_id"] = None
    if not rec["working_directory"]:
        rec["working_directory"] = None
    if row["reset_at"]:
        rec["reset_at"] = row["reset_at"]
    if row["forget_reason"]:
        rec["forget_reason"] = row["forget_reason"]
    return rec


class SessionBindIndex:
    """WAL SQLite for session binds. Safe for one daemon per data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        conn = connect(self.path)
        conn.executescript(_SCHEMA)
        ensure_column(conn, "session_binds", "backend", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "session_binds", "scope", "TEXT NOT NULL DEFAULT ''")
        ensure_column(
            conn, "session_binds", "merge_request_url", "TEXT NOT NULL DEFAULT ''"
        )
        ensure_column(conn, "session_binds", "document", "TEXT NOT NULL DEFAULT ''")
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
        if not params["bind_id"]:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(_UPSERT, params)
            self._conn.commit()

    def get(self, bind_id: str) -> Optional[Dict[str, Any]]:
        bid = (bind_id or "").strip()
        if not bid:
            return None
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM session_binds WHERE bind_id = ?", (bid,)
            ).fetchone()
        return row_to_bind(row) if row else None

    def delete(self, bind_id: str) -> bool:
        bid = (bind_id or "").strip()
        if not bid:
            return False
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "DELETE FROM session_binds WHERE bind_id = ?", (bid,)
            )
            changed = self._conn.execute("SELECT changes()").fetchone()
            self._conn.commit()
        return bool(changed and int(changed[0]) > 0)

    def list_live(self, *, limit: Optional[int] = 200) -> List[Dict[str, Any]]:
        sql = (
            "SELECT * FROM session_binds WHERE TRIM(session_id) != '' "
            "ORDER BY updated_at DESC, bind_id DESC"
        )
        args: List[Any] = []
        if limit is not None:
            sql += " LIMIT ?"
            args.append(max(1, int(limit)))
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, args).fetchall()
        return [row_to_bind(r) for r in rows]

    def newest_live_for_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        key = (issue_key or "").strip().upper()
        if not key:
            return None
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM session_binds WHERE issue_key = ? "
                "AND TRIM(session_id) != '' "
                "ORDER BY updated_at DESC, bind_id DESC LIMIT 1",
                (key,),
            ).fetchone()
        return row_to_bind(row) if row else None

    def newest_live(
        self, repository_key: str, branch: str, target_branch: str
    ) -> Optional[Dict[str, Any]]:
        if not repository_key or not branch or not target_branch:
            return None
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM session_binds WHERE repository_key = ? "
                "AND branch = ? AND target_branch = ? AND TRIM(session_id) != '' "
                "ORDER BY updated_at DESC, bind_id DESC LIMIT 1",
                (repository_key, branch, target_branch),
            ).fetchone()
        return row_to_bind(row) if row else None

    def rows_for_checkout(
        self, repository_key: str, branch: str, target_branch: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM session_binds WHERE repository_key = ? "
                "AND branch = ? AND target_branch = ?",
                (repository_key, branch, target_branch),
            ).fetchall()
        return [row_to_bind(r) for r in rows]

    def rows_with_directory(self) -> List[Dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(
                "SELECT * FROM session_binds WHERE TRIM(working_directory) != ''"
            ).fetchall()
        return [row_to_bind(r) for r in rows]

    def all_ids(self) -> set[str]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT bind_id FROM session_binds").fetchall()
        return {str(r["bind_id"]) for r in rows}

    def reconcile(self, binds_dir: Path) -> int:
        """Import leftover ``osb_*.json`` once. Later calls do not scan.

        A file that omits ``bind_id`` takes the filename. Rows already in
        the database stay when their file is already gone.
        """
        with self._lock:
            assert self._conn is not None
            try:
                return import_json_once(
                    self._conn,
                    binds_dir,
                    flag="binds_json_imported",
                    pattern="osb_*.json",
                    consume=self._consume_bind_file,
                )
            except OSError as exc:
                logger.warning(f"Session import glob failed: {exc}")
                raise

    def _consume_bind_file(self, path: Path) -> str:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning(f"Session import skip {path.name}: {exc}")
            return "bad"
        if not isinstance(raw, dict):
            logger.warning(f"Session import skip {path.name}: not an object")
            return "bad"
        rec = dict(raw)
        bid = str(rec.get("bind_id") or "").strip() or path.stem
        if not bid.startswith("osb_"):
            return "skip"
        rec["bind_id"] = bid
        self.upsert(rec)
        return "ok"
