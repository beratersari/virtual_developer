"""Release-site Analytics read: built-in bearer, dashboard page stays locked."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.auth import is_exempt_path
from src.dashboard.install_analytics import INSTALL_ANALYTICS_TOKEN


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _sample_store(tmp_path: Path):
    from src.state.job_store import JobStore

    store = JobStore(jobs_dir=tmp_path / "jobs")
    built = store.create_job(
        issue_key="KAN-1",
        summary="build the login",
        workflow_type="execution",
        status="completed",
        source="jira",
        model="opencode/deepseek",
        backend="opencode",
        agent="derman-build",
        repository_url="https://user:secret-token@gitlab.example/group/app.git",
        merge_request_url="https://gitlab.example/group/app/-/merge_requests/7",
    )
    assert built is not None
    assert store.update_job(built["job_id"], status="completed", merge_request_state="merged")
    planned = store.create_job(
        issue_key="KAN-2",
        summary="plan the login",
        workflow_type="planning",
        status="error",
        source="jira",
        model="opencode/deepseek",
        backend="opencode",
        agent="derman-plan",
    )
    assert planned is not None
    return store


def test_only_the_install_path_skips_the_dashboard_password():
    assert is_exempt_path("GET", "/api/analytics/install") is True
    assert is_exempt_path("GET", "/api/analytics") is False
    assert is_exempt_path("GET", "/api/analytics/reviews") is False
    assert is_exempt_path("POST", "/api/analytics/install") is False
    assert is_exempt_path("GET", "/api/analytics/install/extra") is False


def test_install_analytics_requires_the_bearer_and_the_page_stays_locked(monkeypatch, tmp_path):
    store = _sample_store(tmp_path)
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", store)
    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    client = TestClient(create_dashboard_app())

    locked = client.get("/api/analytics")
    assert locked.status_code == 403
    assert locked.json().get("code") == "login_required"
    reviews = client.get("/api/analytics/reviews")
    assert reviews.status_code == 403
    assert reviews.json().get("code") == "login_required"
    posted = client.post("/api/analytics/install", headers=_bearer(INSTALL_ANALYTICS_TOKEN))
    assert posted.status_code == 403
    assert posted.json().get("code") == "login_required"

    missing = client.get("/api/analytics/install")
    assert missing.status_code == 403
    assert missing.json().get("detail") == "Forbidden"
    assert missing.json().get("code") != "login_required"
    assert "www-authenticate" not in {key.lower() for key in missing.headers}
    assert missing.headers.get("cache-control") == "no-store"
    queried = client.get("/api/analytics/install", params={"token": INSTALL_ANALYTICS_TOKEN})
    assert queried.status_code == 403
    wrong = client.get("/api/analytics/install", headers=_bearer("not-the-token"))
    assert wrong.status_code == 403
    short = client.get("/api/analytics/install", headers=_bearer(INSTALL_ANALYTICS_TOKEN[:-1]))
    assert short.status_code == 403

    opened = client.get("/api/analytics/install", headers=_bearer(INSTALL_ANALYTICS_TOKEN))
    assert opened.status_code == 200
    assert opened.headers.get("cache-control") == "no-store"
    body = opened.json()
    assert body["jobs"] == 2
    assert body["completed"] == 1
    assert body["error"] == 1
    assert body["merge_requests"] == 1
    assert body["merged"] == 1
    assert body["ours"]["merged"] == 1
    raw = opened.text
    assert "secret-token" not in raw
    assert "https://gitlab.example/group/app" in raw
    assert any(row["label"] == "Build" for row in body["categories"])
    assert any(row["label"] == "Plan" for row in body["categories"])
    assert INSTALL_ANALYTICS_TOKEN not in raw


def test_install_route_still_requires_the_bearer_when_dashboard_login_is_off(monkeypatch, tmp_path):
    store = _sample_store(tmp_path)
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", store)
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")
    client = TestClient(create_dashboard_app())
    missing = client.get("/api/analytics/install")
    assert missing.status_code == 403
    page = client.get("/api/analytics", params={"period": "all"})
    assert page.status_code == 200
    opened = client.get(
        "/api/analytics/install",
        headers={"Authorization": f"bearer {INSTALL_ANALYTICS_TOKEN}"},
    )
    assert opened.status_code == 200
    assert opened.json()["jobs"] == 2


def test_release_site_constant_matches_this_install():
    release = Path(r"C:\Users\BERAT\yaver-releases\yaver_releases\install_fetch.py")
    if not release.is_file():
        return
    text = release.read_text(encoding="utf-8")
    assert f'INSTALL_ANALYTICS_TOKEN = "{INSTALL_ANALYTICS_TOKEN}"' in text
