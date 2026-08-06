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


# Patterns seen when OpenCode is mid-compaction or just finished compact without
# continuing the agent loop (headless `opencode run` exit-0 bug).
_COMPACT_OUTPUT_RE = re.compile(
    r"(?:"
    r"\bcompacting\b"
    r"|\bcompaction\b"
    r"|\bsession\s+compact(?:ed|ing)?\b"
    r"|\bauto[- ]?compact\b"
    r"|\bcontext\s+(?:automatically\s+)?compacted\b"
    r")",
    re.IGNORECASE,
)

# Finish values that mean the agent was still mid-tool-loop when the process
# stopped (not a clean terminal answer).
_UNFINISHED_FINISH = frozenset({"tool-calls", "unknown", ""})


def detect_compact_in_output(text: str) -> bool:
    """True if CLI transcript mentions an in-progress / just-finished compact."""
    if not text:
        return False
    return bool(_COMPACT_OUTPUT_RE.search(text))


def _apply_todo_counts(result: Dict[str, Any], todos: List[Dict[str, Any]]) -> None:
    """Merge open-todo signals from a list of {status: ...} rows."""
    by_status: Dict[str, int] = {}
    for row in todos:
        if not isinstance(row, dict):
            continue
        st = (row.get("status") or "").strip().lower()
        by_status[st] = by_status.get(st, 0) + 1
    pending = by_status.get("pending", 0)
    in_prog = by_status.get("in_progress", 0) + by_status.get("in-progress", 0)
    result["pending_todos"] = pending
    result["in_progress_todos"] = in_prog
    result["open_todos"] = pending + in_prog
    if result["open_todos"] > 0:
        result["reasons"].append(
            f"open todos: {pending} pending, {in_prog} in_progress"
        )


def _apply_last_assistant(
    result: Dict[str, Any],
    *,
    role: Any,
    finish: Any,
    summary: Any,
    parts: Optional[List[Any]] = None,
) -> None:
    """Record last-message signals used for premature-exit detection."""
    is_summary = summary is True or (
        isinstance(summary, dict) and bool(summary.get("compaction"))
    )
    if summary is True:
        is_summary = True
    # Compaction user part as last-ish signal when assistant summary follows
    if parts:
        if any(isinstance(p, dict) and p.get("type") == "compaction" for p in parts):
            # Not necessarily incomplete alone; summary assistant check below
            pass

    result["last_role"] = role
    result["last_finish"] = finish
    result["last_is_summary"] = bool(is_summary)

    if role == "assistant":
        finish_s = "" if finish is None else str(finish).strip().lower()
        if finish is None or finish_s in _UNFINISHED_FINISH:
            result["reasons"].append(
                f"last assistant finish is unfinished ({finish!r})"
            )
        elif is_summary and finish_s == "stop":
            result["reasons"].append(
                "session ended on compaction summary (finish=stop, summary=true)"
            )


def assess_session_completeness(
    session_id: Optional[str],
    *,
    output_text: str = "",
    db_path: Optional[Path] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    todos: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Decide whether an OpenCode session looks finished after a run/turn.

    Background (upstream + production):
    - ``opencode run`` can exit **0** after auto-compaction without continuing
      the task (anomalyco/opencode#13946, #3560). Transcripts often end on a
      "compacting" step; Virtual Developer previously treated that as success.
    - Sessions also die mid-turn (``finish`` null, only ``step-start``) while
      todos remain ``pending`` / ``in_progress`` — still exit 0 sometimes.

    Signals (any one can mark premature):
    1. Open todos (``pending`` / ``in_progress``) — API list or SQLite
    2. Last assistant message has no terminal ``finish`` (or is mid tool-calls)
    3. Last assistant is a compaction **summary** (``summary`` truthy) with
       finish ``stop`` — classic compact-then-exit without "Continue..."
    4. CLI output ends with compacting markers (when no stronger state signal)

    ``messages`` / ``todos``: optional live snapshots from ``opencode serve``
    HTTP (preferred when provided; skips SQLite for those fields).

    Returns a dict always including ``complete`` (bool) and ``reasons`` (list).
    """
    result: Dict[str, Any] = {
        "complete": True,
        "premature": False,
        "session_id": session_id,
        "reasons": [],
        "open_todos": 0,
        "pending_todos": 0,
        "in_progress_todos": 0,
        "last_finish": None,
        "last_role": None,
        "last_is_summary": False,
        "compact_in_output": detect_compact_in_output(output_text or ""),
        "db_checked": False,
        "api_checked": False,
    }

    # --- Live API snapshots (serve mode) ---
    if todos is not None:
        result["api_checked"] = True
        _apply_todo_counts(result, list(todos))

    if messages is not None:
        result["api_checked"] = True
        # Walk from the end to find the last assistant (or last message)
        last_assistant: Optional[Dict[str, Any]] = None
        last_any: Optional[Dict[str, Any]] = None
        for m in messages:
            if not isinstance(m, dict):
                continue
            last_any = m
            role = m.get("role")
            if role is None and isinstance(m.get("info"), dict):
                role = m["info"].get("role")
            if role == "assistant":
                last_assistant = m
        target = last_assistant or last_any
        if target is not None:
            info = target.get("info") if isinstance(target.get("info"), dict) else {}
            role = target.get("role") or info.get("role")
            finish = target.get("finish")
            if finish is None:
                finish = info.get("finish")
            summary = target.get("summary")
            if summary is None:
                summary = info.get("summary")
            parts = target.get("_parts") or target.get("parts") or []
            _apply_last_assistant(
                result,
                role=role,
                finish=finish,
                summary=summary,
                parts=parts if isinstance(parts, list) else None,
            )
            # If the absolute last message is a compaction *user* part with no
            # following assistant, treat as mid-compact incomplete.
            if last_any is not None and last_assistant is not last_any:
                parts_last = last_any.get("_parts") or last_any.get("parts") or []
                if any(
                    isinstance(p, dict) and p.get("type") == "compaction"
                    for p in parts_last
                ):
                    result["reasons"].append(
                        "session ended on compaction user part (no follow-up)"
                    )

    sid = (session_id or "").strip()
    path = db_path or _default_db_path()

    # --- SQLite fallback when API snapshots not supplied ---
    use_db_todos = todos is None
    use_db_messages = messages is None
    if sid and path.is_file() and (use_db_todos or use_db_messages):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            result["db_checked"] = True

            if use_db_todos:
                try:
                    todo_rows = cur.execute(
                        """
                        SELECT status, COUNT(*) AS c
                        FROM todo
                        WHERE session_id = ?
                        GROUP BY status
                        """,
                        (sid,),
                    ).fetchall()
                    flat: List[Dict[str, Any]] = []
                    for row in todo_rows:
                        st = (row["status"] or "").strip().lower()
                        for _ in range(int(row["c"] or 0)):
                            flat.append({"status": st})
                    _apply_todo_counts(result, flat)
                except sqlite3.Error as e:
                    logger.debug(f"todo completeness check skipped for {sid}: {e}")

            if use_db_messages:
                try:
                    last = cur.execute(
                        """
                        SELECT id, data
                        FROM message
                        WHERE session_id = ?
                        ORDER BY time_created DESC
                        LIMIT 1
                        """,
                        (sid,),
                    ).fetchone()
                    if last is not None:
                        import json

                        raw = last["data"]
                        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
                        if not isinstance(data, dict):
                            data = {}
                        info = (
                            data.get("info")
                            if isinstance(data.get("info"), dict)
                            else {}
                        )
                        role = data.get("role") or info.get("role")
                        finish = data.get("finish")
                        if finish is None:
                            finish = info.get("finish")
                        summary = data.get("summary")
                        if summary is None:
                            summary = info.get("summary")
                        _apply_last_assistant(
                            result, role=role, finish=finish, summary=summary
                        )
                except (sqlite3.Error, ValueError, TypeError) as e:
                    logger.debug(
                        f"message completeness check skipped for {sid}: {e}"
                    )

            con.close()
        except Exception as e:
            logger.debug(f"OpenCode completeness lookup failed for {sid}: {e}")

    # Output-only signal: compacting was the last notable activity and we have
    # no contradictory "all clear" from state.
    if result["compact_in_output"]:
        tail = (output_text or "")[-2048:]
        if detect_compact_in_output(tail):
            clean = (
                (result["db_checked"] or result["api_checked"])
                and result["open_todos"] == 0
                and (result["last_finish"] or "").lower() == "stop"
                and not result["last_is_summary"]
            )
            if not clean:
                result["reasons"].append(
                    "CLI output indicates compaction near end of run"
                )

    if result["reasons"]:
        result["complete"] = False
        result["premature"] = True
    return result
