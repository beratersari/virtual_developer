"""Lookup OpenCode session IDs from the local OpenCode SQLite store."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger

_SESSION_SELECT = (
    "id, title, directory, agent, time_created, time_updated, cost, "
    "tokens_input, tokens_output"
)

_SES_ID_RE = re.compile(r"(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)")
_SES_LABELED_RE = re.compile(
    r"(?:session created:|session resumed:|Session(?:\s*ID)?[:\s]+)"
    r"\s*(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)",
    re.IGNORECASE,
)


def extract_session_ids_from_text(text: str) -> List[str]:
    """ses_* ids from a session log (prefer labeled serve lines)."""
    if not text:
        return []
    labeled = _SES_LABELED_RE.findall(text)
    if labeled:
        out: List[str] = []
        for sid in labeled:
            if sid not in out:
                out.append(sid)
        return out
    out = []
    for match in _SES_ID_RE.finditer(text):
        sid = match.group(1)
        if sid not in out:
            out.append(sid)
    return out


def _default_db_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "opencode.db"


def paths_equivalent(left: Any, right: Any) -> bool:
    """True when two filesystem paths resolve to the same location."""
    if left is None or right is None:
        return False
    try:
        a = str(left).strip()
        b = str(right).strip()
    except Exception:
        return False
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _like_escape(value: str) -> str:
    """Escape ``LIKE`` wildcards so issue keys with ``_`` match literally."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _title_matches_issue(title: str, issue_key: str) -> bool:
    t = (title or "").strip()
    key = (issue_key or "").strip()
    if not t or not key:
        return False
    return t.lower().startswith(f"{key.lower()}:")


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
    if working_directory and paths_equivalent(directory, working_directory):
        tier = 0
    elif _title_matches_issue(title, issue_key):
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

    wd: Optional[str] = None
    wd_variants: List[str] = []
    if working_directory:
        raw = str(working_directory)
        wd_variants.append(raw)
        try:
            wd = str(working_directory.resolve())
            if wd not in wd_variants:
                wd_variants.append(wd)
        except OSError:
            wd = raw

    rows: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _take(fetched: Any) -> None:
        for r in fetched:
            sid = r["id"]
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            rows.append(
                {
                    "id": sid,
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

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        # Precise queries first so a flood of PROJ-10 substring hits cannot
        # crowd PROJ-1 out of a small LIKE window. Escape LIKE wildcards.
        title_like = f"{_like_escape(key)}:%"
        dir_like = f"%{_like_escape(key)}%"
        if wd_variants:
            placeholders = ",".join("?" * len(wd_variants))
            cur.execute(
                f"""
                SELECT {_SESSION_SELECT}
                FROM session
                WHERE directory IN ({placeholders})
                ORDER BY time_updated DESC
                """,
                tuple(wd_variants),
            )
            _take(cur.fetchall())
        cur.execute(
            f"""
            SELECT {_SESSION_SELECT}
            FROM session
            WHERE LOWER(IFNULL(title, '')) LIKE LOWER(?) ESCAPE '\\'
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (title_like, max(int(limit) * 10, 100)),
        )
        _take(cur.fetchall())
        cur.execute(
            f"""
            SELECT {_SESSION_SELECT}
            FROM session
            WHERE IFNULL(directory, '') LIKE ? ESCAPE '\\'
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (dir_like, max(int(limit) * 50, 500)),
        )
        _take(cur.fetchall())
        con.close()
    except Exception as e:
        logger.debug(f"OpenCode session lookup failed for {key}: {e}")
        return []

    filtered: List[Dict[str, Any]] = []
    for row in rows:
        directory = row.get("directory") or ""
        title = row.get("title") or ""
        if wd and paths_equivalent(directory, wd):
            filtered.append(row)
        elif _title_matches_issue(title, key):
            filtered.append(row)
        elif path_contains_issue_key(directory, key):
            filtered.append(row)

    filtered.sort(key=lambda r: _rank_session(r, issue_key=key, working_directory=wd))
    return filtered[: max(1, int(limit))]


def find_sessions_for_directory(
    working_directory: Any,
    *,
    limit: int = 20,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Sessions whose ``directory`` is this clone path (newest first)."""
    if working_directory is None:
        return []
    try:
        raw = str(working_directory).strip()
    except Exception:
        return []
    if not raw:
        return []
    variants: List[str] = [raw]
    try:
        resolved = str(Path(raw).resolve())
        if resolved not in variants:
            variants.append(resolved)
    except OSError:
        pass
    path = db_path or _default_db_path()
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        placeholders = ",".join("?" * len(variants))
        rows = cur.execute(
            f"""
            SELECT {_SESSION_SELECT}
            FROM session
            WHERE directory IN ({placeholders})
            ORDER BY time_updated DESC
            LIMIT ?
            """,
            (*variants, max(1, int(limit))),
        ).fetchall()
        if not rows:
            # Windows / slash / casing: SQL IN misses, resolve-compare instead.
            rows = cur.execute(
                f"""
                SELECT {_SESSION_SELECT}
                FROM session
                ORDER BY time_updated DESC
                LIMIT ?
                """,
                (max(80, int(limit) * 4),),
            ).fetchall()
        con.close()
    except Exception as e:
        logger.debug(f"OpenCode directory session lookup failed: {e}")
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        sid = r["id"]
        directory = r["directory"] or ""
        if not sid or sid in seen:
            continue
        if not any(paths_equivalent(directory, v) for v in variants):
            continue
        seen.add(sid)
        out.append(
            {
                "id": sid,
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
    return out


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


def lookup_session_directory(
    session_id: str,
    *,
    db_path: Optional[Path] = None,
) -> tuple[Optional[str], bool]:
    """Return ``(directory, ok)`` for an OpenCode session id.

    * ``ok=False`` — DB missing-as-unreadable query failed (transient).
    * ``ok=True`` and ``directory is None`` — id is absent (or empty).
    * ``ok=True`` and ``directory`` set — found.
    """
    sid = (session_id or "").strip()
    if not sid:
        return None, True
    path = db_path or _default_db_path()
    if not path.is_file():
        # No OpenCode DB yet — treat as "not found", not a read error.
        return None, True
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT directory FROM session WHERE id = ? LIMIT 1", (sid,))
        row = cur.fetchone()
        con.close()
    except Exception as e:
        logger.debug(f"OpenCode session dir lookup failed for {sid}: {e}")
        return None, False
    if not row:
        return None, True
    d = row[0]
    # Empty string: row exists but directory was never stored.
    # None: no row. Callers must not treat those the same.
    if d is None or str(d).strip() == "":
        return "", True
    return str(d), True


def get_session_directory(
    session_id: str,
    *,
    db_path: Optional[Path] = None,
) -> Optional[str]:
    """Return OpenCode ``session.directory`` for this id, or None."""
    directory, ok = lookup_session_directory(session_id, db_path=db_path)
    return directory if ok else None


def relocate_session_directories(
    old_dir: Any,
    new_dir: Any,
    *,
    db_path: Optional[Path] = None,
) -> int:
    """Rewrite ``session.directory`` after a clone folder was renamed in place.

    OpenCode serve requires the stored directory to match the live clone
    path. A MAX_PATH short-folder rename would otherwise force a cold start
    (and can hang if we resume against the old string).
    """
    path = db_path or _default_db_path()
    if not path.is_file():
        return 0
    try:
        old_r = Path(old_dir).resolve()
        new_s = str(Path(new_dir).resolve())
    except (OSError, TypeError):
        return 0
    if not new_s or paths_equivalent(old_r, new_s):
        return 0
    try:
        con = sqlite3.connect(str(path), timeout=1.0)
        cur = con.cursor()
        rows = cur.execute("SELECT id, directory FROM session").fetchall()
        n = 0
        for sid, directory in rows:
            if not directory or not paths_equivalent(directory, old_r):
                continue
            cur.execute(
                "UPDATE session SET directory = ? WHERE id = ?",
                (new_s, sid),
            )
            n += 1
        con.commit()
        con.close()
        if n:
            logger.info(
                f"Relocated {n} OpenCode session directory(ies) "
                f"{old_r} → {new_s}"
            )
        return n
    except Exception as e:
        logger.warning(f"Could not relocate OpenCode session directories: {e}")
        return 0


def session_matches_workdir(
    session_id: str,
    working_directory: Optional[Path],
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """True when OpenCode stored this session under *working_directory*.

    Resuming a session on a *new* temp clone hangs or no-ops; only resume
    when the clone path still matches.
    """
    if not working_directory:
        return False
    stored, ok = lookup_session_directory(session_id, db_path=db_path)
    if not ok or not stored:
        return False
    return paths_equivalent(stored, working_directory)


_MAX_CHAT_MESSAGES = 2000
_MAX_CHAT_PARTS = 20_000
_MAX_CHAT_PART_TEXT = 32_000


def _coerce_epoch_ms(raw: Any) -> Optional[int]:
    """Normalize OpenCode epoch seconds or milliseconds to milliseconds."""
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 10_000_000_000:  # already ms
        return int(n)
    return int(n * 1000.0)


def _epoch_to_iso(raw: Any) -> Optional[str]:
    """OpenCode stores epoch ms (sometimes seconds) on message/part rows."""
    ms = _coerce_epoch_ms(raw)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _cap_text(value: Any, *, limit: int = _MAX_CHAT_PART_TEXT) -> tuple[str, bool]:
    if value is None:
        return "", False
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            value = str(value)
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n…(truncated)", True


def _normalize_part(raw: Any, *, part_id: str = "", created_at: Any = None) -> Optional[Dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    ptype_raw = str(raw.get("type") or "unknown")
    ptype = ptype_raw.lower()
    time_obj = raw.get("time") if isinstance(raw.get("time"), dict) else {}
    out: Dict[str, Any] = {
        "id": part_id or raw.get("id") or "",
        "type": ptype,
        "created_at": _epoch_to_iso(created_at or time_obj.get("start")),
    }
    if ptype == "text":
        text, trunc = _cap_text(raw.get("text") or "")
        out["text"] = text
        out["truncated"] = trunc
    elif ptype in {"reasoning", "thinking"}:
        text, trunc = _cap_text(raw.get("text") or "")
        out["type"] = "reasoning"
        out["text"] = text
        out["truncated"] = trunc
    elif ptype == "tool":
        state = raw.get("state") if isinstance(raw.get("state"), dict) else {}
        inp = state.get("input")
        out["tool"] = raw.get("tool") or state.get("title") or "tool"
        out["call_id"] = raw.get("callID") or raw.get("call_id")
        out["status"] = state.get("status") or raw.get("status") or ""
        out["title"] = state.get("title") or ""
        if isinstance(inp, dict):
            out["input"] = {
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in list(inp.items())[:20]
            }
        elif inp is not None:
            out["input"] = {"value": str(inp)[:2000]}
        else:
            out["input"] = {}
        output, trunc = _cap_text(state.get("output") or raw.get("output") or "")
        out["output"] = output
        out["truncated"] = trunc
    elif ptype in {"compaction", "compact"}:
        out["type"] = "compaction"
        out["text"] = "Session compacted"
        out["auto"] = bool(raw.get("auto", True) if "auto" in raw else True)
    elif ptype in {"step-start", "step-finish"}:
        out["reason"] = raw.get("reason") or ""
    else:
        text, trunc = _cap_text(raw.get("text") or raw.get("content") or "")
        if text:
            out["text"] = text
            out["truncated"] = trunc
    return out


_CONTINUE_PROMPT_PREFIX = "continue the previous opencode session"
_FINISH_TODOS_PREFIX = "finish remaining todos and complete the original task"
_CONTINUE_AFTER_COMPACT_PREFIX = "continue after context compaction"
_OPENCODE_AUTO_CONTINUE_PREFIX = "continue if you have next steps"
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_COMPACT_USER_TEXT_RE = re.compile(
    r"(?:"
    r"session\s+compacted"
    r"|compacting\s+session"
    r"|compaction\s+summary"
    r"|context\s+(?:was\s+)?compacted"
    r"|continue after context compaction"
    r"|auto[- ]?compact(?:ed|ing)?"
    r")",
    re.IGNORECASE,
)
# OpenCode / oh-my-openagent post these as role=user. They are not the operator.
# Do NOT match OMO_INTERNAL / [search-mode] on the raw body: those tags are
# appended to the *first* real task prompt too, and hiding them emptied chat
# when continuing an older session.
_INTERNAL_COMPACT_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"restore\s+checkpointed\s+session"
    r"|checkpointed\s+session\s+agent\s+configuration"
    r"|todo\s+continuation"
    r"|background task completed"
    r"|system directive:\s*oh-my-opencode"
    r"|system-reminder"
    r"|continue if you have next steps"
    r"|continue after context compaction"
    r"|exceeded the provider's size limit"
    r"|conversation was compacted"
    r"|media (?:files|attachments) were removed"
    r"|large media attachments"
    r")",
    re.IGNORECASE,
)
_OMO_MODE_WRAP_RE = re.compile(
    r"\[(?:search|analyze|build|plan)-mode\]|maximize search effort",
    re.IGNORECASE,
)
_OMO_MODE_LINE_RE = re.compile(
    r"^\[(?:search|analyze|build|plan)-mode\]",
    re.IGNORECASE,
)
_ASSISTANT_QUESTION_RE = re.compile(
    r"(?:"
    r"shall i (?:continue|proceed|keep going)"
    r"|should i (?:continue|proceed|keep going)"
    r"|do you want me to"
    r"|want me to (?:continue|proceed|keep)"
    r"|continue with the remaining"
    r"|please (?:confirm|advise|let me know)"
    r")",
    re.IGNORECASE,
)


def strip_internal_markup(text: str) -> str:
    """Drop HTML comments / OMO_INTERNAL tags so real task text remains."""
    cleaned = _HTML_COMMENT_RE.sub("", text or "")
    cleaned = re.sub(r"OMO_INTERNAL\w*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_orchestrator_continue_text(text: str) -> bool:
    low = strip_internal_markup(text or "").lower()
    return (
        low.startswith(_CONTINUE_PROMPT_PREFIX)
        or low.startswith(_FINISH_TODOS_PREFIX)
        or low.startswith(_CONTINUE_AFTER_COMPACT_PREFIX)
    )


def is_internal_compact_followup_text(text: str) -> bool:
    """True for OpenCode/oh-my-openagent post-compact synthetic user turns.

    Real operator / subagent prompts often end with
    ``<!-- OMO_INTERNAL_INITIATOR -->``. That tag alone must not hide them.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    cleaned = strip_internal_markup(raw)
    if not cleaned:
        return True
    low = cleaned.lower()
    if low.startswith(_OPENCODE_AUTO_CONTINUE_PREFIX):
        return True
    if low.startswith(_FINISH_TODOS_PREFIX):
        return True
    if low.startswith(_CONTINUE_AFTER_COMPACT_PREFIX):
        return True
    if low.startswith("[restore checkpointed"):
        return True
    if low.startswith("the previous request exceeded"):
        return True
    # Synthetic compact follow-ups are short. A long TASK / BUILD kit that
    # merely mentions "compact" or a notepad path is the operator's message.
    if len(cleaned) < 400 and _INTERNAL_COMPACT_FOLLOWUP_RE.search(cleaned):
        return True
    return False


def is_omo_mode_wrap_text(text: str) -> bool:
    """True when oh-my-openagent wrapped a user turn with [search-mode] etc."""
    return bool(_OMO_MODE_WRAP_RE.search(text or ""))


def strip_omo_mode_wrap(text: str) -> str:
    """Drop leading [search-mode]/[analyze-mode] banners; keep the real prompt."""
    raw = text or ""
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if _OMO_MODE_LINE_RE.match(s):
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if _OMO_MODE_LINE_RE.match(nxt):
                    break
                if nxt.startswith("#"):
                    return "\n".join(lines[i:]).strip() or raw
                i += 1
            continue
        if s:
            break
        i += 1
    stripped = "\n".join(lines[i:]).strip()
    return stripped or raw


def assistant_asked_question(text: str) -> bool:
    """True when the last assistant turn is waiting on the operator."""
    t = (text or "").strip()
    if not t:
        return False
    if _ASSISTANT_QUESTION_RE.search(t):
        return True
    tail = t[-500:]
    if "?" not in tail:
        return False
    low = tail.lower()
    return any(
        p in low
        for p in (
            "shall i",
            "should i",
            "continue",
            "proceed",
            "confirm",
            "let me know",
        )
    )


def _message_text(msg: Optional[Dict[str, Any]]) -> str:
    if not isinstance(msg, dict):
        return ""
    parts = msg.get("_parts") or msg.get("parts") or []
    bits = [
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and (p.get("type") or "text").lower() == "text"
    ]
    if bits:
        return "\n".join(bits)
    info = msg.get("info") if isinstance(msg.get("info"), dict) else {}
    return str(msg.get("text") or info.get("text") or "")


def chat_display_role(
    role: Any,
    *,
    agent: Any = None,
    summary: Any = None,
    parts: Optional[List[Any]] = None,
) -> str:
    """Map an OpenCode message to a dashboard role.

    Compaction is stored as ``role=user`` in the OpenCode DB. Those rows must
    not render as "You". Orchestrator Continue prompts are skipped.
    The compact *summary* (``agent=compaction``, ``## Objective``) is the
    previous-turn recap — show it as ``summary``, not as the operator.
    """
    items = [p for p in (parts or []) if isinstance(p, dict)]
    types = {(p.get("type") or "").lower() for p in items}
    text = "\n".join(
        str(p.get("text") or "")
        for p in items
        if (p.get("type") or "text").lower() == "text"
    )
    if is_orchestrator_continue_text(text) or is_internal_compact_followup_text(text):
        return "skip"
    if "compaction" in types or "compact" in types:
        return "compaction"
    is_compact_agent = str(agent or "").strip().lower() == "compaction"
    if is_compact_agent or _is_compaction_summary(summary):
        if strip_internal_markup(text):
            return "summary"
        return "compaction"
    raw = str(role or "unknown").strip().lower() or "unknown"
    cleaned = strip_internal_markup(text)
    if raw == "user" and cleaned and len(cleaned) < 400:
        if _COMPACT_USER_TEXT_RE.search(cleaned):
            return "compaction"
    return raw


def list_session_chat(
    session_id: str,
    *,
    db_path: Optional[Path] = None,
    limit: int = _MAX_CHAT_MESSAGES,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
    restart_wraps_at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Load OpenCode chat (messages + parts) for a session id.

    Parts live in a separate ``part`` table in current OpenCode DBs. Older
    snapshots may embed ``parts`` on the message JSON — both are accepted.

    Continuing a session must keep the full transcript. ``restart_wraps_at_ms``
    (this job's start) only resets the "[search-mode] show once" rule so the
    new run's prompt is not dropped as a retry. ``since_ms`` / ``until_ms``
    remain optional filters for callers that need a slice.
    """
    sid = (session_id or "").strip()
    result: Dict[str, Any] = {
        "session_id": sid,
        "title": None,
        "directory": None,
        "messages": [],
        "db_checked": False,
        "error": None,
        "truncated": False,
    }
    if not sid:
        result["error"] = "missing session id"
        return result
    path = db_path or _default_db_path()
    if not path.is_file():
        result["error"] = "OpenCode session database not found"
        return result
    cap = max(1, min(int(limit or _MAX_CHAT_MESSAGES), _MAX_CHAT_MESSAGES))
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        result["db_checked"] = True
        try:
            srow = cur.execute(
                "SELECT title, directory FROM session WHERE id = ? LIMIT 1",
                (sid,),
            ).fetchone()
            if srow is not None:
                result["title"] = srow["title"]
                result["directory"] = srow["directory"]
        except sqlite3.Error:
            pass

        msg_rows = cur.execute(
            """
            SELECT id, time_created, data
            FROM message
            WHERE session_id = ?
            ORDER BY time_created ASC
            LIMIT ?
            """,
            (sid, max(cap + 1, 10_000)),
        ).fetchall()
        if since_ms is not None or until_ms is not None:
            scoped: List[Any] = []
            for row in msg_rows:
                ms = _coerce_epoch_ms(row["time_created"]) or 0
                if since_ms is not None and ms < int(since_ms):
                    continue
                if until_ms is not None and ms > int(until_ms):
                    continue
                scoped.append(row)
            msg_rows = scoped
        if len(msg_rows) > cap:
            result["truncated"] = True
            if restart_wraps_at_ms is not None:
                # Full continued transcript (fetch already capped at 10k).
                pass
            elif since_ms is not None:
                msg_rows = msg_rows[-cap:]
            else:
                msg_rows = msg_rows[:cap]

        parts_by_msg: Dict[str, List[Dict[str, Any]]] = {r["id"]: [] for r in msg_rows}
        if msg_rows:
            try:
                part_rows = cur.execute(
                    """
                    SELECT id, message_id, time_created, data
                    FROM part
                    WHERE session_id = ?
                    ORDER BY time_created ASC
                    LIMIT ?
                    """,
                    (sid, _MAX_CHAT_PARTS),
                ).fetchall()
                for prow in part_rows:
                    mid = prow["message_id"]
                    if mid not in parts_by_msg:
                        continue
                    parsed = _normalize_part(
                        prow["data"],
                        part_id=prow["id"] or "",
                        created_at=prow["time_created"],
                    )
                    if parsed:
                        parts_by_msg[mid].append(parsed)
            except sqlite3.Error as e:
                logger.debug(f"part table unavailable for {sid}: {e}")

        messages: List[Dict[str, Any]] = []
        shown_user = False
        wraps_restarted = restart_wraps_at_ms is None
        for row in msg_rows:
            raw = row["data"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if not isinstance(data, dict):
                data = {}
            info = data.get("info") if isinstance(data.get("info"), dict) else {}
            role = data.get("role") or info.get("role") or "unknown"
            finish = data.get("finish")
            if finish is None:
                finish = info.get("finish")
            summary = data.get("summary")
            if summary is None:
                summary = info.get("summary")
            agent = data.get("agent") or info.get("agent") or data.get("mode")
            time_obj = data.get("time") if isinstance(data.get("time"), dict) else {}
            created_at = _epoch_to_iso(
                row["time_created"] or time_obj.get("created") or info.get("time")
            )
            parts = list(parts_by_msg.get(row["id"]) or [])
            if not parts:
                embedded = data.get("parts") or data.get("_parts") or info.get("parts") or []
                if isinstance(embedded, list):
                    for i, ep in enumerate(embedded):
                        parsed = _normalize_part(ep, part_id=f"{row['id']}:p{i}")
                        if parsed:
                            parts.append(parsed)
            display_role = chat_display_role(
                role, agent=agent, summary=summary, parts=parts
            )
            text_blob = "\n".join(
                str(p.get("text") or "")
                for p in parts
                if isinstance(p, dict) and (p.get("type") or "text").lower() == "text"
            )
            if display_role == "skip":
                continue
            row_ms = _coerce_epoch_ms(row["time_created"])
            if (
                restart_wraps_at_ms is not None
                and not wraps_restarted
                and row_ms is not None
                and row_ms >= int(restart_wraps_at_ms)
            ):
                # New job resumed this session — allow this run's prompt
                # even when it is the same [search-mode] kit as last time.
                shown_user = False
                wraps_restarted = True
            # Plugin wraps the first task prompt with [search-mode]. Show that
            # once per run; later wraps in the same run are retries.
            if (
                display_role == "user"
                and shown_user
                and is_omo_mode_wrap_text(text_blob)
            ):
                continue
            if display_role in {"user", "summary"}:
                for p in parts:
                    if isinstance(p, dict) and (p.get("type") or "text").lower() == "text":
                        cleaned = strip_internal_markup(str(p.get("text") or ""))
                        if display_role == "user" and is_omo_mode_wrap_text(cleaned):
                            cleaned = strip_omo_mode_wrap(cleaned)
                        p["text"] = cleaned
            if display_role == "user":
                shown_user = True
            messages.append(
                {
                    "id": row["id"],
                    "session_id": sid,
                    "role": display_role,
                    "raw_role": str(role),
                    "finish": finish,
                    "summary": bool(_is_compaction_summary(summary)),
                    "agent": agent,
                    "created_at": created_at,
                    "parts": parts,
                }
            )
        result["messages"] = messages
        con.close()
    except Exception as e:
        logger.debug(f"OpenCode chat load failed for {sid}: {e}")
        result["error"] = str(e)
    return result


# Patterns seen when OpenCode is mid-compaction or just finished compact without
# continuing the agent loop.
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

# Our Continue prompt mentions "context compaction" — that is not a live compact.
_CONTINUE_PROMPT_NOISE_RE = re.compile(
    r"(?:"
    r"last turn stopped early\s*\(\s*context compaction"
    r"|Continue the previous OpenCode session"
    r"|\[serve\] incomplete \(likely compact"
    r"|\[INCOMPLETE\].*auto-compaction"
    r")",
    re.IGNORECASE,
)

# Finish values that mean the agent was still mid-tool-loop when the process
# stopped (not a clean terminal answer).
_UNFINISHED_FINISH = frozenset({"tool-calls", "unknown", ""})


def detect_compact_in_output(text: str) -> bool:
    """True if the session log mentions an in-progress / just-finished compact."""
    if not text:
        return False
    return bool(_COMPACT_OUTPUT_RE.search(text))


_COMPACT_REASON_MARKERS = (
    "compact-then-stop",
    "compaction summary",
    "compaction user part",
    "compaction near end",
    "compaction occurred this turn",
    "cli output indicates compaction",
)
# Safe to drop after we waited for auto-compact. Compact-then-stop / summary
# / user-part mean the session still ended on compact — that is incomplete.
_TRANSIENT_COMPACT_REASON_MARKERS = (
    "compaction near end",
    "compaction occurred this turn",
    "cli output indicates compaction",
)


def is_compact_reason(reason: Any) -> bool:
    """True when an assessment reason is only about auto-compaction."""
    s = str(reason or "").lower()
    return any(m in s for m in _COMPACT_REASON_MARKERS)


def is_transient_compact_reason(reason: Any) -> bool:
    """True for compact log noise that wait-then-reassess may clear."""
    s = str(reason or "").lower()
    return any(m in s for m in _TRANSIENT_COMPACT_REASON_MARKERS)


def reasons_are_compact_only(reasons: Optional[List[Any]]) -> bool:
    """True when every incompleteness reason is a compact/idle marker."""
    items = [r for r in (reasons or []) if str(r).strip()]
    if not items:
        return False
    return all(is_compact_reason(r) for r in items)


def compact_related_reasons(reasons: Optional[List[Any]]) -> bool:
    """True when any reason is about auto-compaction (not a hard crash)."""
    for reason in reasons or []:
        if is_compact_reason(reason) or "compact" in str(reason or "").lower():
            return True
    return False


def strip_compact_reasons(result: Dict[str, Any]) -> Dict[str, Any]:
    """Drop transient compact *log* flags after we waited for auto-compact.

    Compact-then-stop / summary-stop / compaction user part stay: those mean
    the session still ended on compact (opencode#13946 false success).
    """
    kept = [
        r
        for r in (result.get("reasons") or [])
        if not is_transient_compact_reason(r)
    ]
    result["reasons"] = kept
    if kept:
        result["complete"] = False
        result["premature"] = True
    else:
        result["complete"] = True
        result["premature"] = False
    return result


def compact_output_indicates_premature_exit(text: str, *, last_lines: int = 12) -> bool:
    """True when the *end* of a CLI run is a compact event, not a finished answer.

    A 2k-char tail match is too broad: the Continue prompt (and echoed user
    text) contains the word ``compaction``, which previously marked successful
    resume turns as incomplete / error.
    """
    if not text:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    tail = lines[-max(1, int(last_lines or 12)) :]
    saw_compact = False
    for ln in tail:
        if _CONTINUE_PROMPT_NOISE_RE.search(ln):
            continue
        if _COMPACT_OUTPUT_RE.search(ln):
            saw_compact = True
    return saw_compact


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


def _is_compaction_summary(summary: Any) -> bool:
    """True for OpenCode compaction summary flags (bool or ``{compaction: …}``)."""
    if summary is True:
        return True
    if isinstance(summary, dict) and summary.get("compaction"):
        return True
    return False


def _message_has_compaction_part(msg: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(msg, dict):
        return False
    parts = msg.get("_parts") or msg.get("parts") or []
    return any(isinstance(p, dict) and p.get("type") == "compaction" for p in parts)


def _apply_last_assistant(
    result: Dict[str, Any],
    *,
    role: Any,
    finish: Any,
    summary: Any,
    parts: Optional[List[Any]] = None,
) -> None:
    """Record last-message signals used for premature-exit detection."""
    is_summary = _is_compaction_summary(summary)
    if parts:
        if any(isinstance(p, dict) and p.get("type") == "compaction" for p in parts):
            # Not necessarily incomplete alone; summary assistant check below
            pass

    result["last_role"] = role
    result["last_finish"] = finish
    result["last_is_summary"] = bool(is_summary)

    if role == "assistant":
        finish_s = "" if finish is None else str(finish).strip().lower()
        asked = bool(result.get("assistant_asked_question"))
        if asked:
            # Clarifying question is a clean stop, not a crashed turn.
            pass
        elif finish is None or finish_s in _UNFINISHED_FINISH:
            result["reasons"].append(
                f"last assistant finish is unfinished ({finish!r})"
            )
        elif is_summary and finish_s == "stop":
            result["reasons"].append(
                "session ended on compaction summary (finish=stop, summary=true)"
            )


def _apply_message_list(
    result: Dict[str, Any], messages: List[Dict[str, Any]]
) -> None:
    """Walk a chronological message list for premature-exit signals."""
    indexed: List[Dict[str, Any]] = [m for m in messages if isinstance(m, dict)]
    last_assistant: Optional[Dict[str, Any]] = None
    last_assistant_idx: Optional[int] = None
    last_any: Optional[Dict[str, Any]] = None
    for i, m in enumerate(indexed):
        last_any = m
        role = m.get("role")
        if role is None and isinstance(m.get("info"), dict):
            role = m["info"].get("role")
        if role == "assistant":
            last_assistant = m
            last_assistant_idx = i
    target = last_assistant or last_any
    if target is None:
        return
    info = target.get("info") if isinstance(target.get("info"), dict) else {}
    role = target.get("role") or info.get("role")
    finish = target.get("finish")
    if finish is None:
        finish = info.get("finish")
    summary = target.get("summary")
    if summary is None:
        summary = info.get("summary")
    parts = target.get("_parts") or target.get("parts") or []
    if last_assistant is not None and assistant_asked_question(
        _message_text(last_assistant)
    ):
        result["assistant_asked_question"] = True
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
        if _message_has_compaction_part(last_any):
            result["reasons"].append(
                "session ended on compaction user part (no follow-up)"
            )
    # Compact-then-stop: last assistant immediately follows a compaction
    # *user part* (classic auto-compact exit that still looks like finish=stop).
    # Do **not** treat "summary assistant then more work" as premature — that is
    # the agent continuing after compaction in the same turn (success).
    if last_assistant_idx is not None and last_assistant_idx > 0:
        prev = indexed[last_assistant_idx - 1]
        if _message_has_compaction_part(prev):
            result["reasons"].append(
                "last assistant followed a compaction message "
                "(compact-then-stop)"
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
    - A turn can look finished after auto-compaction without the task
      continuing (anomalyco/opencode#13946, #3560). Transcripts often end on
      a "compacting" step; Virtual Developer previously treated that as success.
    - Sessions also die mid-turn (``finish`` null, only ``step-start``) while
      todos remain ``pending`` / ``in_progress`` — still exit 0 sometimes.

    Signals (any one can mark premature):
    1. Open todos (``pending`` / ``in_progress``) — API list or SQLite
    2. Last assistant message has no terminal ``finish`` (or is mid tool-calls)
    3. Last assistant is a compaction **summary** (``summary`` truthy) with
       finish ``stop`` — classic compact-then-exit without "Continue..."
    4. Session log ends with compacting markers (when no stronger state signal)

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
        "assistant_asked_question": False,
        "compact_in_output": detect_compact_in_output(output_text or ""),
        "db_checked": False,
        "api_checked": False,
    }

    # --- Live API snapshots (serve mode) ---
    # An empty list is *not* "API checked, nothing to see": list failures and
    # wrapped/non-list payloads used to arrive as [] and skip SQLite, which
    # then defaulted to complete=True (false COMPLETE after compact).
    api_todos = list(todos) if todos else []
    api_messages = list(messages) if messages else []
    if todos:
        result["api_checked"] = True
        _apply_todo_counts(result, api_todos)

    if api_messages:
        result["api_checked"] = True
        _apply_message_list(result, api_messages)

    sid = (session_id or "").strip()
    path = db_path or _default_db_path()

    # --- SQLite fallback when API snapshots not supplied *or* empty ---
    use_db_todos = not api_todos
    use_db_messages = not api_messages
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
                    import json

                    msg_rows = cur.execute(
                        """
                        SELECT id, data
                        FROM message
                        WHERE session_id = ?
                        ORDER BY time_created DESC
                        LIMIT 100
                        """,
                        (sid,),
                    ).fetchall()
                    parsed: List[Dict[str, Any]] = []
                    for row in reversed(list(msg_rows)):
                        raw = row["data"]
                        data = (
                            json.loads(raw) if isinstance(raw, str) else (raw or {})
                        )
                        if isinstance(data, dict):
                            parsed.append(data)
                    if parsed:
                        _apply_message_list(result, parsed)
                except (sqlite3.Error, ValueError, TypeError) as e:
                    logger.debug(
                        f"message completeness check skipped for {sid}: {e}"
                    )

            con.close()
        except Exception as e:
            logger.debug(f"OpenCode completeness lookup failed for {sid}: {e}")

    # Output-only signal: compacting as the *last* event in the transcript.
    # Do **not** treat empty todos + finish=stop as all-clear — that is the
    # upstream compact-then-exit-0 false success (opencode#13946).
    # Do **not** flag the Continue prompt's mention of "compaction".
    if compact_output_indicates_premature_exit(output_text or ""):
        result["reasons"].append(
            "session log indicates compaction near end of run"
        )

    if result.get("assistant_asked_question"):
        result["reasons"].append("assistant asked a clarifying question")

    if result["reasons"]:
        result["complete"] = False
        result["premature"] = True
    return result
