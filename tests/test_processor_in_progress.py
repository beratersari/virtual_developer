"""Processor must move Jira issues to In Progress when work starts."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus


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


def test_mark_jira_in_progress_calls_client(processor, fake_jira):
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)
    processor._mark_jira_in_progress("KAN-1")
    fake_jira.transition_to_in_progress.assert_called_once_with("KAN-1")


def test_mark_jira_in_progress_soft_fails(processor, fake_jira):
    fake_jira.transition_to_in_progress = MagicMock(side_effect=RuntimeError("boom"))
    # must not raise
    processor._mark_jira_in_progress("KAN-1")


def test_fail_issue_moves_jira_in_progress(processor, state_manager, fake_jira):
    """Template/config errors must leave Jira In Progress (not stuck on To Do)."""
    state_manager.create_state("KAN-FAIL", "s", "missing mode")
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)
    poller = MagicMock()
    poller._last_jira_status = {"KAN-FAIL": "to do"}
    processor._poller = poller

    processor._fail_issue(
        "KAN-FAIL",
        "*Virtual Developer* could not start: the issue description format is incomplete.",
        suggestion="Fix {params} and move back to To Do.",
    )

    fake_jira.transition_to_in_progress.assert_called_with("KAN-FAIL")
    assert state_manager.get_state("KAN-FAIL").status == TaskStatus.ERROR
    assert poller._last_jira_status["KAN-FAIL"] == "in progress"


@pytest.mark.asyncio
async def test_template_error_moves_jira_in_progress(
    processor, state_manager, fake_jira
):
    """Missing Mode / incomplete {params} must transition Jira to In Progress."""
    from src.issue_git_spec import IssueGitConfigError

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/KAN-TMP\n"
        "Target branch: develop\n"
        "{params}\n"
    )
    state = state_manager.create_state("KAN-TMP", "incomplete template", desc)
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)

    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=IssueGitConfigError(
            "*Virtual Developer* could not start: the issue description format "
            "is incomplete.\n\n*Missing / invalid:* Mode."
        ),
    ):
        git = processor._prepare_git_workspace(state)

    assert git is None
    assert state_manager.get_state("KAN-TMP").status == TaskStatus.ERROR
    # fail path always marks In Progress (last guarantee)
    assert fake_jira.transition_to_in_progress.call_count >= 1
    fake_jira.transition_to_in_progress.assert_any_call("KAN-TMP")


@pytest.mark.asyncio
async def test_issue_created_claims_in_progress_before_workflow(
    processor, state_manager, fake_jira
):
    """Accepting an issue moves Jira to In Progress before template validation."""
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/KAN-EARLY\n"
        "Target branch: develop\n"
        "Mode: plan\n"
        "{params}\n"
    )
    event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "KAN-EARLY",
            "fields": {
                "summary": "plan something",
                "description": desc,
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["ai-assist"],
                "assignee": None,
            },
        },
    }

    async def _boom(state):
        raise RuntimeError("stop before real agent")

    with patch.object(processor, "_start_planning_workflow", side_effect=_boom):
        await processor._handle_issue_created(event)

    # Claim happens in _handle_issue_created before workflow body
    assert fake_jira.transition_to_in_progress.call_count >= 1
    fake_jira.transition_to_in_progress.assert_any_call("KAN-EARLY")
    # Workflow crash → fail_issue → still ERROR + In Progress attempts
    assert state_manager.get_state("KAN-EARLY").status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_direct_execution_transitions_in_progress(
    processor, state_manager, fake_jira, tmp_path
):
    """cli.py process / direct path must transition Jira, not only the poller."""
    state = state_manager.create_state("KAN-IP", "test", "create main.cpp")
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)

    git = MagicMock()
    git.ensure_feature_branch.return_value = "feature/KAN-IP"
    git.work_branch = "feature/KAN-IP"
    git.target_branch = "develop"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/KAN-IP"
    git.ensure_on_work_branch.return_value = True
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "feat: x"
    git.get_last_commit_message.return_value = "feat: x"
    _sha_calls = {"n": 0}

    def _sha(*_a, **_k):
        _sha_calls["n"] += 1
        return "baseline000001" if _sha_calls["n"] == 1 else "delivered000002"

    git.get_last_commit_sha.side_effect = _sha
    git.build_commit_url.return_value = "http://git/c/delivered000002"
    git.create_merge_request.return_value = "http://mr/1"
    git.get_mr_url.return_value = "http://mr/1"
    git.cleanup.return_value = True

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "s1",
            "retry_info": {"attempts": 1, "max_retries": 3, "retried": False},
            "timed_out": False,
        }
    )
    runner.run_agent = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "review ok",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "s2",
        }
    )

    processor._contexts["KAN-IP"] = {"git": git, "runner": runner}
    processor.git_manager = git
    processor.agent_runner = runner

    with patch.object(processor, "_init_git_manager", return_value=git):
        await processor._start_execution_workflow(state)

    fake_jira.transition_to_in_progress.assert_called_with("KAN-IP")
    assert state_manager.get_state("KAN-IP").status == TaskStatus.COMPLETED
