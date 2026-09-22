"""Local SQLite index of OpenCode session-bind JSON files (Sessions list).

One file per data dir, created on first open. JSON remains the full record.
The Sessions page, issue lookup, and workspace rollup read this table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_binds (
    bind_id TEXT PRIMARY KEY,
    repository_url TEXT NOT NULL DEFAULT '',
    repository_key TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    target_branch TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
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
    session_id, kind, issue_key, job_id, working_directory,
    forgotten_json, reset_at, forget_reason, created_at, updated_at
) VALUES (
    :bind_id, :repository_url, :repository_key, :branch, :target_branch,
    :session_id, :kind, :issue_key, :job_id, :working_directory,
    :forgotten_json, :reset_at, :forget_reason, :created_at, :updated_at
)
ON CONFLICT(bind_id) DO UPDATE SET
    repository_url=excluded.repository_url,
    repository_key=excluded.repository_key,
    branch=excluded.branch,
    target_branch=excluded.target_branch,
    session_id=excluded.session_id,
    kind=excluded.kind,
    issue_key=excluded.issue_key,
    job_id=excluded.job_id,
    working_directory=excluded.working_directory,
    forgotten_json=excluded.forgotten_json,
    reset_at=excluded.reset_at,
    forget_reason=excluded.forget_reason,
    created_at=excluded.created_at,
    updated_at=excluded.updated_at
"""


def default_index_path(binds_dir: Path) -> Path:
    return binds_dir.parent / "opencode-binds.sqlite"


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
        "issue_key": _text(rec, "issue_key").upper(),
        "job_id": _text(rec, "job_id"),
        "working_directory": _text(rec, "working_directory"),
        "forgotten_json": json.dumps(clean[-50:], ensure_ascii=False),
        "reset_at": _text(rec, "reset_at"),
        "forget_reason": _text(rec, "forget_reason"),
        "created_at": _text(rec, "created_at"),
        "updated_at": _text(rec, "updated_at"),
    }


def row_to_bind(row: sqlite3.Row) -> Dict[str, Any]:
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
    """WAL SQLite next to ``opencode-binds/``. Safe for one daemon per data dir."""

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
        params = row_params(rec)
        if not params["bind_id"]:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(_UPSERT, params)
            self._conn.commit()

    def delete(self, bind_id: str) -> None:
        bid = (bind_id or "").strip()
        if not bid:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(
                "DELETE FROM session_binds WHERE bind_id = ?", (bid,)
            )
            self._conn.commit()

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
        """Reload every bind JSON and drop index rows whose file is gone."""
        if not binds_dir.is_dir():
            return 0
        try:
            paths = list(binds_dir.glob("osb_*.json"))
        except OSError as e:
            logger.warning(f"Session index glob failed: {e}")
            return 0
        kept: set[str] = set()
        unread: set[str] = set()
        upserted = 0
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError) as e:
                logger.warning(f"Session index skip {path.name}: {e}")
                unread.add(path.stem)
                continue
            if not isinstance(raw, dict):
                unread.add(path.stem)
                continue
            rec = dict(raw)
            bid = str(rec.get("bind_id") or "").strip() or path.stem
            if not bid.startswith("osb_"):
                continue
            rec["bind_id"] = bid
            self.upsert(rec)
            kept.add(bid)
            upserted += 1
        for bid in self.all_ids() - kept - unread:
            self.delete(bid)
        return upserted
