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


def test_check_status_changes_skips_open_still_open(poller, state_manager):
    """Non-English/Open To Do-like names must not reprocess every poll."""
    state_manager.create_state("P-5", "done", "")
    state_manager.update_state("P-5", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"P-5": "open"}
    poller._last_jira_status = {"P-5": "open"}
    issues = [{"key": "P-5", "fields": {"status": {"name": "Open"}}}]
    assert poller.check_status_changes(issues) == []


def test_check_status_changes_skips_turkish_still_todo(poller, state_manager):
    state_manager.create_state("P-6", "done", "")
    state_manager.update_state("P-6", status=TaskStatus.ERROR)
    poller._status_before_poll = {"P-6": "yapılacaklar"}
    poller._last_jira_status = {"P-6": "yapılacaklar"}
    issues = [{"key": "P-6", "fields": {"status": {"name": "Yapılacaklar"}}}]
    assert poller.check_status_changes(issues) == []


def test_check_status_changes_reprocesses_cancelled_after_in_progress(poller, state_manager):
    """Cancel while In Progress keeps tracker as in progress; return to To Do re-queues."""
    state_manager.create_state("P-7", "cancelled job", "")
    state_manager.update_state(
        "P-7",
        status=TaskStatus.CANCELLED,
        metadata={"requeue_eligible": True},
    )
    # After cancel from In Progress we leave last status as "in progress"
    poller._status_before_poll = {"P-7": "in progress"}
    issues = [
        {
            "key": "P-7",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["ai-assist"],
            },
        }
    ]
    result = poller.check_status_changes(issues)
    assert len(result) == 1
    assert result[0]["key"] == "P-7"


def test_check_status_changes_skips_cancel_while_still_todo(poller, state_manager):
    """Cancel while Jira stayed To Do must not auto-requeue next poll."""
    state_manager.create_state("P-9", "cancelled still todo", "")
    state_manager.update_state(
        "P-9",
        status=TaskStatus.CANCELLED,
        metadata={"requeue_eligible": True},
    )
    poller._status_before_poll = {"P-9": "to do"}
    issues = [
        {
            "key": "P-9",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            },
        }
    ]
    assert poller.check_status_changes(issues) == []


def test_error_still_todo_reprocesses_when_description_changes(poller, state_manager):
    """ERROR + To Do requeues after user edits description (e.g. adds Mode)."""
    from src.state.models import TaskStatus

    poller.state_manager = state_manager
    state_manager.create_state("P-EDIT", "s", "old body")
    state_manager.update_state(
        "P-EDIT",
        status=TaskStatus.ERROR,
        metadata={
            "requeue_eligible": True,
            "last_intake_fingerprint": "deadbeef",  # old hash
        },
    )
    poller._status_before_poll = {"P-EDIT": "to do"}
    issue = {
        "key": "P-EDIT",
        "fields": {
            "summary": "s",
            "description": "new body with Mode: build",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    out = poller.check_status_changes([issue])
    assert [i["key"] for i in out] == ["P-EDIT"]


def test_error_still_todo_skips_when_text_unchanged(poller, state_manager):
    """Same description after ERROR must not spam reprocess every poll."""
    from src.state.models import TaskStatus

    poller.state_manager = state_manager
    desc = "unchanged body"
    issue = {
        "key": "P-SAME",
        "fields": {
            "summary": "s",
            "description": desc,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    fp = poller.issue_text_fingerprint(issue)
    state_manager.create_state("P-SAME", "s", desc)
    state_manager.update_state(
        "P-SAME",
        status=TaskStatus.ERROR,
        metadata={"requeue_eligible": True, "last_intake_fingerprint": fp},
    )
    poller._status_before_poll = {"P-SAME": "to do"}
    assert poller.check_status_changes([issue]) == []


def test_process_issue_updates_last_status_after_in_progress(poller, fake_jira):
    """After accepting work, tracker must not keep stale 'to do' (In Progress first)."""
    fake_jira.transition_to_in_progress = MagicMock(return_value=True)
    poller._last_jira_status = {"P-8": "to do"}
    poller._handler = None
    poller.process_issue(
        {"key": "P-8", "fields": {"summary": "s", "status": {"name": "To Do"}}},
        is_update=False,
    )
    assert poller._last_jira_status["P-8"] == "in progress"
    fake_jira.transition_to_in_progress.assert_called_once_with("P-8")
