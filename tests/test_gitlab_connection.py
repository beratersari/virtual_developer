"""GitLab connection test (settings Test button)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from src.gitlab_connection import probe_gitlab_connection


def _resp(status: int, data=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.content = b"x" if data is not None else b""
    r.text = text or ("" if data is None else "json")
    r.json.return_value = data
    return r


def test_connection_ok_lists_user_and_projects():
    user = _resp(200, {"id": 1, "username": "devbot", "name": "Dev Bot"})
    projects = _resp(
        200,
        [
            {
                "id": 10,
                "name": "App",
                "path_with_namespace": "org/app",
                "web_url": "https://gitlab.com/org/app",
                "visibility": "private",
            }
        ],
    )
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = [user, projects]

    with patch("src.gitlab_connection.httpx.Client", return_value=client):
        out = probe_gitlab_connection("gitlab.com", pat="glpat-test")

    assert out["ok"] is True
    assert out["host"] == "gitlab.com"
    assert out["user"]["username"] == "devbot"
    assert out["project_count"] == 1
    assert out["projects"][0]["path_with_namespace"] == "org/app"
    assert "glpat-test" not in str(out)


def test_connection_unauthorized():
    user = _resp(401, text="401")
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = user

    with patch("src.gitlab_connection.httpx.Client", return_value=client):
        out = probe_gitlab_connection("gitlab.com", pat="bad")

    assert out["ok"] is False
    assert "401" in out["error"] or "Unauthorized" in out["error"]


def test_connection_uses_stored_pat(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(
        settings,
        "gitlab_host_pats",
        '{"gitlab.com":"stored-pat"}',
    )
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")

    user = _resp(200, {"id": 2, "username": "stored"})
    projects = _resp(200, [])
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = [user, projects]

    with patch("src.gitlab_connection.httpx.Client", return_value=client) as C:
        out = probe_gitlab_connection("gitlab.com", pat=None)
    assert out["ok"] is True
    # Client constructed with headers
    assert C.call_args.kwargs["headers"]["PRIVATE-TOKEN"] == "stored-pat"


def test_connection_missing_pat(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    out = probe_gitlab_connection("gitlab.com", pat="")
    assert out["ok"] is False
    assert "No PAT" in out["error"]


def test_connection_timeout():
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.side_effect = httpx.TimeoutException("timeout")

    with patch("src.gitlab_connection.httpx.Client", return_value=client):
        out = probe_gitlab_connection("gitlab.example.com", pat="x")
    assert out["ok"] is False
    assert "Timed out" in out["error"]


def test_api_gitlab_test_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.state.manager import JiraStateManager

    sm = JiraStateManager(state_dir=tmp_path / "state")
    with monkeypatch.context() as m:
        m.setattr(
            "src.dashboard.api.probe_gitlab_connection",
            lambda host, pat=None, max_projects=25: {
                "ok": True,
                "host": host,
                "user": {"username": "u"},
                "projects": [],
                "project_count": 0,
                "message": "ok",
            },
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)
        r = tc.post(
            "/api/settings/gitlab/test",
            json={"host": "gitlab.com", "pat": "glpat-x"},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["host"] == "gitlab.com"
