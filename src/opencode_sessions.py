"""Lookup OpenCode session IDs from the local OpenCode SQLite store."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger


def _default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def path_contains_issue_key(directory: str, issue_key: str) -> bool:
    """True if *issue_key* appears as a path/folder token (not a bare substring).

    Matches real temp-clone names like ``repo_PROJ-1_20260101`` and rejects
    ``PROJ-10`` when looking for ``PROJ-1``.
    """
    if not directory or not issue_key:
        return False
    # Normalize slashes; match key bounded by start/end or separator chars
    d = directory.replace("\\", "/")
    key = issue_key.strip()
    if not key:
        return False
    pattern = re.compile(
        rf"(?:^|[/_.-]){re.escape(key)}(?:$|[/_.-])",
        re.IGNORECASE,
    )
    return bool(pattern.search(d))


def _rank_session(
    row: Dict[str, Any],
    *,
    issue_key: str,
    working_directory: Optional[str],
) -> tuple:
    """Lower sort key = better match (exact dir, then title prefix, then path)."""
    directory = row.get("directory") or ""
    title = row.get("title") or ""
    if working_directory and directory == working_directory:
        tier = 0
    elif title.startswith(f"{issue_key}:"):
        tier = 1
    elif path_contains_issue_key(directory, issue_key):
        tier = 2
    else:
        tier = 9
    # Newer first within tier (negate time_updated)
    t_upd = row.get("time_updated") or 0
    try:
        t_upd = int(t_upd)
    except (TypeError, ValueError):
        t_upd = 0
    return (tier, -t_upd)


def find_sessions_for_issue(
    issue_key: str,
    *,
    working_directory: Optional[Path] = None,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return OpenCode sessions related to an issue (best match first).

    Matching priority:
    1. Exact ``directory`` = working_directory (when provided)
    2. ``title`` starts with ``{issue_key}:`` (agent_runner --title format)
    3. Directory path contains the issue key as a bounded token
    """
    key = (issue_key or "").strip()
    if not key:
        return []

    path = db_path or _default_db_path()
    if not path.is_file():
        return []

    wd = str(working_directory.resolve()) if working_directory else None
    rows: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        # Broad SQL candidate set; precise filtering/ranking in Python
        # (SQL LIKE treats ``_`` as a single-char wildcard — unsafe for keys).
        title_prefix = f"{key}:%"
        if wd:
            cur.execute(
                """
                SELECT id, title, directory, agent, time_created, time_updated, cost,
                       tokens_input, tokens_output
                FROM session
                WHERE directory = ?
                   OR IFNULL(title, '') LIKE ?
                   OR IFNULL(directory, '') LIKE ?
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (wd, title_prefix, f"%{key}%", max(limit * 5, 50)),
            )
        else:
            cur.execute(
                """
                SELECT id, title, directory, agent, time_created, time_updated, cost,
                       tokens_input, tokens_output
                FROM session
                WHERE IFNULL(title, '') LIKE ?
                   OR IFNULL(directory, '') LIKE ?
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (title_prefix, f"%{key}%", max(limit * 5, 50)),
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

    filtered: List[Dict[str, Any]] = []
    for row in rows:
        directory = row.get("directory") or ""
        title = row.get("title") or ""
        if wd and directory == wd:
            filtered.append(row)
        elif title.startswith(f"{key}:"):
            filtered.append(row)
        elif path_contains_issue_key(directory, key):
            filtered.append(row)

    filtered.sort(key=lambda r: _rank_session(r, issue_key=key, working_directory=wd))
    return filtered[: max(1, int(limit))]


def resolve_session_id(
    issue_key: str,
    *,
    working_directory: Optional[Path] = None,
    preferred: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Pick best session id: preferred parse result, else newest DB match."""
    if preferred and str(preferred).startswith("ses_"):
        return preferred
    sessions = find_sessions_for_issue(
        issue_key,
        working_directory=working_directory,
        limit=1,
        db_path=db_path,
    )
    if sessions:
        return sessions[0]["id"]
    return preferred
