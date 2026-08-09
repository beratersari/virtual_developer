"""Stable temp clone + OpenCode session resume on same repo/work branch."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import (
    GitManager,
    purge_stale_temp_dirs,
    session_bound_workspace_paths,
)
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.session_bind_store import SessionBindStore
from tests.test_opencode_sessions import _make_session_db


def _gm(tmp_path, *, issue_key: str, source: str, target: str, url: str):
    with patch.object(GitManager, "_clone_into_temp"), patch.object(
        GitManager, "_refresh_existing_clone"
    ), patch("src.git_manager.set_current_temp_dir"):
        return GitManager(
            issue_key=issue_key,
            remote_url=url,
            source_branch=source,
            target_branch=target,
        )


def test_shared_source_different_target_uses_new_temp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    a = _gm(tmp_path, issue_key="KAN-A", source="feature/shared", target="develop", url=url)
    b = _gm(tmp_path, issue_key="KAN-B", source="feature/shared", target="main", url=url)
    assert a.temp_dir.resolve() != b.temp_dir.resolve()


def test_workspace_folder_name_fits_windows_path_budget(tmp_path, monkeypatch):
    """Clone folder stays short so nested MSBuild paths fit under MAX_PATH."""
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/test_project.git"
    gm = _gm(
        tmp_path,
        issue_key="KAN-1905",
        source="feature/KAN-1905",
        target="feature/KAN-21",
        url=url,
    )
    name = gm.temp_dir.name
    assert len(name) <= 25
    assert "feature-KAN-1905" not in name
    assert "feature-KAN-21" not in name
    # Full Windows-style prefix + nested Debug tree stays under 260
    prefix = rf"C:\Users\BERAT\virtual_developer\.temp\{name}"
    nested = r"\build\proje1\src\Debug\proje1.tlog\CL.read.1.tlog"
    assert len(prefix + nested) < 260


def test_shared_source_reuses_same_temp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    a = _gm(tmp_path, issue_key="KAN-A", source="feature/shared", target="develop", url=url)
    b = _gm(tmp_path, issue_key="KAN-B", source="feature/shared", target="develop", url=url)
    assert a.temp_dir is not None and b.temp_dir is not None
    assert a.temp_dir.resolve() == b.temp_dir.resolve()
    assert a.work_branch == "feature/shared"
    assert b.work_branch == "feature/shared"


def test_primary_source_isolates_per_issue_key(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    a = _gm(tmp_path, issue_key="KAN-1", source="develop", target="develop", url=url)
    b = _gm(tmp_path, issue_key="KAN-2", source="develop", target="develop", url=url)
    assert a.temp_dir is not None and b.temp_dir is not None
    assert a.temp_dir.resolve() != b.temp_dir.resolve()
    assert a.work_branch == "feature/KAN-1"
    assert b.work_branch == "feature/KAN-2"


def test_same_issue_rerun_reuses_feature_key_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    first = _gm(tmp_path, issue_key="KAN-9", source="develop", target="develop", url=url)
    second = _gm(tmp_path, issue_key="KAN-9", source="develop", target="develop", url=url)
    assert first.temp_dir.resolve() == second.temp_dir.resolve()


def test_existing_git_clone_is_refreshed_not_cloned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    first = _gm(tmp_path, issue_key="KAN-A", source="feature/shared", target="develop", url=url)
    git_dir = first.temp_dir / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/feature/shared\n", encoding="utf-8")

    def fake_origin(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if cmd[:3] == ["git", "remote", "get-url"]:
            r = MagicMock()
            r.returncode = 0
            r.stdout = url + "\n"
            r.stderr = ""
            return r
        raise AssertionError(f"unexpected subprocess: {cmd}")

    refreshed = {"n": 0}
    cloned = {"n": 0}

    def mark_refresh(self):
        refreshed["n"] += 1

    def mark_clone(self):
        cloned["n"] += 1

    with patch.object(GitManager, "_refresh_existing_clone", mark_refresh), patch.object(
        GitManager, "_clone_into_temp", mark_clone
    ), patch("src.git_manager.subprocess.run", side_effect=fake_origin), patch(
        "src.git_manager.set_current_temp_dir"
    ):
        again = GitManager(
            issue_key="KAN-B",
            remote_url=url,
            source_branch="feature/shared",
            target_branch="develop",
        )
    assert again.temp_dir.resolve() == first.temp_dir.resolve()
    assert refreshed["n"] == 1
    assert cloned["n"] == 0


def test_cleanup_keeps_session_bound_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    url = "https://gitlab.example.com/acme/app.git"
    gm = _gm(tmp_path, issue_key="KAN-A", source="feature/shared", target="develop", url=url)
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url=url,
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_keep",
        issue_key="KAN-A",
        working_directory=str(gm.temp_dir),
    )
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    monkeypatch.setattr("src.git_manager.settings.temp_cleanup_policy", "always")
    assert gm.cleanup(success=True) is True
    assert gm.temp_dir is not None and gm.temp_dir.exists()


def test_purge_protects_bound_workspace(tmp_path, monkeypatch):
    clone = tmp_path / "bound_clone"
    clone.mkdir()
    (clone / "f").write_text("x", encoding="utf-8")
    old = time.time() - (3 * 86400)
    os.utime(clone, (old, old))
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_keep",
        issue_key="KAN-A",
        working_directory=str(clone),
    )
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    removed = purge_stale_temp_dirs(max_age_days=1.0, base_dir=tmp_path)
    assert clone.exists()
    assert removed == 0
    assert clone.resolve() in session_bound_workspace_paths()


def test_attach_resumes_when_second_job_reuses_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    clone = tmp_path / "shared_clone"
    clone.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_shared", "directory": str(clone), "title": "KAN-A: x"}],
    )
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()

    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.work_branch = "feature/shared"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    sm.create_state("KAN-A", "s", "d")
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._active_jobs["KAN-A"] = "job_a"
    proc._upsert_session_bind("KAN-A", "ses_shared")
    bound = store.get(git.remote_url, "feature/shared", "develop")
    assert bound is not None
    assert Path(bound["working_directory"]).resolve() == clone.resolve()

    sm.create_state("KAN-B", "s", "d")
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_shared"
    assert task.session_id == "ses_shared"


def test_source_lock_key_matches_bind_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    a = proc._source_lock_key(
        "https://gitlab.com/Group/Repo.git", "refs/heads/feature/shared"
    )
    b = proc._source_lock_key("https://gitlab.com/group/repo", "feature/shared")
    c = proc._source_lock_key(
        "git@gitlab.com:group/repo.git", "feature/shared"
    )
    assert a == b == c
    assert proc._claim_source_branch(
        "A-1", "https://gitlab.com/g/r.git", "feature/x"
    )
    assert proc._claim_source_branch(
        "A-1", "https://gitlab.com/g/r.git", "feature/x"
    )
    assert (
        proc._claim_source_branch("B-2", "https://gitlab.com/g/r", "feature/x")
        is False
    )


def test_attach_db_error_matching_bind_wd_resumes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    clone = tmp_path / "shared_clone"
    clone.mkdir()
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.work_branch = "feature/shared"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    sm.create_state("KAN-A", "s", "d")
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-A", "ses_shared")
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-A")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(None, False),
    ):
        sid = proc._attach_bound_opencode_session("KAN-A", task, git)
    assert sid == "ses_shared"
    assert task.session_id == "ses_shared"
    assert "KAN-A" not in proc._freeze_session_binds


def test_attach_db_error_mismatch_freezes_bind(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    clone = tmp_path / "shared_clone"
    clone.mkdir()
    other = tmp_path / "other_clone"
    other.mkdir()
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.work_branch = "feature/shared"
    git.target_branch = "develop"
    git.get_working_directory.return_value = other
    sm.create_state("KAN-A", "s", "d")
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    store.upsert(
        repository_url=git.remote_url,
        branch="feature/shared",
        target_branch="develop",
        session_id="ses_shared",
        issue_key="KAN-A",
        working_directory=str(clone),
    )
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-A")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(None, False),
    ):
        sid = proc._attach_bound_opencode_session("KAN-A", task, git)
    assert sid is None
    assert task.session_id is None
    assert "KAN-A" in proc._freeze_session_binds
    proc._upsert_session_bind("KAN-A", "ses_new_should_not_stick")
    bound = store.get(git.remote_url, "feature/shared", "develop")
    assert bound["session_id"] == "ses_shared"
