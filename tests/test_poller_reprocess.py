"""Unit tests for poller reprocess logic (no infinite loops)."""

from unittest.mock import MagicMock

import pytest

from src.state.models import TaskStatus


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    return p


def test_check_status_changes_skips_in_flight(poller, state_manager):
    state_manager.create_state("P-1", "running", "")
    state_manager.update_state("P-1", status=TaskStatus.EXECUTING)
    poller._status_before_poll = {"P-1": "in progress"}
    poller._last_jira_status = {"P-1": "to do"}

    issues = [
        {
            "key": "P-1",
            "fields": {"status": {"name": "To Do"}, "labels": ["ai-assist"]},
        }
    ]
    result = poller.check_status_changes(issues)
    assert result == []


def test_check_status_changes_skips_completed_still_todo(poller, state_manager):
    """If completed but always was To Do (no real transition), do not loop."""
    state_manager.create_state("P-2", "done", "")
    state_manager.update_state("P-2", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"P-2": "to do"}  # still To Do last poll
    poller._last_jira_status = {"P-2": "to do"}

    issues = [
        {
            "key": "P-2",
            "fields": {"status": {"name": "To Do"}, "labels": ["ai-assist"]},
        }
    ]
    result = poller.check_status_changes(issues)
    assert result == []


def test_check_status_changes_reprocesses_on_real_todo_reentry(poller, state_manager):
    """User moved issue back to To Do from In Progress after completion → reprocess."""
    state_manager.create_state("P-3", "reopen", "")
    state_manager.update_state("P-3", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"P-3": "in progress"}
    poller._last_jira_status = {"P-3": "to do"}

    issues = [
        {
            "key": "P-3",
            "fields": {"status": {"name": "To Do"}, "labels": ["ai-assist"]},
        }
    ]
    result = poller.check_status_changes(issues)
    assert len(result) == 1
    assert result[0]["key"] == "P-3"


def test_check_status_changes_skips_planning(poller, state_manager):
    state_manager.create_state("P-4", "plan", "")
    state_manager.update_state("P-4", status=TaskStatus.PLANNING)
    poller._status_before_poll = {"P-4": "to do"}
    issues = [{"key": "P-4", "fields": {"status": {"name": "To Do"}}}]
    assert poller.check_status_changes(issues) == []
