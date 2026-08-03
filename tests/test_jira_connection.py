"""Jira connection probe (settings Test connection)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from src.jira_connection import probe_jira_connection


def _resp(status: int, data=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.content = b"x" if data is not None else b""
    r.text = text or ""
    r.json.return_value = data
    return r


def test_jira_probe_ok():
    me = _resp(
        200,
        {
            "displayName": "Dev Bot",
            "accountId": "abc",
            "emailAddress": "bot@ex.com",
        },
    )
    projects = _resp(
        200,
        [
            {
                "id": "10000",
                "key": "KAN",
                "name": "Kanban",
                "projectTypeKey": "software",
            }
        ],
    )
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = [me, projects]

    with patch("src.jira_connection.httpx.Client", return_value=client) as C:
        out = probe_jira_connection(
            host="https://ex.atlassian.net",
            api_token="secret-token",
        )
    assert out["ok"] is True
    assert out["user"]["display_name"] == "Dev Bot"
    assert out["projects"][0]["key"] == "KAN"
    assert "secret-token" not in str(out)
    assert out["auth_mode"] == "bearer"
    headers = C.call_args.kwargs["headers"]
    assert headers.get("Authorization") == "Bearer secret-token"
    assert C.call_args.kwargs.get("auth") is None


def test_jira_probe_unauthorized():
    me = _resp(401, text="nope")
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = me

    with patch("src.jira_connection.httpx.Client", return_value=client):
        out = probe_jira_connection(
            host="https://jira.example.com",
            api_token="bad",
        )
    assert out["ok"] is False
    assert out["http_status"] == 401
    assert out["auth_mode"] == "bearer"
    err = (out.get("error") or "").lower()
    assert "basic auth" not in err
    assert "set jira email" not in err


def test_jira_probe_cloud_host_still_bearer():
    """Cloud host uses Bearer (host + token only)."""
    me = _resp(200, {"displayName": "Cloud User", "accountId": "1"})
    projects = _resp(200, [])
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = [me, projects]

    with patch("src.jira_connection.httpx.Client", return_value=client) as C:
        out = probe_jira_connection(
            host="https://site.atlassian.net",
            api_token="pat-xyz",
        )
    assert out["ok"] is True
    assert out["auth_mode"] == "bearer"
    assert C.call_args.kwargs["headers"]["Authorization"] == "Bearer pat-xyz"
    assert C.call_args.kwargs.get("auth") is None


def test_jira_probe_uses_stored_token(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")
    monkeypatch.setattr(settings, "jira_api_token", "stored-pat")

    me = _resp(200, {"displayName": "OnPrem", "name": "onprem"})
    projects = _resp(200, [])
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = [me, projects]

    with patch("src.jira_connection.httpx.Client", return_value=client) as C:
        out = probe_jira_connection()
    assert out["ok"] is True
    # Bearer header for on-prem
    headers = C.call_args.kwargs["headers"]
    assert "Bearer stored-pat" in headers.get("Authorization", "")


def test_jira_probe_timeout():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = httpx.TimeoutException("t")

    with patch("src.jira_connection.httpx.Client", return_value=client):
        out = probe_jira_connection(
            host="https://jira.example.com",
            api_token="x",
        )
    assert out["ok"] is False
    assert "Timed out" in out["error"]


def test_api_jira_test_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.state.manager import JiraStateManager

    sm = JiraStateManager(state_dir=tmp_path / "state")
    with monkeypatch.context() as m:
        m.setattr(
            "src.dashboard.api.probe_jira_connection",
            lambda **kwargs: {
                "ok": True,
                "host": kwargs.get("host") or "https://j",
                "user": {"display_name": "U"},
                "projects": [{"key": "P", "name": "Proj"}],
                "project_count": 1,
                "message": "ok",
            },
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)
        r = tc.post(
            "/api/settings/jira/test",
            json={
                "host": "https://ex.atlassian.net",
                "api_token": "tok",
            },
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["project_count"] == 1
