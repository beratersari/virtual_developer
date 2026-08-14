"""Jira REST Agile API verification for sprint widen + PENDING intake.

These tests drive ``JiraClient`` over httpx ``MockTransport`` that speaks the
real Agile 1.0 paths production uses:

  GET {host}/rest/agile/1.0/board/{id}/sprint?state=active
  GET {host}/rest/agile/1.0/sprint/{id}/issue
  GET {host}/rest/agile/1.0/board/{id}/issue

No live Jira; the HTTP contract is the same.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import httpx
import pytest

from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.state.models import TaskStatus


BOARD = "10"
HOST = "https://jira.example.com"

_TODO = {
    "status": {"name": "To Do"},
    "labels": ["bot"],
}


def _issue(key: str, summary: str) -> Dict[str, Any]:
    return {"key": key, "fields": {**_TODO, "summary": summary}}


def _json(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _text(status: int, body: str) -> httpx.Response:
    return httpx.Response(status, text=body)


class AgileJira:
    """In-memory Jira Agile 1.0 + issue GET used by JiraClient."""

    def __init__(
        self,
        *,
        sprint_mode: str = "ok",
        sprint_issues: Optional[List[Dict[str, Any]]] = None,
        board_issues: Optional[List[Dict[str, Any]]] = None,
    ):
        self.sprint_mode = sprint_mode
        self.sprint_issues = list(sprint_issues or [])
        self.board_issues = list(board_issues or [])
        self.calls: List[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == f"/rest/agile/1.0/board/{BOARD}/sprint":
            if self.sprint_mode == "kanban":
                return _text(400, "Board does not support sprints")
            if self.sprint_mode == "500":
                return _text(500, "INTERNAL")
            if self.sprint_mode == "empty":
                return _json(200, {"values": [], "maxResults": 50, "startAt": 0})
            return _json(
                200,
                {
                    "values": [
                        {"id": 42, "name": "Sprint 1", "state": "active"}
                    ]
                },
            )
        if path == "/rest/agile/1.0/sprint/42/issue":
            return _json(
                200,
                {
                    "issues": self.sprint_issues,
                    "total": len(self.sprint_issues),
                    "maxResults": 100,
                    "startAt": 0,
                },
            )
        if path == f"/rest/agile/1.0/board/{BOARD}/issue":
            return _json(
                200,
                {
                    "issues": self.board_issues,
                    "total": len(self.board_issues),
                    "maxResults": 100,
                    "startAt": 0,
                },
            )
        if path.startswith("/rest/api/2/issue/"):
            return _json(200, {"fields": {"assignee": None}})
        return _text(404, "not found")


def _client(api: AgileJira) -> JiraClient:
    c = JiraClient.__new__(JiraClient)
    c.host = HOST
    c.api_token = "test-token"
    c.email = ""
    c.last_error = None
    c.sprint_lookup = None
    c.is_cloud = False
    c.client = httpx.Client(transport=httpx.MockTransport(api.handler), verify=False)
    return c


@pytest.fixture
def trigger_settings():
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["bot"]
        s.trigger_assignee_names_list = []
        s.trigger_on_assignment = False
        yield s


def test_jira_api_sprint_500_does_not_call_board_issue(state_manager, trigger_settings):
    api = AgileJira(
        sprint_mode="500",
        board_issues=[_issue("BACKLOG-99", "must not intake")],
    )
    client = _client(api)
    assert client.get_active_sprint(BOARD) is None
    assert client.sprint_lookup == "error"
    assert client.last_error
    assert any("/sprint" in c for c in api.calls)
    assert not any(c.endswith(f"/board/{BOARD}/issue") for c in api.calls)

    api.calls.clear()
    poller = JiraPoller(client=client, interval_seconds=1, board_id=BOARD)
    poller.state_manager = state_manager
    result = poller.poll_board()
    assert result == []
    assert any("/sprint" in c for c in api.calls)
    assert not any(c.endswith(f"/board/{BOARD}/issue") for c in api.calls)


def test_jira_api_empty_active_sprint_does_not_call_board_issue(
    state_manager, trigger_settings
):
    api = AgileJira(
        sprint_mode="empty",
        board_issues=[_issue("BACKLOG-1", "between sprints")],
    )
    client = _client(api)
    assert client.get_active_sprint(BOARD) is None
    assert client.sprint_lookup == "empty"

    poller = JiraPoller(client=client, interval_seconds=1, board_id=BOARD)
    poller.state_manager = state_manager
    assert poller.poll_board() == []
    assert not any(c.endswith(f"/board/{BOARD}/issue") for c in api.calls)


def test_jira_api_kanban_400_loads_board_issues(state_manager, trigger_settings):
    api = AgileJira(
        sprint_mode="kanban",
        board_issues=[_issue("KAN-7", "kanban todo")],
    )
    client = _client(api)
    assert client.get_active_sprint(BOARD) is None
    assert client.sprint_lookup == "kanban"

    poller = JiraPoller(client=client, interval_seconds=1, board_id=BOARD)
    poller.state_manager = state_manager
    result = poller.poll_board()
    assert any(i["key"] == "KAN-7" for i in result)
    assert any(c.endswith(f"/board/{BOARD}/issue") for c in api.calls)


def test_jira_api_active_sprint_intakes_sprint_not_board(
    state_manager, trigger_settings
):
    api = AgileJira(
        sprint_mode="ok",
        sprint_issues=[_issue("SPR-1", "in sprint")],
        board_issues=[_issue("BACKLOG-1", "only on board")],
    )
    client = _client(api)
    sprint = client.get_active_sprint(BOARD)
    assert sprint is not None
    assert sprint["id"] == 42
    assert client.sprint_lookup == "ok"

    poller = JiraPoller(client=client, interval_seconds=1, board_id=BOARD)
    poller.state_manager = state_manager
    result = poller.poll_board()
    keys = {i["key"] for i in result}
    assert "SPR-1" in keys
    assert "BACKLOG-1" not in keys
    assert any("/sprint/42/issue" in c for c in api.calls)
    assert not any(c.endswith(f"/board/{BOARD}/issue") for c in api.calls)


def test_jira_api_pending_issue_is_not_requeued(state_manager, trigger_settings):
    api = AgileJira(
        sprint_mode="ok",
        sprint_issues=[
            _issue("PEND-1", "already accepted"),
            _issue("NEW-1", "fresh"),
        ],
    )
    client = _client(api)
    state_manager.create_state("PEND-1", "pending job", "d")
    assert state_manager.get_state("PEND-1").status == TaskStatus.PENDING

    poller = JiraPoller(client=client, interval_seconds=1, board_id=BOARD)
    poller.state_manager = state_manager
    result = poller.poll_board()
    keys = {i["key"] for i in result}
    assert "NEW-1" in keys
    assert "PEND-1" not in keys
    assert any("/sprint/42/issue" in c for c in api.calls)
