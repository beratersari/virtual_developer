"""Coverage for SimulatedJiraClient."""

from unittest.mock import MagicMock, patch

import pytest

from src.jira.simulated_client import SimulatedJiraClient


def _resp(code, json_data=None):
    r = MagicMock()
    r.status_code = code
    r.json.return_value = json_data or {}
    return r


@pytest.fixture
def client():
    with patch("src.jira.simulated_client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        c = SimulatedJiraClient("http://localhost:7001")
        c.client = http
        yield c, http


def test_create_issue_ok_and_fail(client):
    c, http = client
    http.post.return_value = _resp(201, {"issue": {"key": "S-1"}})
    assert c.create_issue("s", "d", key="S-1", labels=["a"])["key"] == "S-1"
    http.post.return_value = _resp(400)
    assert c.create_issue("s", "d") is None


def test_get_list_comment_assign(client):
    c, http = client
    http.get.return_value = _resp(200, {"key": "S-1"})
    assert c.get_issue("S-1")["key"] == "S-1"
    http.get.return_value = _resp(404)
    assert c.get_issue("S-1") is None

    http.get.return_value = _resp(200, {"issues": [{"key": "A"}]})
    assert c.list_issues() == [{"key": "A"}]
    http.get.return_value = _resp(500)
    assert c.list_issues() == []

    http.post.return_value = _resp(201, {"comment": {"id": "1"}})
    assert c.add_comment("S-1", "hi")["id"] == "1"
    http.post.return_value = _resp(400)
    assert c.add_comment("S-1", "hi") is None

    http.post.return_value = _resp(200, {"issue": {"key": "S-1"}})
    assert c.assign_issue("S-1", "bot")["key"] == "S-1"
    http.post.return_value = _resp(500)
    assert c.assign_issue("S-1", "bot") is None


def test_context_manager(client):
    c, http = client
    with c:
        pass
    http.close.assert_called()
    c.close()
