"""Dashboard username/password must not block poller or GitLab webhooks."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.auth import credentials_ok, dashboard_auth_enabled, parse_basic_header
def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_auth_off_when_username_or_password_empty(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "")
    assert dashboard_auth_enabled() is False
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "secret")
    assert dashboard_auth_enabled() is False


def test_settings_require_login_when_configured(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    client = TestClient(create_dashboard_app())
    denied = client.get("/api/settings")
    assert denied.status_code == 401
    assert "basic" not in (denied.headers.get("www-authenticate") or "").lower()
    page = client.get("/")
    assert page.status_code == 200
    chrome = client.get("/api/settings", headers={"Authorization": _basic("ops", "s3cret")})
    assert chrome.status_code == 401
    ok = client.get(
        "/api/settings",
        headers={
            "Authorization": _basic("ops", "s3cret"),
            "X-Yaver-Login": "1",
        },
    )
    assert ok.status_code == 200
    client.cookies.clear()
    wrong = client.get(
        "/api/settings",
        headers={"Authorization": _basic("ops", "nope"), "X-Yaver-Login": "1"},
    )
    assert wrong.status_code == 401


def test_logout_clears_cookie_and_locks_api(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    client = TestClient(create_dashboard_app())
    login = client.post("/api/login", json={"username": "ops", "password": "s3cret"})
    assert login.status_code == 200
    assert client.get("/api/settings").status_code == 200
    out = client.post("/api/logout")
    assert out.status_code == 200
    locked = client.get(
        "/api/settings",
        headers={"Authorization": _basic("ops", "s3cret")},
    )
    assert locked.status_code == 401


def test_logout_handler_is_async():
    """Sign out must not wait for a sync thread-pool worker."""
    import asyncio
    import inspect

    app = create_dashboard_app()
    route = next(
        r
        for r in app.routes
        if getattr(r, "path", None) == "/api/logout"
        and "POST" in getattr(r, "methods", set())
    )
    assert inspect.iscoroutinefunction(route.endpoint) or asyncio.iscoroutinefunction(
        route.endpoint
    )


def test_health_stays_open_when_dashboard_login_set(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    client = TestClient(create_dashboard_app())
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


def test_gitlab_webhook_ignores_dashboard_password(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "hook-tok")
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    client = TestClient(create_dashboard_app())
    payload = {
        "object_kind": "merge_request",
        "project": {
            "id": 1,
            "path_with_namespace": "acme/demo",
            "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
        },
        "object_attributes": {
            "iid": 4,
            "action": "merge",
            "state": "merged",
            "title": "feat(KAN-12): x",
            "source_branch": "feature/KAN-12",
            "target_branch": "develop",
            "url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
        },
    }
    r = client.post(
        "/webhooks/gitlab",
        json=payload,
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Token": "hook-tok",
        },
    )
    # Processor is unbound in this app — 503 after auth, not 401.
    assert r.status_code != 401
    assert r.status_code in {200, 503}


def test_parse_basic_and_credentials(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    user, password = parse_basic_header(_basic("ops", "s3cret"))
    assert user == "ops"
    assert password == "s3cret"
    assert credentials_ok("ops", "s3cret") is True
    assert credentials_ok("ops", "other") is False
