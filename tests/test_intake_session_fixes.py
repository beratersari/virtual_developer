"""Regression tests for intake + session-continue fixes."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.jira.client import JiraClient, _comment_body_to_adf
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


def _resp(status=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text or str(json_data)
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=r
        )
    return r


@pytest.fixture
def jira_client():
    with patch("src.jira.client.httpx.Client") as mock_cls:
        mock_http = MagicMock()
        mock_cls.return_value = mock_http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://example.atlassian.net"
            s.jira_api_token = "token"
            s.jira_email = "a@b.c"
            c = JiraClient()
            c.client = mock_http
            yield c, mock_http


def test_search_issues_falls_back_to_api3_on_410(jira_client):
    c, http = jira_client
    gone = _resp(410, text="removed")
    gone.raise_for_status = MagicMock()
    ok = _resp(200, {"issues": [{"key": "KAN-1"}]})
    http.get.side_effect = [gone, ok]
    issues = c.search_issues("project=KAN")
    assert [i["key"] for i in issues] == ["KAN-1"]
    assert "/rest/api/3/search/jql" in http.get.call_args_list[1].args[0]


def test_sprint_pagination_advances_by_page_length(jira_client):
    c, http = jira_client
    page1 = _resp(
        200, {"issues": [{"key": f"I-{i}"} for i in range(50)], "total": 120}
    )
    page2 = _resp(
        200, {"issues": [{"key": f"I-{i}"} for i in range(50, 100)], "total": 120}
    )
    page3 = _resp(
        200, {"issues": [{"key": f"I-{i}"} for i in range(100, 120)], "total": 120}
    )
    http.get.side_effect = [page1, page2, page3]
    issues = c.get_sprint_issues(1, max_results=100)
    assert len(issues) == 120
    assert http.get.call_args_list[1].kwargs["params"]["startAt"] == 50


def test_onprem_matches_start_progress_name(jira_client):
    c, http = jira_client
    c.is_cloud = False
    http.get.return_value = _resp(
        200,
        {
            "transitions": [
                {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
                {"id": "21", "name": "Done", "to": {"name": "Done"}},
            ]
        },
    )
    http.post.return_value = _resp(204)
    assert c.transition_to_in_progress("P-1") is True
    assert http.post.call_args.kwargs["json"]["transition"]["id"] == "11"


def test_add_labels_fail_closed_when_get_issue_fails(jira_client):
    c, http = jira_client
    http.get.return_value = _resp(500)
    assert c.add_labels("P-1", ["ai-plan-ready"]) is False
    http.put.assert_not_called()


def test_adf_comment_splits_newlines():
    doc = _comment_body_to_adf("line1\n\nline2")
    assert doc["type"] == "doc"
    texts = []
    for p in doc["content"]:
        for n in p["content"]:
            if n.get("type") == "text":
                texts.append(n["text"])
                assert "\n" not in n["text"]
    assert texts == ["line1", "line2"]


def _poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    return p


def test_poll_board_treats_completed_todo_as_rework(
    state_manager, fake_jira, monkeypatch
):
    """To Do + trigger after completed is intentional rework (not a stuck loop)."""
    from src.config import settings

    poller = _poller(state_manager, fake_jira)
    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    state_manager.create_state("PS-DONE", "s", "d")
    state_manager.update_state(
        "PS-DONE",
        status=TaskStatus.COMPLETED,
        metadata={"requeue_eligible": True},
    )
    poller._status_before_poll = {"PS-DONE": "to do"}
    poller._seen_issues.add("PS-DONE")
    issue = {
        "key": "PS-DONE",
        "fields": {
            "summary": "s",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    result = poller.poll_board()
    assert "PS-DONE" in [i["key"] for i in result]


def test_poll_board_reemits_after_in_progress_to_todo(
    state_manager, fake_jira, monkeypatch
):
    from src.config import settings

    poller = _poller(state_manager, fake_jira)
    monkeypatch.setattr(settings, "trigger_labels", "bot")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    state_manager.create_state("PS-ERR", "s", "d")
    state_manager.update_state(
        "PS-ERR",
        status=TaskStatus.ERROR,
        metadata={"requeue_eligible": True},
    )
    poller._status_before_poll = {"PS-ERR": "in progress"}
    issue = {
        "key": "PS-ERR",
        "fields": {
            "summary": "s",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    result = poller.poll_board()
    assert "PS-ERR" in [i["key"] for i in result]


def test_queue_recover_stuck_running(tmp_path):
    qs = WorkQueueStore(tmp_path / "q")
    rec = qs.enqueue(
        source="jira",
        issue_key="KAN-1",
        summary="s",
        message="m",
    )
    qs.update(rec["queue_id"], status="running", started_at="2026-01-01T00:00:00")
    n = qs.recover_stuck_running()
    assert n == 1
    assert qs.get(rec["queue_id"])["status"] == "queued"


def test_bind_forget_refuses_old_session(tmp_path):
    from src.state.session_bind_store import SessionBindStore

    store = SessionBindStore(tmp_path / "binds")
    rec = store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/x",
        target_branch="develop",
        session_id="ses_old",
        issue_key="KAN-1",
    )
    store.forget_session(rec["bind_id"], session_id="ses_old", reason="reset")
    refused = store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/x",
        target_branch="develop",
        session_id="ses_old",
        issue_key="KAN-1",
    )
    assert not (refused or {}).get("session_id")
    ok = store.upsert(
        repository_url="https://gitlab.com/g/r.git",
        branch="feature/x",
        target_branch="develop",
        session_id="ses_new",
        issue_key="KAN-1",
    )
    assert ok["session_id"] == "ses_new"
    assert "ses_old" in ok["forgotten_session_ids"]


def test_webhook_enabled_requires_secret():
    from src.gitlab.webhook import decide_gitlab_note_webhook

    d = decide_gitlab_note_webhook(
        {"object_kind": "note"},
        headers={"x-gitlab-event": "Note Hook"},
        enabled=True,
        secret="",
    )
    assert d.accepted is False
    assert d.http_status == 401


def test_session_is_busy_treats_retry_as_busy():
    from src.opencode_serve import session_is_busy

    assert session_is_busy({"ses_1": {"type": "retry", "attempt": 2}}, "ses_1")
    assert session_is_busy({"ses_1": {"type": "busy"}}, "ses_1")
    assert not session_is_busy({"ses_1": {"type": "idle"}}, "ses_1")
