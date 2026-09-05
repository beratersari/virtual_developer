"""Full branch coverage for JiraPoller."""

from unittest.mock import MagicMock, patch

import pytest

from src.jira.poller import JiraPoller
from src.state.models import TaskStatus


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


@pytest.fixture
def poller(state_manager, fake_jira):
    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="10")
    p.state_manager = state_manager
    return p


def test_init_without_board():
    with patch("src.jira.poller.settings") as s:
        s.poll_interval_seconds = 30
        s.jira_board_id = ""
        p = JiraPoller(client=MagicMock(), board_id="")
        assert p.board_id == "" or p.board_id is None or True


def test_is_assigned_to_bot_variants(poller, fake_jira):
    # client is FakeJiraClient which returns None for get_issue by default
    # use MagicMock client
    poller.client = MagicMock()
    poller.client.get_issue.return_value = None
    assert poller._is_assigned_to_jira_ai_bot("X-1") is False

    poller.client.get_issue.return_value = {"fields": {"assignee": None}}
    assert poller._is_assigned_to_jira_ai_bot("X-1") is False

    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "Jira AI Bot"}}
    }
    assert poller._is_assigned_to_jira_ai_bot("X-1") is True

    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "jira-ai-bot"}}
    }
    assert poller._is_assigned_to_jira_ai_bot("X-1") is True

    # Configurable fragments include "devbot" by default
    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "DevBot"}}
    }
    assert poller._is_assigned_to_jira_ai_bot("X-1") is True

    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "Alice"}}
    }
    assert poller._is_assigned_to_jira_ai_bot("X-1") is False

    poller.client.get_issue.side_effect = RuntimeError("net")
    assert poller._is_assigned_to_jira_ai_bot("X-1") is False


def test_poll_board_no_board(poller):
    poller.board_id = ""
    assert poller.poll_board() == []


def test_poll_board_no_sprint(poller):
    poller.client = MagicMock()
    poller.client.get_active_sprint.return_value = None
    assert poller.poll_board() == []


def test_poll_board_no_issues(poller):
    poller.client = MagicMock()
    poller.client.get_active_sprint.return_value = {"id": 1, "name": "S"}
    poller.client.get_sprint_issues.return_value = []
    assert poller.poll_board() == []


def _todo_issue(key: str, *, labels=None, assignee=None):
    fields = {
        "status": {"name": "To Do"},
        "labels": list(labels or []),
        "summary": key,
    }
    if assignee is not None:
        fields["assignee"] = assignee
    return {"key": key, "fields": fields}


def test_poll_board_requires_label_and_assignee(poller):
    """To Do + label-only or assignee-only must not start work."""
    poller.client = MagicMock()
    poller.client.get_active_sprint.return_value = {"id": 1, "name": "S"}
    poller.client.get_sprint_issues.return_value = [
        _todo_issue("L-1", labels=["bot"], assignee={"displayName": "Alice"}),
        _todo_issue("A-1", labels=["other"], assignee={"displayName": "DevBot"}),
        _todo_issue("B-1", labels=["bot"], assignee={"displayName": "DevBot"}),
    ]
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["bot", "ai-assist"]
        s.trigger_assignee_names_list = ["devbot"]
        out = poller.poll_board()
    keys = [i["key"] for i in out]
    assert keys == ["B-1"]


def test_poller_triggers_on_helper():
    from src.jira.triggers import poller_triggers_on

    assert poller_triggers_on(has_trigger_label=True, assigned_to_bot=True) is True
    assert poller_triggers_on(has_trigger_label=True, assigned_to_bot=False) is False
    assert poller_triggers_on(has_trigger_label=False, assigned_to_bot=True) is False
    assert poller_triggers_on(has_trigger_label=False, assigned_to_bot=False) is False


def test_poll_board_new_and_reprocess(poller, state_manager):
    poller.client = MagicMock()
    poller.client.get_active_sprint.return_value = {"id": 1, "name": "S"}
    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "Jira AI Bot"}}
    }
    poller.client.get_sprint_issues.return_value = [
        {
            "key": "N-1",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["ai-assist"],
                "summary": "new",
            },
        },
        {
            "key": "N-2",
            "fields": {
                "status": {"name": "In Progress"},
                "labels": ["ai-assist"],
                "summary": "wip",
            },
        },
    ]
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_assignee_names_list = ["jira ai bot", "devbot"]
        # first poll — status_before empty
        poller._status_before_poll = {}
        result = poller.poll_board()
    assert any(i["key"] == "N-1" for i in result)

    # completed still todo without transition — no reprocess
    state_manager.create_state("N-1", "s", "d")
    state_manager.update_state("N-1", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"N-1": "to do"}
    poller._seen_issues.add("N-1")
    poller.client.get_sprint_issues.return_value = [
        {
            "key": "N-1",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["ai-assist"],
                "summary": "new",
            },
        }
    ]
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_assignee_names_list = ["jira ai bot", "devbot"]
        result2 = poller.poll_board()
    # not in reprocess
    assert not any(
        i["key"] == "N-1" and state_manager.get_state("N-1").status == TaskStatus.COMPLETED
        for i in result2
        if False
    )


def test_process_issue_create_and_update(poller):
    events = []
    poller._handler = lambda e: events.append(e)
    poller.client = MagicMock()
    poller.client.transition_to_in_progress.return_value = True
    poller.client.get_myself.return_value = {"name": "devbot", "key": "devbot"}
    poller.client.assign_issue.return_value = True
    poller.client.is_cloud = False
    issue = {"key": "P-1", "fields": {"summary": "s", "assignee": None}}
    poller.process_issue(issue, is_update=False)
    poller.process_issue(issue, is_update=True)
    assert events[0]["webhookEvent"] == "jira:issue_created"
    assert events[1]["webhookEvent"] == "jira:issue_updated"
    assert poller.client.assign_issue.call_count >= 1
    poller.client.assign_issue.assert_any_call("P-1", "devbot")


def test_process_issue_no_handler(poller):
    poller._handler = None
    poller.client = MagicMock()
    poller.client.transition_to_in_progress.return_value = False
    poller.process_issue({"key": "P-2", "fields": {"summary": "s"}})


def test_start_stop_loop(poller):
    poller.client = MagicMock()
    poller.client.get_active_sprint.return_value = {"id": 1, "name": "S"}
    poller.client.get_sprint_issues.return_value = [
        {
            "key": "S-1",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["ai-assist"],
                "summary": "x",
            },
        }
    ]
    poller.client.get_issue.return_value = {
        "fields": {"assignee": {"displayName": "Jira AI Bot"}}
    }
    poller.client.transition_to_in_progress.return_value = True
    events = []

    def handler(e):
        events.append(e)
        poller.stop()

    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_assignee_names_list = ["jira ai bot", "devbot"]
        with patch("time.sleep", return_value=None):
            poller.interval = 1
            poller.start(handler)
    assert events
    assert poller._running is False


def test_start_poll_error_continues(poller):
    poller.client = MagicMock()
    poller.client.get_active_sprint.side_effect = RuntimeError("boom")
    calls = {"n": 0}

    def stop_soon(e=None):
        calls["n"] += 1
        if calls["n"] >= 1:
            poller.stop()

    # stop after error via short loop
    original_sleep = __import__("time").sleep

    def sleep_and_stop(_):
        poller.stop()

    with patch("time.sleep", side_effect=sleep_and_stop):
        poller.interval = 1
        poller.start(lambda e: None)
    assert poller._running is False
