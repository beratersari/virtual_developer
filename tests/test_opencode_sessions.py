"""Unit tests for OpenCode SQLite session lookup (src/opencode_sessions.py)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.opencode_sessions import (
    find_sessions_for_issue,
    path_contains_issue_key,
    resolve_session_id,
)


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


def test_resolve_uses_path_segment_when_no_preferred(session_db: Path):
    sid = resolve_session_id(
        "PROJ-1",
        working_directory=Path("/tmp/vd/.temp/repo_PROJ-1_20260101"),
        db_path=session_db,
    )
    # Exact directory match on path_segment row
    assert sid == "ses_path_segment"
