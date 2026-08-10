"""Unit tests for repo+branch OpenCode session binds."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.state.session_bind_store import (
    SessionBindStore,
    bind_id_for,
    normalize_branch,
    normalize_repo_key,
)


def test_normalize_repo_key_collapses_url_shapes():
    a = normalize_repo_key("https://gitlab.com/Group/Repo.git")
    b = normalize_repo_key("https://gitlab.com/group/repo")
    c = normalize_repo_key("git@gitlab.com:group/repo.git")
    assert a == b == c == "gitlab.com/group/repo"


def test_normalize_branch_strips_refs():
    assert normalize_branch("refs/heads/feature/KAN-1") == "feature/KAN-1"
    assert normalize_branch("  develop  ") == "develop"


def test_session_matches_workdir(tmp_path):
    from tests.test_opencode_sessions import _make_session_db
    from src.opencode_sessions import get_session_directory, session_matches_workdir

    clone = tmp_path / "clone_a"
    clone.mkdir()
    other = tmp_path / "clone_b"
    other.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_here", "directory": str(clone), "title": "KAN-1: x"}],
    )
    assert get_session_directory("ses_here", db_path=db) == str(clone)
    assert session_matches_workdir("ses_here", clone, db_path=db) is True
    assert session_matches_workdir("ses_here", other, db_path=db) is False
    assert session_matches_workdir("ses_missing", clone, db_path=db) is False


def test_empty_timeout_retries_cold(tmp_path):
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner(working_directory=tmp_path)
    empty = tmp_path / "empty.log"
    empty.write_text("")
    task = AgentTask(
        description="t",
        prompt="ORIGINAL BUILD PROMPT",
        agent="atlas",
        issue_key="KAN-7",
        session_id="ses_old",
    )
    runner._resume_opencode_session_for_retry(
        task,
        "ses_old",
        why="timeout",
        session_file=str(empty),
        timed_out=True,
        stdout="",
    )
    assert task.session_id is None
    assert task.abandoned_session_id == "ses_old"
    assert task.prompt == "ORIGINAL BUILD PROMPT"


def test_retry_missing_db_row_keeps_session_for_same_job(tmp_path):
    """SQLite may not have flushed the just-created session yet — keep --session."""
    from src.orchestrator.agent_runner import AgentRunner, AgentTask
    from tests.test_opencode_sessions import _make_session_db

    runner = AgentRunner(working_directory=tmp_path)
    db = _make_session_db(tmp_path / "empty.db", [])
    task = AgentTask(
        description="t",
        prompt="ORIGINAL",
        agent="atlas",
        issue_key="KAN-8",
        session_id="ses_new",
    )
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        runner._resume_opencode_session_for_retry(
            task,
            "ses_new",
            why="incomplete_session",
            session_file=str(tmp_path / "x.log"),
            timed_out=False,
            stdout="partial",
        )
    assert task.session_id == "ses_new"
    # Compact/incomplete resume must not inject a Continue user message
    assert not (task.prompt or "").lower().startswith("continue")
    assert task.prompt == "ORIGINAL"


def test_retry_db_error_keeps_session(tmp_path):
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="t",
        prompt="ORIGINAL",
        agent="atlas",
        issue_key="KAN-8",
        session_id="ses_keep",
    )
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(None, False),
    ):
        runner._resume_opencode_session_for_retry(
            task,
            "ses_keep",
            why="error",
            session_file=str(tmp_path / "x.log"),
            timed_out=False,
            stdout="partial",
        )
    assert task.session_id == "ses_keep"
    assert (task.prompt or "").lower().startswith("continue")


def test_bind_store_upsert_get_delete(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    rec = store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_abc123",
        issue_key="KAN-1",
        job_id="job_1",
        working_directory=str(tmp_path / "clone"),
    )
    assert rec is not None
    assert rec["bind_id"] == bind_id_for(
        "https://gitlab.com/g/r.git", "feature/shared", "develop"
    )
    got = store.get("https://gitlab.com/g/r", "feature/shared", "develop")
    assert got is not None
    assert got["session_id"] == "ses_abc123"
    assert got["issue_key"] == "KAN-1"
    assert got["target_branch"] == "develop"
    assert Path(got["working_directory"]).resolve() == (tmp_path / "clone").resolve()
    dirs = store.working_directories()
    assert (tmp_path / "clone").resolve() in dirs
    listed = store.list_binds()
    assert len(listed) == 1
    assert store.delete(rec["bind_id"]) is True
    assert store.get("https://gitlab.com/g/r.git", "feature/shared", "develop") is None


def test_bind_id_differs_when_only_target_differs():
    a = bind_id_for("https://gitlab.com/g/r.git", "feature/shared", "develop")
    b = bind_id_for("https://gitlab.com/g/r.git", "feature/shared", "main")
    c = bind_id_for("https://gitlab.com/g/r.git", "feature/shared", "develop")
    assert a != b
    assert a == c


def test_bind_store_isolates_sessions_by_target(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_dev",
        issue_key="KAN-1",
    )
    store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        target_branch="main",
        session_id="ses_main",
        issue_key="KAN-2",
    )
    assert (
        store.get("https://gitlab.com/g/r.git", "feature/shared", "develop")[
            "session_id"
        ]
        == "ses_dev"
    )
    assert (
        store.get("https://gitlab.com/g/r.git", "feature/shared", "main")["session_id"]
        == "ses_main"
    )
    assert store.get("https://gitlab.com/g/r.git", "feature/shared") is None
    assert store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        target_branch="",
        session_id="ses_no_tgt",
    ) is None


def test_bind_store_relocate_working_directory(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    old = tmp_path / "legacy"
    new = tmp_path / "short"
    old.mkdir()
    new.mkdir()
    store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_move",
        working_directory=str(old),
    )
    other = tmp_path / "other"
    other.mkdir()
    store.upsert(
        repository_url="https://gitlab.com/g/other.git",
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_stay",
        working_directory=str(other),
    )
    n = store.relocate_working_directory(old, new)
    assert n == 1
    moved = store.get("https://gitlab.com/g/r.git", "feature/shared", "develop")
    assert Path(moved["working_directory"]).resolve() == new.resolve()
    stayed = store.get("https://gitlab.com/g/other.git", "feature/shared", "develop")
    assert Path(stayed["working_directory"]).resolve() == other.resolve()
