"""Session bind reuse: (repo, work/source, target) → ses_* until dashboard reset.

Product rule: if that key is already mapped, the next job must continue the
same OpenCode session. Cancel, error, re-queue, a missing SQLite row, or a
new clone path must not start a cold session. Only dashboard Reset forgets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import SessionBindStore, bind_id_for
from tests.test_opencode_sessions import _make_session_db

REPO = "https://gitlab.example.com/acme/app.git"
REPO_SSH = "git@gitlab.example.com:acme/app.git"
SOURCE = "feature/shared"
TARGET = "develop"


def _params(repo: str = REPO, source: str = SOURCE, target: str = TARGET) -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}\n"
    )


def _proc(tmp_path, monkeypatch, *, sm=None, store=None):
    sm = sm or JiraStateManager(state_dir=tmp_path / "state")
    store = store or SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", store)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    return proc, sm, store


def _git(clone, *, repo=REPO, work=SOURCE, target=TARGET, source=None):
    git = MagicMock()
    git.remote_url = repo
    git.work_branch = work
    git.source_branch = source if source is not None else work
    git.target_branch = target
    git.get_working_directory.return_value = clone
    git.ensure_on_work_branch.return_value = True
    return git


def _bind(store, *, sid="ses_shared", issue="KAN-A", clone=None, repo=REPO, work=SOURCE, target=TARGET):
    return store.upsert(
        repository_url=repo,
        branch=work,
        target_branch=target,
        session_id=sid,
        issue_key=issue,
        working_directory=str(clone) if clone is not None else None,
    )


def test_cancel_does_not_forget_bind(tmp_path, monkeypatch):
    """Dashboard Cancel must leave the (repo, source, target) map intact."""
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-A", "s", _params())
    sm.update_state("KAN-A", status=TaskStatus.EXECUTING)
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._active_jobs["KAN-A"] = "job_a"
    proc._upsert_session_bind("KAN-A", "ses_live")

    ok = asyncio.run(proc.cancel_job("KAN-A", reason="operator cancel"))
    assert ok["ok"] is True
    bound = store.get(REPO, SOURCE, TARGET)
    assert bound is not None
    assert bound["session_id"] == "ses_live"
    assert "ses_live" not in (bound.get("forgotten_session_ids") or [])
    st = sm.get_state("KAN-A")
    assert st is not None and st.status == TaskStatus.CANCELLED


def test_attach_after_cancel_reuses_bind_for_new_issue(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_live", "directory": str(clone), "title": "KAN-A: x"}],
    )
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-A", "s", _params())
    sm.update_state("KAN-A", status=TaskStatus.EXECUTING)
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-A", "ses_live")
    asyncio.run(proc.cancel_job("KAN-A"))

    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"
    assert task.session_id == "ses_live"


def test_attach_resumes_when_opencode_db_has_no_row(tmp_path, monkeypatch):
    """Bind key exists → resume even if SQLite never flushed the session row."""
    clone = tmp_path / "clone"
    clone.mkdir()
    db = _make_session_db(tmp_path / "empty.db", [])
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    _bind(store, sid="ses_live", issue="KAN-A", clone=clone)
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"
    assert task.session_id == "ses_live"


def test_attach_resumes_when_bind_has_no_working_directory(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    _bind(store, sid="ses_live", issue="KAN-A", clone=None)
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(None, True),
    ):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"


def test_attach_resumes_when_clone_path_changed_after_cancel(tmp_path, monkeypatch):
    old = tmp_path / "old_clone"
    new = tmp_path / "new_clone"
    old.mkdir()
    new.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_live", "directory": str(old), "title": "KAN-A: x"}],
    )
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_live", issue="KAN-A", clone=old)
    git = _git(new)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
        from src.opencode_sessions import get_session_directory

        assert get_session_directory("ses_live", db_path=db) == str(new.resolve())
    assert sid == "ses_live"


def test_attach_resumes_when_db_unreadable_and_paths_differ(tmp_path, monkeypatch):
    """Transient SQLite lock must not drop a live bind (the intermittent miss)."""
    old = tmp_path / "old_clone"
    new = tmp_path / "new_clone"
    old.mkdir()
    new.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_live", issue="KAN-A", clone=old)
    git = _git(new)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(None, False),
    ):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"
    assert task.session_id == "ses_live"
    assert "KAN-B" not in proc._freeze_session_binds


def test_attach_resumes_when_lookup_raises(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_live", issue="KAN-A", clone=clone)
    git = _git(clone)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        side_effect=RuntimeError("db locked"),
    ):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"
    assert "KAN-B" not in proc._freeze_session_binds


def test_attach_resumes_when_working_directory_is_missing(tmp_path, monkeypatch):
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_live", issue="KAN-A", clone=None)
    git = _git(None)
    git.get_working_directory.return_value = None
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_live"


def test_url_and_ref_normalization_hits_same_bind(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(
        store,
        sid="ses_norm",
        repo="https://gitlab.example.com/Acme/App.git",
        work="refs/heads/feature/shared",
        target="refs/heads/develop",
        clone=clone,
    )
    git = _git(clone, repo=REPO_SSH, work="feature/shared", target="develop")
    sm.create_state("KAN-B", "s", _params(repo=REPO_SSH))
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(str(clone), True),
    ):
        sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid == "ses_norm"
    assert bind_id_for(REPO_SSH, "feature/shared", "develop") == bind_id_for(
        "https://gitlab.example.com/Acme/App.git",
        "refs/heads/feature/shared",
        "refs/heads/develop",
    )


def test_different_target_does_not_reuse_session(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_dev", target="develop", clone=clone)
    git = _git(clone, target="main")
    sm.create_state("KAN-B", "s", _params(target="main"))
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid is None
    assert task.session_id is None


def test_different_source_does_not_reuse_session(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_a", work="feature/one", clone=clone)
    git = _git(clone, work="feature/two", source="feature/two")
    sm.create_state("KAN-B", "s", _params(source="feature/two"))
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid is None


def test_primary_source_isolates_per_issue_work_branch(tmp_path, monkeypatch):
    """Source=develop → feature/{KEY}; two issues must not share a session."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    _bind(store, sid="ses_a", issue="KAN-A", work="feature/KAN-A", clone=a)
    git = _git(b, work="feature/KAN-B", source="develop")
    sm.create_state("KAN-B", "s", _params(source="develop"))
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid is None
    assert store.get(REPO, "feature/KAN-A", TARGET)["session_id"] == "ses_a"


def test_dashboard_reset_blocks_resume_for_next_issue(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    rec = _bind(store, sid="ses_old", issue="KAN-A", clone=clone)
    store.forget_session(rec["bind_id"], session_id="ses_old", reason="dashboard-reset")
    git = _git(clone)
    sm.create_state("KAN-B", "s", _params())
    proc._contexts["KAN-B"] = {"git": git, "runner": None}
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-B")
    sid = proc._attach_bound_opencode_session("KAN-B", task, git)
    assert sid is None
    assert "ses_old" in (task.forgotten_session_ids or [])


def test_same_issue_rework_after_cancel_resumes(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-A", "s", _params())
    sm.update_state(
        "KAN-A",
        status=TaskStatus.CANCELLED,
        current_opencode_session_id="ses_live",
        metadata={"last_opencode_session_id": "ses_live"},
    )
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    _bind(store, sid="ses_live", issue="KAN-A", clone=clone)
    proc._reset_for_reprocess("KAN-A")
    # begin-style clear of the live slot (reprocess / new job)
    sm.update_state("KAN-A", current_opencode_session_id=None)
    task = AgentTask(description="t", prompt="p", agent="atlas", issue_key="KAN-A")
    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=(str(clone), True),
    ):
        sid = proc._attach_bound_opencode_session("KAN-A", task, git)
    assert sid == "ses_live"


def test_late_apply_after_cancel_does_not_forget_live_session(tmp_path, monkeypatch):
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone)
    sm.create_state("KAN-A", "s", _params())
    sm.update_state("KAN-A", status=TaskStatus.EXECUTING)
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-A", "ses_live")
    asyncio.run(proc.cancel_job("KAN-A"))
    # Workflow still applies the aborted agent result after cancel released ctx
    proc._apply_agent_result_session(
        "KAN-A",
        {
            "opencode_session_id": "ses_live",
            "session_file": str(tmp_path / "a.log"),
            "retry_info": {"aborted": True, "last_opencode_session_id": "ses_live"},
        },
    )
    bound = store.get(REPO, SOURCE, TARGET)
    assert bound["session_id"] == "ses_live"
    assert "ses_live" not in (bound.get("forgotten_session_ids") or [])


def test_late_apply_without_git_does_not_rebind_primary_source(tmp_path, monkeypatch):
    """After cancel, metadata Source=develop must not create a develop→develop bind."""
    clone = tmp_path / "clone"
    clone.mkdir()
    proc, sm, store = _proc(tmp_path, monkeypatch)
    git = _git(clone, work="feature/KAN-A", source="develop")
    sm.create_state("KAN-A", "s", _params(source="develop"))
    sm.update_state(
        "KAN-A",
        metadata={
            "repository_url": REPO,
            "source_branch": "develop",
            "target_branch": "develop",
            "feature_branch": "feature/KAN-A",
        },
    )
    proc._contexts["KAN-A"] = {"git": git, "runner": None}
    proc._upsert_session_bind("KAN-A", "ses_iso")
    proc._contexts.pop("KAN-A", None)
    proc._apply_agent_result_session(
        "KAN-A",
        {"opencode_session_id": "ses_iso", "session_file": None},
    )
    assert store.get(REPO, "feature/KAN-A", "develop")["session_id"] == "ses_iso"
    assert store.get(REPO, "develop", "develop") is None


@pytest.mark.asyncio
async def test_e2e_cancel_in_flight_then_second_issue_resumes(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Cancel while the agent is running; the next issue must resume the session."""
    monkeypatch.chdir(tmp_path)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    clone = tmp_path / "shared_clone"
    clone.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_a", "directory": str(clone), "title": "KAN-A: x"}],
    )
    monkeypatch.setattr("src.opencode_sessions._default_db_path", lambda: db)

    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = MagicMock(return_value=None)
    proc._snapshot_delivery_baseline = MagicMock()

    git = _git(clone)
    runner = AgentRunner(working_directory=clone)
    seen: List[Dict[str, Any]] = []
    released = asyncio.Event()

    async def fake_run(task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(
            {
                "issue_key": task.issue_key,
                "session_id": task.session_id,
                "attempt": kwargs.get("attempt_number"),
            }
        )
        on_sid = kwargs.get("on_session_id")
        sid = task.session_id or "ses_a"
        if on_sid:
            on_sid(sid)
        if task.issue_key == "KAN-A":
            released.set()
            for _ in range(400):
                st = sm.get_state("KAN-A")
                if st and st.status == TaskStatus.CANCELLED:
                    break
                await asyncio.sleep(0.01)
            return {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": f"Session: {sid}\n",
                "stderr": "Aborted",
                "session_file": str(tmp_path / f"{task.issue_key}.log"),
                "opencode_session_id": sid,
                "aborted": True,
                "progress": 0,
            }
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": f"Session: {sid}\ndone\n",
            "stderr": "",
            "session_file": str(tmp_path / f"{task.issue_key}.log"),
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(runner, "run_agent", fake_run)

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    monkeypatch.setattr(proc, "_prepare_git_workspace", fake_prepare)

    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 0
    live.default_agent = "atlas"
    live.agent_task_max_incomplete_retries = 0
    monkeypatch.setattr("src.config.get_settings", lambda: live)

    sm.create_state("KAN-A", "first", _params())
    job_a = asyncio.create_task(proc._start_execution_workflow(sm.get_state("KAN-A")))
    await asyncio.wait_for(released.wait(), timeout=2)
    cancelled = await proc.cancel_job("KAN-A", reason="stop A")
    assert cancelled["ok"] is True
    await asyncio.wait_for(job_a, timeout=2)

    bound = binds.get(REPO, SOURCE, TARGET)
    assert bound is not None
    assert bound["session_id"] == "ses_a"
    assert "ses_a" not in (bound.get("forgotten_session_ids") or [])

    sm.create_state("KAN-B", "second", _params())
    await proc._start_execution_workflow(sm.get_state("KAN-B"))
    b_runs = [row for row in seen if row["issue_key"] == "KAN-B"]
    assert b_runs, "KAN-B never reached the agent"
    assert b_runs[0]["session_id"] == "ses_a"
    assert binds.get(REPO, SOURCE, TARGET)["session_id"] == "ses_a"


@pytest.mark.asyncio
async def test_e2e_error_then_new_issue_resumes(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    clone = tmp_path / "shared_clone"
    clone.mkdir()
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_err", "directory": str(clone), "title": "KAN-A: x"}],
    )
    monkeypatch.setattr("src.opencode_sessions._default_db_path", lambda: db)

    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = MagicMock(return_value=None)
    proc._snapshot_delivery_baseline = MagicMock()

    git = _git(clone)
    runner = AgentRunner(working_directory=clone)
    seen: List[Optional[str]] = []

    async def fake_run(task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(task.session_id)
        sid = task.session_id or "ses_err"
        on_sid = kwargs.get("on_session_id")
        if on_sid:
            on_sid(sid)
        if task.issue_key == "KAN-A":
            return {
                "task_id": task.task_id,
                "returncode": 1,
                "stdout": f"Session: {sid}\n",
                "stderr": "agent boom",
                "session_file": str(tmp_path / "a.log"),
                "opencode_session_id": sid,
                "progress": 0,
            }
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": f"Session: {sid}\ndone\n",
            "stderr": "",
            "session_file": str(tmp_path / "b.log"),
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(runner, "run_agent", fake_run)

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": git, "runner": runner}
        return git

    monkeypatch.setattr(proc, "_prepare_git_workspace", fake_prepare)
    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 0
    live.default_agent = "atlas"
    live.agent_task_max_incomplete_retries = 0
    monkeypatch.setattr("src.config.get_settings", lambda: live)

    sm.create_state("KAN-A", "first", _params())
    await proc._start_execution_workflow(sm.get_state("KAN-A"))
    assert binds.get(REPO, SOURCE, TARGET)["session_id"] == "ses_err"

    sm.create_state("KAN-B", "second", _params())
    await proc._start_execution_workflow(sm.get_state("KAN-B"))
    assert seen[0] is None
    assert seen[1] == "ses_err"



