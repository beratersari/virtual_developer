"""Regression tests for critical isolation / failure-path bugs."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, _agent_subprocess_env
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _bind_git_agent(processor, issue_key: str, tmp_path: Path, *, push_ok: bool = True):
    git = MagicMock()
    git.ensure_feature_branch.return_value = f"feature/{issue_key}"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = f"feature/{issue_key}"
    git.push.return_value = push_ok
    git.get_last_commit_subject.return_value = "feat: x"
    git.get_last_commit_message.return_value = "feat: x\n\nbody"
    git.create_merge_request.return_value = "http://mr/1" if push_ok else None
    git.get_mr_url.return_value = "http://mr/1" if push_ok else None
    git.add_mr_comment.return_value = True
    git.cleanup.return_value = True

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_1",
            "retry_info": {
                "attempts": 1,
                "max_retries": 3,
                "retried": False,
                "last_opencode_session_id": "ses_1",
            },
            "timed_out": False,
        }
    )
    runner.run_agent = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "review ok",
            "stderr": "",
            "session_file": str(tmp_path / "r.log"),
            "opencode_session_id": "ses_r",
        }
    )
    runner.cancel_task.return_value = True

    processor._contexts[issue_key] = {"git": git, "runner": runner}
    processor.git_manager = git
    processor.agent_runner = runner
    return git, runner


@pytest.mark.asyncio
async def test_retry_refreshes_current_task_id(processor, state_manager, tmp_path):
    """Retries mint a new task_id; state.current_task_id must track it for cancel."""
    state = state_manager.create_state("RETRY-1", "s", "implement the feature")
    git, runner = _bind_git_agent(processor, "RETRY-1", tmp_path)
    seen = {}

    async def with_retry(task, on_retry=None, **kwargs):
        seen["original"] = task.task_id
        # Simulate agent_runner minting a new id and notifying on_retry
        new_id = "task_retry_second"
        task.task_id = new_id
        if on_retry:
            on_retry(1, 0.0, "error", None, "boom", 1, "ses_old", new_id)
        seen["after_retry_state"] = state_manager.get_state("RETRY-1").current_task_id
        return {
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_new",
            "retry_info": {"attempts": 2, "retried": True},
        }

    runner.run_agent_with_retry = AsyncMock(side_effect=with_retry)

    with patch.object(processor, "_init_git_manager", return_value=git):
        await processor._start_direct_execution(state)

    assert seen["after_retry_state"] == "task_retry_second"
    # Completed job should not leave a stale running id (may be cleared or final)
    loaded = state_manager.get_state("RETRY-1")
    assert loaded.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_push_failure_does_not_complete(processor, state_manager, tmp_path, fake_jira):
    """Agent success + push fail must ERROR, never COMPLETED."""
    state = state_manager.create_state("PUSH-1", "s", "d")
    git, _ = _bind_git_agent(processor, "PUSH-1", tmp_path, push_ok=False)

    with patch.object(processor, "_init_git_manager", return_value=git):
        await processor._start_direct_execution(state)

    loaded = state_manager.get_state("PUSH-1")
    assert loaded.status == TaskStatus.ERROR
    assert "push" in (loaded.error_message or "").lower()
    assert any("push" in c["body"].lower() for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_concurrent_init_keeps_per_issue_context(processor, tmp_path):
    """Second issue must not erase first issue's git/agent context."""
    class FakeGM:
        def __init__(self, issue_key):
            self.issue_key = issue_key
            self.temp_dir = tmp_path / issue_key
            self.temp_dir.mkdir(exist_ok=True)

        def get_working_directory(self):
            return self.temp_dir

    with patch("src.processor.GitManager", side_effect=lambda issue_key: FakeGM(issue_key)):
        with patch("src.processor.AgentRunner") as AR:
            AR.side_effect = lambda working_directory=None: MagicMock(
                working_directory=working_directory
            )
            processor._init_git_manager("A-1")
            processor._init_git_manager("B-2")

    assert processor._git_for("A-1").issue_key == "A-1"
    assert processor._git_for("B-2").issue_key == "B-2"
    assert processor._runner_for("A-1") is not processor._runner_for("B-2")


@pytest.mark.asyncio
async def test_cancel_is_sticky_against_late_success(processor, state_manager, tmp_path):
    """After CANCELLED, finishing agent must not mark COMPLETED."""
    state = state_manager.create_state("CX-1", "s", "d")
    git, runner = _bind_git_agent(processor, "CX-1", tmp_path, push_ok=True)

    async def agent_then_cancel(task, **kwargs):
        # Simulate /cancel while agent runs
        state_manager.update_state("CX-1", status=TaskStatus.CANCELLED)
        return {
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "s",
            "retry_info": {"attempts": 1, "max_retries": 3, "retried": False},
            "timed_out": False,
        }

    runner.run_agent_with_retry = AsyncMock(side_effect=agent_then_cancel)

    with patch.object(processor, "_init_git_manager", return_value=git):
        await processor._start_direct_execution(state)

    assert state_manager.get_state("CX-1").status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_comment_failure_does_not_error_completed_issue(
    processor, state_manager, fake_jira, tmp_path
):
    state_manager.create_state("CM-1", "s", "d")
    state_manager.update_state("CM-1", status=TaskStatus.COMPLETED)

    runner = MagicMock()
    runner.run_agent = AsyncMock(
        return_value={
            "returncode": 1,
            "stdout": "",
            "stderr": "agent boom",
            "session_file": None,
            "opencode_session_id": None,
        }
    )
    processor._contexts["CM-1"] = {"git": None, "runner": runner}
    processor.agent_runner = runner

    await processor._handle_direct_request("CM-1", "please do something")

    assert state_manager.get_state("CM-1").status == TaskStatus.COMPLETED
    assert any("could not complete" in c["body"].lower() for c in fake_jira.comments)


def test_ensure_agent_runner_does_not_reuse_other_issue(processor, tmp_path):
    runner_a = AgentRunner(working_directory=tmp_path / "a")
    processor._contexts["A-1"] = {"git": None, "runner": runner_a}
    processor.agent_runner = runner_a

    with patch.object(processor, "_init_git_manager") as init:
        # Force fallback path for B-2
        init.side_effect = RuntimeError("no remote")
        runner_b = processor._ensure_agent_runner("B-2")

    assert runner_b is not runner_a
    assert processor._runner_for("A-1") is runner_a
    assert processor._runner_for("B-2") is runner_b


def test_run_agent_registers_for_cancel():
    """Foreground run_agent must put process in _running_tasks (cancelable)."""
    import inspect
    from src.orchestrator.agent_runner import AgentRunner

    src = inspect.getsource(AgentRunner.run_agent)
    assert "self._running_tasks[task.task_id] = process" in src


def test_agent_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", "secret-jira")
    monkeypatch.setenv("GITLAB_PAT", "secret-pat")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _agent_subprocess_env()
    assert "JIRA_API_TOKEN" not in env
    assert "GITLAB_PAT" not in env
    assert env.get("PATH") == "/usr/bin"


def test_router_implementation_beats_oracle():
    assert (
        WorkflowRouter.route_issue("X", "how to implement feature", "build auth")
        != WorkflowType.ORACLE_CONSULT
    )
    assert (
        WorkflowRouter.route_issue("X", "should we use kafka", "pure architecture question")
        == WorkflowType.ORACLE_CONSULT
    )


def test_resolve_plan_path_prefers_workspace(processor, tmp_path):
    issue = "PL-1"
    workspace = tmp_path / "clone"
    plan_dir = workspace / ".sisyphus" / "plans"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / f"{issue}.md"
    plan_file.write_text("# plan")

    git = MagicMock()
    git.get_working_directory.return_value = workspace
    processor._contexts[issue] = {"git": git, "runner": MagicMock()}

    found = processor._resolve_plan_path(issue)
    assert found == plan_file


@pytest.mark.asyncio
async def test_stuck_without_started_at_errors(state_manager, reporter, fake_jira):
    from src.daemon import JiraAgentDaemon
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = proc
    daemon.state_manager = state_manager
    daemon._running = True

    state_manager.create_state("ST-1", "s", "d")
    state_manager.update_state("ST-1", status=TaskStatus.EXECUTING, started_at=None)

    async def stop(_):
        daemon._running = False

    with patch("asyncio.sleep", side_effect=stop):
        await daemon._monitor_active_issues()

    assert state_manager.get_state("ST-1").status == TaskStatus.ERROR
    assert any("stuck" in c["body"].lower() or "timestamp" in c["body"].lower() for c in fake_jira.comments)


def test_cleanup_always_deletes(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    monkeypatch.chdir(tmp_path)
    d = tmp_path / "repo"
    d.mkdir()
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="CL-1")
    gm.temp_dir = d
    gm.remote_enabled = True
    with patch("src.git_manager.settings") as s:
        s.temp_cleanup_policy = "always"
        assert gm.cleanup() is True
    assert not d.exists()
