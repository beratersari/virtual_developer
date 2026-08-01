"""Lookup OpenCode session IDs from the local OpenCode SQLite store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger


def _default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def find_sessions_for_issue(
    issue_key: str,
    *,
    working_directory: Optional[Path] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return OpenCode sessions related to an issue (newest first).

    Matching:
    - ``directory`` path contains the issue key (temp clones include it)
    - or ``title`` contains the issue key
    """
    key = (issue_key or "").strip()
    if not key:
        return []

    path = db_path or _default_db_path()
    if not path.is_file():
        return []

    like = f"%{key}%"
    wd = str(working_directory.resolve()) if working_directory else None
    rows: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        if wd:
            cur.execute(
                """
                SELECT id, title, directory, agent, time_created, time_updated, cost,
                       tokens_input, tokens_output
                FROM session
                WHERE directory = ? OR directory LIKE ? OR IFNULL(title, '') LIKE ?
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (wd, like, like, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, title, directory, agent, time_created, time_updated, cost,
                       tokens_input, tokens_output
                FROM session
                WHERE directory LIKE ? OR IFNULL(title, '') LIKE ?
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (like, like, limit),
            )
        for r in cur.fetchall():
            rows.append(
                {
                    "id": r["id"],
                    "title": r["title"],
                    "directory": r["directory"],
                    "agent": r["agent"],
                    "time_created": r["time_created"],
                    "time_updated": r["time_updated"],
                    "cost": r["cost"],
                    "tokens_input": r["tokens_input"],
                    "tokens_output": r["tokens_output"],
                }
            )
        con.close()
    except Exception as e:
        logger.debug(f"OpenCode session lookup failed for {key}: {e}")
        return []
    return rows


def resolve_session_id(
    issue_key: str,
    *,
    working_directory: Optional[Path] = None,
    preferred: Optional[str] = None,
) -> Optional[str]:
    """Pick best session id: preferred parse result, else newest DB match."""
    if preferred and str(preferred).startswith("ses_"):
        return preferred
    sessions = find_sessions_for_issue(
        issue_key, working_directory=working_directory, limit=1
    )
    if sessions:
        return sessions[0]["id"]
    return preferred
