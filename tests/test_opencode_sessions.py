"""Unit tests for OpenCode SQLite session lookup (src/opencode_sessions.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.opencode_sessions import (
    assess_session_completeness,
    chat_display_role,
    compact_output_indicates_premature_exit,
    compact_related_reasons,
    detect_compact_in_output,
    reasons_are_compact_only,
    strip_compact_reasons,
    find_sessions_for_issue,
    lookup_session_directory,
    path_contains_issue_key,
    paths_equivalent,
    relocate_session_directories,
    resolve_session_id,
)
from src.opencode_serve import DEFAULT_CONTINUE_PROMPT


def _make_session_db(path: Path, rows: list[dict]) -> Path:
    """Create a minimal OpenCode-like ``session`` table for tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            agent TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER
        )
        """
    )
    for r in rows:
        con.execute(
            """
            INSERT INTO session (
                id, title, directory, agent,
                time_created, time_updated, cost, tokens_input, tokens_output
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["id"],
                r.get("title"),
                r.get("directory"),
                r.get("agent", "build"),
                r.get("time_created", 1),
                r.get("time_updated", 1),
                r.get("cost", 0.0),
                r.get("tokens_input", 0),
                r.get("tokens_output", 0),
            ),
        )
    con.commit()
    con.close()
    return path


@pytest.fixture
def session_db(tmp_path: Path) -> Path:
    """DB with several sessions to exercise ranking / false positives."""
    return _make_session_db(
        tmp_path / "opencode.db",
        [
            {
                "id": "ses_unrelated",
                "title": "random work",
                "directory": "/tmp/other_project",
                "time_updated": 100,
            },
            {
                "id": "ses_wrong_substring",
                # PROJ-10 must NOT match issue PROJ-1
                "title": "not our issue",
                "directory": "/tmp/PROJ-10_extra_clone",
                "time_updated": 200,
            },
            {
                "id": "ses_title_match",
                "title": "PROJ-1: implement feature",
                "directory": "/tmp/some_other_dir",
                "time_updated": 300,
            },
            {
                "id": "ses_path_segment",
                "title": "agent run",
                # Real temp-clone layout: {remote}_{issue_key}_{timestamp}
                "directory": "/tmp/vd/.temp/repo_PROJ-1_20260101",
                "time_updated": 400,
            },
            {
                "id": "ses_exact_dir",
                "title": "PROJ-1: exact",
                "directory": "/tmp/vd/.temp/exact_clone",
                "time_updated": 500,
            },
            {
                "id": "ses_older_exact",
                "title": "PROJ-1: older exact",
                "directory": "/tmp/vd/.temp/exact_clone",
                "time_updated": 450,
            },
        ],
    )


# --- path token helper ---


def test_title_prefix_match_is_case_insensitive(tmp_path: Path):
    db = _make_session_db(
        tmp_path / "case.db",
        [
            {
                "id": "ses_lower",
                "title": "proj-1: implement feature",
                "directory": "/tmp/other",
                "time_updated": 10,
            }
        ],
    )
    rows = find_sessions_for_issue("PROJ-1", db_path=db)
    assert len(rows) == 1
    assert rows[0]["id"] == "ses_lower"


def test_like_underscore_in_issue_key_is_literal(tmp_path: Path):
    db = _make_session_db(
        tmp_path / "us.db",
        [
            {
                "id": "ses_wild",
                "title": "noise",
                "directory": "/tmp/PROJX1_extra_clone",
                "time_updated": 20,
            },
            {
                "id": "ses_real",
                "title": "agent run",
                "directory": "/tmp/vd/.temp/repo_PROJ_1_20260101",
                "time_updated": 10,
            },
        ],
    )
    rows = find_sessions_for_issue("PROJ_1", db_path=db)
    ids = {r["id"] for r in rows}
    assert "ses_real" in ids
    assert "ses_wild" not in ids


def test_substring_flood_does_not_hide_real_issue(tmp_path: Path):
    """PROJ-10 rows must not crowd PROJ-1 out of the SQL candidate window."""
    rows = [
        {
            "id": f"ses_other_{i}",
            "title": "not ours",
            "directory": f"/tmp/PROJ-10_clone_{i}",
            "time_updated": 1000 + i,
        }
        for i in range(80)
    ]
    rows.append(
        {
            "id": "ses_real_proj1",
            "title": "PROJ-1: the real one",
            "directory": "/tmp/unrelated_dir",
            "time_updated": 1,
        }
    )
    db = _make_session_db(tmp_path / "flood.db", rows)
    found = find_sessions_for_issue("PROJ-1", db_path=db, limit=5)
    ids = {r["id"] for r in found}
    assert "ses_real_proj1" in ids
    assert all(not i.startswith("ses_other_") for i in ids)


def test_path_contains_issue_key_boundaries():
    assert path_contains_issue_key("/tmp/repo_PROJ-1_20260101", "PROJ-1")
    assert path_contains_issue_key("/tmp/vd/PROJ-1", "PROJ-1")
    assert path_contains_issue_key(r"C:\temp\repo_KAN-42_ts", "KAN-42")
    # Substring false positive: PROJ-10 is not PROJ-1
    assert not path_contains_issue_key("/tmp/PROJ-10_extra_clone", "PROJ-1")
    assert not path_contains_issue_key("/tmp/other_project", "PROJ-1")
    assert not path_contains_issue_key("", "PROJ-1")
    assert not path_contains_issue_key("/tmp/x", "")


# --- find_sessions_for_issue ---


def test_empty_issue_key_returns_empty(session_db: Path):
    assert find_sessions_for_issue("", db_path=session_db) == []
    assert find_sessions_for_issue("   ", db_path=session_db) == []


def test_missing_db_returns_empty(tmp_path: Path):
    missing = tmp_path / "nope.db"
    assert find_sessions_for_issue("PROJ-1", db_path=missing) == []


def test_exact_directory_ranked_first(session_db: Path):
    wd = Path("/tmp/vd/.temp/exact_clone")
    rows = find_sessions_for_issue(
        "PROJ-1",
        working_directory=wd,
        db_path=session_db,
        limit=10,
    )
    assert rows
    assert rows[0]["id"] == "ses_exact_dir"
    ids = [r["id"] for r in rows]
    assert "ses_path_segment" in ids
    assert ids.index("ses_exact_dir") < ids.index("ses_path_segment")
    assert ids.index("ses_exact_dir") < ids.index("ses_title_match")


def test_title_prefix_match_without_working_directory(session_db: Path):
    rows = find_sessions_for_issue("PROJ-1", db_path=session_db, limit=10)
    ids = {r["id"] for r in rows}
    assert "ses_title_match" in ids
    assert "ses_path_segment" in ids
    assert "ses_unrelated" not in ids
    # PROJ-10 clone must not appear for PROJ-1
    assert "ses_wrong_substring" not in ids


def test_proj1_does_not_match_proj10_directory(session_db: Path):
    rows = find_sessions_for_issue("PROJ-1", db_path=session_db, limit=20)
    ids = {r["id"] for r in rows}
    assert "ses_wrong_substring" not in ids


def test_title_prefix_ranks_above_path_when_no_exact_dir(session_db: Path):
    rows = find_sessions_for_issue(
        "PROJ-1",
        working_directory=Path("/tmp/does_not_exist_clone"),
        db_path=session_db,
        limit=10,
    )
    assert rows
    # Without exact dir, title-prefix (tier 1) before pure path (tier 2)
    # Newest title match among title-tier rows is ses_exact_dir / ses_older_exact
    # (they have title PROJ-1:…) — first non-path-only should be title-based
    first = rows[0]
    assert (first.get("title") or "").startswith("PROJ-1:")


def test_limit_respected(session_db: Path):
    rows = find_sessions_for_issue("PROJ-1", db_path=session_db, limit=1)
    assert len(rows) == 1


def test_corrupt_db_returns_empty(tmp_path: Path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a sqlite database", encoding="utf-8")
    assert find_sessions_for_issue("PROJ-1", db_path=bad) == []


def test_windows_style_path_segment(tmp_path: Path):
    db = _make_session_db(
        tmp_path / "win.db",
        [
            {
                "id": "ses_win",
                "title": "build",
                "directory": r"C:\Users\x\.temp\repo_KAN-42_ts",
                "time_updated": 10,
            }
        ],
    )
    rows = find_sessions_for_issue("KAN-42", db_path=db)
    assert len(rows) == 1
    assert rows[0]["id"] == "ses_win"


def test_row_fields_populated(session_db: Path):
    rows = find_sessions_for_issue(
        "PROJ-1",
        working_directory=Path("/tmp/vd/.temp/exact_clone"),
        db_path=session_db,
        limit=1,
    )
    assert rows[0]["id"] == "ses_exact_dir"
    assert "title" in rows[0]
    assert "directory" in rows[0]
    assert "time_updated" in rows[0]


# --- resolve_session_id ---


def test_resolve_prefers_parsed_ses_id(session_db: Path):
    sid = resolve_session_id(
        "PROJ-1",
        preferred="ses_from_cli_output",
        db_path=session_db,
    )
    assert sid == "ses_from_cli_output"


def test_resolve_ignores_non_ses_preferred_and_uses_db(session_db: Path):
    sid = resolve_session_id(
        "PROJ-1",
        working_directory=Path("/tmp/vd/.temp/exact_clone"),
        preferred="not_a_session",
        db_path=session_db,
    )
    assert sid == "ses_exact_dir"


def test_resolve_falls_back_to_preferred_when_no_db_match(tmp_path: Path):
    empty = _make_session_db(tmp_path / "empty.db", [])
    sid = resolve_session_id(
        "ZZZ-99",
        preferred="still_not_ses",
        db_path=empty,
    )
    assert sid == "still_not_ses"


def test_resolve_returns_none_when_nothing(tmp_path: Path):
    empty = _make_session_db(tmp_path / "empty.db", [])
    assert resolve_session_id("ZZZ-99", db_path=empty) is None


def test_lookup_session_directory_distinguishes_missing_and_error(tmp_path: Path):
    db = _make_session_db(
        tmp_path / "ok.db",
        [{"id": "ses_here", "title": "t", "directory": "/tmp/x"}],
    )
    d, ok = lookup_session_directory("ses_here", db_path=db)
    assert ok is True
    assert d == "/tmp/x"
    d, ok = lookup_session_directory("ses_missing", db_path=db)
    assert ok is True
    assert d is None
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite", encoding="utf-8")
    d, ok = lookup_session_directory("ses_here", db_path=bad)
    assert ok is False
    assert d is None


def test_relocate_session_directories_rewrites_matching_rows(tmp_path: Path):
    old = tmp_path / "legacy_clone"
    new = tmp_path / "short_clone"
    old.mkdir()
    new.mkdir()
    db = _make_session_db(
        tmp_path / "rel.db",
        [
            {"id": "ses_move", "title": "KAN-1: x", "directory": str(old)},
            {"id": "ses_keep", "title": "other", "directory": str(tmp_path / "other")},
        ],
    )
    n = relocate_session_directories(old, new, db_path=db)
    assert n == 1
    d, ok = lookup_session_directory("ses_move", db_path=db)
    assert ok is True
    assert paths_equivalent(d, new)
    keep, _ = lookup_session_directory("ses_keep", db_path=db)
    assert paths_equivalent(keep, tmp_path / "other")


def test_resolve_uses_path_segment_when_no_preferred(session_db: Path):
    sid = resolve_session_id(
        "PROJ-1",
        working_directory=Path("/tmp/vd/.temp/repo_PROJ-1_20260101"),
        db_path=session_db,
    )
    # Exact directory match on path_segment row
    assert sid == "ses_path_segment"


# --- completeness / compact premature exit ---


def _make_full_session_db(
    path: Path,
    *,
    session_id: str = "ses_test1",
    todos: list[tuple[str, str]] | None = None,
    last_message: dict | None = None,
    messages: list[dict] | None = None,
) -> Path:
    """Session + optional todo + message tables matching real OpenCode layout."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            agent TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE todo (
            session_id TEXT,
            content TEXT,
            status TEXT,
            priority TEXT,
            position INTEGER,
            time_created INTEGER,
            time_updated INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
        """
    )
    con.execute(
        """
        INSERT INTO session (
            id, title, directory, agent,
            time_created, time_updated, cost, tokens_input, tokens_output
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (session_id, "PROJ-1: work", "/tmp/x", "build", 1, 2, 0.0, 80000, 1000),
    )
    for i, (content, status) in enumerate(todos or []):
        con.execute(
            """
            INSERT INTO todo (
                session_id, content, status, priority, position,
                time_created, time_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, content, status, "high", i, 1, 1),
        )
    to_insert = messages if messages is not None else (
        [last_message] if last_message is not None else []
    )
    for i, msg in enumerate(to_insert):
        con.execute(
            """
            INSERT INTO message (id, session_id, time_created, time_updated, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"msg_{i}",
                session_id,
                i + 1,
                i + 1,
                json.dumps(msg),
            ),
        )
    con.commit()
    con.close()
    return path


def test_detect_compact_in_output_patterns():
    assert detect_compact_in_output("… Compacting session …")
    assert detect_compact_in_output("session compacted successfully")
    assert detect_compact_in_output("Context automatically compacted")
    assert detect_compact_in_output("auto-compact triggered")
    assert not detect_compact_in_output("implemented compact hash function")
    assert not detect_compact_in_output("")


def test_assess_complete_when_todos_done_and_finish_stop(tmp_path: Path):
    db = _make_full_session_db(
        tmp_path / "ok.db",
        todos=[("Implement", "completed"), ("Commit", "completed")],
        last_message={"role": "assistant", "finish": "stop", "summary": None},
    )
    r = assess_session_completeness("ses_test1", db_path=db)
    assert r["complete"] is True
    assert r["premature"] is False
    assert r["open_todos"] == 0


def test_assess_premature_open_todos(tmp_path: Path):
    """Reproduce: process exits 0 while todos remain (compact / mid-work die)."""
    db = _make_full_session_db(
        tmp_path / "open.db",
        todos=[
            ("Explore", "completed"),
            ("Build", "in_progress"),
            ("Commit", "pending"),
        ],
        last_message={"role": "assistant", "finish": None},
    )
    r = assess_session_completeness("ses_test1", db_path=db)
    assert r["complete"] is False
    assert r["premature"] is True
    assert r["open_todos"] == 2
    assert any("open todos" in x for x in r["reasons"])
    assert any("unfinished" in x for x in r["reasons"])


def test_assess_premature_compaction_summary_stop(tmp_path: Path):
    """Upstream bug: last msg is compaction summary with finish=stop → exit 0."""
    db = _make_full_session_db(
        tmp_path / "compact.db",
        todos=[("Still working", "pending")],
        last_message={
            "role": "assistant",
            "finish": "stop",
            "summary": True,
        },
    )
    r = assess_session_completeness("ses_test1", db_path=db)
    assert r["premature"] is True
    assert any("compaction summary" in x for x in r["reasons"])


def test_assess_premature_from_compacting_cli_output(tmp_path: Path):
    db = _make_full_session_db(
        tmp_path / "cli.db",
        todos=[],  # no todos table signal
        last_message={"role": "assistant", "finish": "tool-calls"},
    )
    out = (
        "read files...\n"
        "tool: bash\n"
        "Compacting session to free context…\n"
    )
    r = assess_session_completeness(
        "ses_test1",
        output_text=out,
        db_path=db,
    )
    assert r["premature"] is True
    assert r["compact_in_output"] is True


def test_assess_no_session_id_with_compact_output_still_flags():
    r = assess_session_completeness(
        None,
        output_text="done some work\ncompacting\n",
    )
    assert r["premature"] is True
    assert r["compact_in_output"] is True


def test_assess_compact_output_plus_finish_stop_empty_todos_is_premature(
    tmp_path: Path,
):
    """False success: compact-then-exit-0 with todos gone / finish=stop.

    Production hole: assess treated this as ``clean`` and marked COMPLETED.
    """
    db = _make_full_session_db(
        tmp_path / "false_ok.db",
        todos=[],
        last_message={"role": "assistant", "finish": "stop", "summary": None},
    )
    out = (
        "All todos complete.\n"
        "Compacting session to free context…\n"
    )
    r = assess_session_completeness(
        "ses_test1",
        output_text=out,
        db_path=db,
    )
    assert r["premature"] is True, r
    assert r["compact_in_output"] is True
    assert any("compaction" in x.lower() for x in r["reasons"])


def test_assess_sqlite_compact_then_stop_sequence_is_premature(tmp_path: Path):
    """CLI/DB path must see compaction → assistant stop, not only last row."""
    db = _make_full_session_db(
        tmp_path / "seq.db",
        todos=[("All", "completed")],
        messages=[
            {
                "role": "user",
                "parts": [{"type": "compaction", "auto": True}],
            },
            {
                "role": "assistant",
                "finish": "stop",
                "summary": None,
                "parts": [{"type": "text", "text": "All todos complete."}],
            },
        ],
    )
    r = assess_session_completeness("ses_test1", db_path=db)
    assert r["premature"] is True, r
    assert any("compact-then-stop" in x for x in r["reasons"])


def test_strip_compact_reasons_clears_compact_only():
    r = {
        "complete": False,
        "premature": True,
        "reasons": [
            "last assistant followed a compaction message (compact-then-stop)",
            "CLI output indicates compaction near end of run",
        ],
    }
    assert reasons_are_compact_only(r["reasons"]) is True
    strip_compact_reasons(r)
    # Transient CLI compact noise is dropped; compact-then-stop stays incomplete.
    assert r["complete"] is False
    assert r["premature"] is True
    assert r["reasons"] == [
        "last assistant followed a compaction message (compact-then-stop)",
    ]


def test_strip_compact_reasons_keeps_open_todos():
    r = {
        "complete": False,
        "premature": True,
        "reasons": [
            "open todos: 1 pending, 0 in_progress",
            "last assistant followed a compaction message (compact-then-stop)",
        ],
    }
    strip_compact_reasons(r)
    assert r["premature"] is True
    assert r["reasons"] == [
        "open todos: 1 pending, 0 in_progress",
        "last assistant followed a compaction message (compact-then-stop)",
    ]


def test_continue_prompt_echo_is_not_premature_compact():
    """Resume prompt mentions 'compaction' but a finished answer must succeed."""
    out = (
        DEFAULT_CONTINUE_PROMPT
        + "\nImplemented the feature and committed on the work branch.\n"
    )
    assert compact_output_indicates_premature_exit(out) is False
    r = assess_session_completeness(
        None,
        output_text=out,
        messages=[
            {
                "role": "assistant",
                "finish": "stop",
                "summary": None,
                "parts": [{"type": "text", "text": "Implemented and committed."}],
            }
        ],
        todos=[{"status": "completed", "content": "All"}],
    )
    assert r["complete"] is True, r
    assert r["premature"] is False


def test_assess_work_after_summary_assistant_is_complete():
    """Last assistant after a compact *summary* is resumed work, not stop."""
    messages = [
        {
            "role": "user",
            "parts": [{"type": "compaction", "auto": True}],
        },
        {
            "role": "assistant",
            "finish": "stop",
            "summary": True,
            "parts": [{"type": "text", "text": "Compacted."}],
        },
        {
            "role": "assistant",
            "finish": "stop",
            "summary": None,
            "parts": [{"type": "text", "text": "Finished remaining work."}],
        },
    ]
    r = assess_session_completeness(
        "ses_ok",
        messages=messages,
        todos=[{"status": "completed", "content": "All"}],
    )
    assert r["complete"] is True, r
    assert r["premature"] is False


def test_assess_compact_then_stop_message_sequence_is_premature():
    """Serve/API: last assistant immediately after a compaction user part."""
    messages = [
        {
            "role": "user",
            "parts": [{"type": "compaction", "auto": True}],
        },
        {
            "role": "assistant",
            "finish": "stop",
            "summary": None,
            "parts": [{"type": "text", "text": "All todos complete."}],
        },
    ]
    r = assess_session_completeness(
        "ses_api",
        messages=messages,
        todos=[{"status": "completed", "content": "All"}],
    )
    assert r["premature"] is True, r
    assert any("compact-then-stop" in x for x in r["reasons"])


def test_chat_display_role_compaction_is_not_user():
    assert (
        chat_display_role(
            "user",
            parts=[{"type": "compaction", "auto": True}],
        )
        == "compaction"
    )
    assert (
        chat_display_role(
            "user",
            parts=[
                {"type": "compaction", "auto": True},
                {"type": "text", "text": "Session compacted to free context."},
            ],
        )
        == "compaction"
    )
    assert (
        chat_display_role(
            "assistant",
            agent="compaction",
            summary=True,
            parts=[{"type": "text", "text": "## Compaction summary"}],
        )
        == "compaction"
    )
    assert (
        chat_display_role(
            "user",
            parts=[
                {
                    "type": "text",
                    "text": "Continue the previous OpenCode session. The last turn stopped early",
                }
            ],
        )
        == "skip"
    )
    assert (
        chat_display_role("user", parts=[{"type": "text", "text": "implement KAN-1"}])
        == "user"
    )
    assert (
        chat_display_role(
            "user",
            parts=[
                {
                    "type": "text",
                    "text": "[restore checkpointed session agent configuration after compaction]\n<!-- OMO_INTERNAL_INITIATOR -->",
                }
            ],
        )
        == "skip"
    )
    assert (
        chat_display_role(
            "user",
            parts=[
                {
                    "type": "text",
                    "text": "Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.",
                }
            ],
        )
        == "skip"
    )
    media = (
        "The previous request exceeded the provider's size limit due to "
        "large media attachments. The conversation was compacted and media "
        "files were removed from context. If the user was asking about "
        "attached images or files, explain that the attachments were too "
        "large to process and suggest they try again with smaller or fewer "
        "files.\n\n"
        "Continue if you have next steps, or stop and ask for clarification "
        "if you are unsure how to proceed."
    )
    assert (
        chat_display_role("user", parts=[{"type": "text", "text": media}]) == "skip"
    )
    search = (
        "[search-mode] MAXIMIZE SEARCH EFFORT. Launch multiple background "
        "agents IN PARALLEL:\n\n# Build mode\n"
    )
    assert (
        chat_display_role("user", parts=[{"type": "text", "text": search}]) == "skip"
    )


def test_assistant_asked_question_is_not_a_crash():
    from src.opencode_sessions import (
        assess_session_completeness,
        assistant_asked_question,
    )

    q = (
        "All module READMEs already exist.\n\n"
        "Shall I continue with the remaining work?"
    )
    assert assistant_asked_question(q) is True
    assert assistant_asked_question("Implemented the parser and committed.") is False
    result = assess_session_completeness(
        "ses_q",
        messages=[
            {
                "role": "assistant",
                "finish": "stop",
                "parts": [{"type": "text", "text": q}],
            }
        ],
        todos=[{"status": "completed"}],
    )
    assert result["assistant_asked_question"] is True
    assert result["premature"] is True
    assert any("clarifying question" in str(r) for r in result["reasons"])
    assert not any("unfinished" in str(r) for r in result["reasons"])


def test_compact_related_reasons_detects_markers():
    assert compact_related_reasons(["compaction summary"]) is True
    assert compact_related_reasons(["open todos: 1 pending, 0 in_progress"]) is False
    assert compact_related_reasons(
        ["last assistant followed a compaction message (compact-then-stop)"]
    )


def test_live_incomplete_session_if_present():
    """Optional: prove real local OpenCode DB still has incomplete KAN-12 session.

    Skips when the known session is gone (clean machines / CI).
    """
    from pathlib import Path

    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    sid = "ses_02ca1287effeEErvnu2CC4dNhK"
    if not db.is_file():
        pytest.skip("no local OpenCode DB")
    # Only run if that session still exists
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT id FROM session WHERE id = ?", (sid,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        pytest.skip("known incomplete session not present")

    r = assess_session_completeness(sid, db_path=db)
    assert r["db_checked"] is True
    assert r["premature"] is True, r
    assert r["open_todos"] > 0
    # Last turn aborted mid-step (finish null) — matches production incomplete
    assert r["last_finish"] is None or str(r["last_finish"]).lower() in {
        "tool-calls",
        "unknown",
        "",
    }
