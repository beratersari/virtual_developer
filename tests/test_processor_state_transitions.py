"""Unit tests for JobProcessor state transitions and Jira error reporting."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import make_issue_event


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path):
    """JobProcessor wired to temp state + fake Jira (no real agents/git)."""
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc.git_manager = None
    proc.agent_runner = None
    return proc


@pytest.mark.asyncio
async def test_in_flight_issue_updated_is_ignored(processor, state_manager, fake_jira):
    """CRITICAL: do not restart PLANNING/EXECUTING/CODE_REVIEW on update events."""
    state_manager.create_state("PROJ-10", "Work", "do stuff")
    state_manager.update_state("PROJ-10", status=TaskStatus.EXECUTING)

    event = make_issue_event(
        key="PROJ-10",
        event_type="jira:issue_updated",
        status="To Do",
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(event)
        created.assert_not_called()

    loaded = state_manager.get_state("PROJ-10")
    assert loaded.status == TaskStatus.EXECUTING


@pytest.mark.asyncio
async def test_terminal_reprocess_only_when_jira_todo(processor, state_manager):
    """Terminal issues must NOT reprocess unless Jira status is TO DO."""
    state_manager.create_state("PROJ-11", "Done", "x")
    state_manager.update_state("PROJ-11", status=TaskStatus.COMPLETED)

    # In Progress → should not reprocess
    event = make_issue_event(
        key="PROJ-11",
        event_type="jira:issue_updated",
        status="In Progress",
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(event)
        created.assert_not_called()
    assert state_manager.get_state("PROJ-11").status == TaskStatus.COMPLETED

    # To Do → should reprocess
    event_todo = make_issue_event(
        key="PROJ-11",
        event_type="jira:issue_updated",
        status="To Do",
    )
    with patch.object(
        processor, "_handle_issue_created", new_callable=AsyncMock
    ) as created:
        await processor._handle_issue_updated(event_todo)
        created.assert_awaited_once()
    assert state_manager.get_state("PROJ-11").status == TaskStatus.PENDING


@pytest.mark.asyncio
async def test_fail_issue_sets_error_and_posts_jira(processor, state_manager, fake_jira):
    """Every ERROR path should report to Jira via post_error."""
    state_manager.create_state("PROJ-12", "Fail me", "x")
    state_manager.update_state("PROJ-12", status=TaskStatus.PLANNING)

    processor._fail_issue("PROJ-12", "git clone failed: auth")

    loaded = state_manager.get_state("PROJ-12")
    assert loaded.status == TaskStatus.ERROR
    assert "git clone failed" in (loaded.error_message or "")
    assert len(fake_jira.comments) >= 1
    assert "Error" in fake_jira.comments[-1]["body"]
    assert "git clone failed" in fake_jira.comments[-1]["body"]


@pytest.mark.asyncio
async def test_workflow_exception_reports_to_jira(processor, state_manager, fake_jira):
    """Unhandled workflow crash must not leave silent intermediate state without Jira."""
    event = make_issue_event(key="PROJ-13", summary="Fix bug", description="fix typo")

    async def boom(_state):
        # Simulate crash after status would have been set
        processor.state_manager.update_state("PROJ-13", status=TaskStatus.EXECUTING)
        raise RuntimeError("opencode not found")

    with patch.object(processor, "_start_direct_execution", side_effect=boom):
        with patch(
            "src.processor.WorkflowRouter.route_issue",
            return_value=__import__(
                "src.orchestrator.workflow_router", fromlist=["WorkflowType"]
            ).WorkflowType.DIRECT_EXECUTION,
        ):
            await processor.process_event(event)

    loaded = state_manager.get_state("PROJ-13")
    assert loaded is not None
    assert loaded.status == TaskStatus.ERROR
    assert any("opencode not found" in c["body"] for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_plan_ready_not_restarted_on_create(processor, state_manager):
    """PLAN_READY must not re-enter planning on another create event."""
    state_manager.create_state("PROJ-14", "Plan ready", "x")
    state_manager.update_state("PROJ-14", status=TaskStatus.PLAN_READY)

    event = make_issue_event(key="PROJ-14", event_type="jira:issue_created")
    with patch.object(
        processor, "_start_planning_workflow", new_callable=AsyncMock
    ) as plan:
        with patch.object(
            processor, "_start_direct_execution", new_callable=AsyncMock
        ) as direct:
            await processor._handle_issue_created(event)
            plan.assert_not_called()
            direct.assert_not_called()
    assert state_manager.get_state("PROJ-14").status == TaskStatus.PLAN_READY


@pytest.mark.asyncio
async def test_cancel_without_agent_runner_still_notifies(processor, state_manager, fake_jira):
    """ /cancel must always set CANCELLED and comment, even without live runner."""
    state_manager.create_state("PROJ-15", "Cancel me", "x")
    state_manager.update_state(
        "PROJ-15", status=TaskStatus.EXECUTING, current_task_id=None
    )
    processor.agent_runner = None

    await processor._handle_bot_command("PROJ-15", "/cancel")

    assert state_manager.get_state("PROJ-15").status == TaskStatus.CANCELLED
    assert any("cancelled" in c["body"].lower() for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_oracle_failure_reports_error(processor, state_manager, fake_jira):
    """Oracle non-zero return must ERROR + post_error, not leave PENDING."""
    state = state_manager.create_state("PROJ-16", "How to design X?", "architecture question")
    mock_runner = MagicMock()
    mock_runner.run_agent = AsyncMock(
        return_value={"returncode": 1, "stdout": "", "stderr": "oracle crashed"}
    )
    processor.agent_runner = mock_runner

    await processor._start_oracle_consultation(state)

    loaded = state_manager.get_state("PROJ-16")
    assert loaded.status == TaskStatus.ERROR
    assert any("oracle crashed" in c["body"] for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_comment_created_alias_event(processor):
    """Accept jira:issue_commented (sim) as well as comment_created."""
    with patch.object(
        processor, "_handle_comment_created", new_callable=AsyncMock
    ) as h:
        await processor.process_event(
            {
                "webhookEvent": "jira:issue_commented",
                "issue": {"key": "PROJ-17"},
                "comment": {"body": "@DevBot /status"},
            }
        )
        h.assert_awaited_once()
