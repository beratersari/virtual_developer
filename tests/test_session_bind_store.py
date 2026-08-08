"""Unit tests for repo+branch OpenCode session binds."""

from __future__ import annotations

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
    assert task.prompt == "ORIGINAL BUILD PROMPT"


def test_bind_store_upsert_get_delete(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    rec = store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/shared",
        session_id="ses_abc123",
        issue_key="KAN-1",
        job_id="job_1",
    )
    assert rec is not None
    assert rec["bind_id"] == bind_id_for("https://gitlab.com/g/r.git", "feature/shared")
    got = store.get("https://gitlab.com/g/r", "feature/shared")
    assert got is not None
    assert got["session_id"] == "ses_abc123"
    assert got["issue_key"] == "KAN-1"
    listed = store.list_binds()
    assert len(listed) == 1
    assert store.delete(rec["bind_id"]) is True
    assert store.get("https://gitlab.com/g/r.git", "feature/shared") is None
