"""E2E: JIRA_EMAIL from .env until Settings save, then token-only Bearer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import apply_settings_update
from src.dashboard.schemas import SettingsUpdate
from src.jira.client import JiraClient
from src.state.manager import JiraStateManager

SETTINGS_PAGE = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "src"
    / "pages"
    / "settings"
    / "SettingsPage.tsx"
)


def test_e2e_settings_page_has_no_jira_email_field():
    src = SETTINGS_PAGE.read_text(encoding="utf-8")
    assert "jira_email" not in src
    assert 'type="email"' not in src
    assert "Cloud: account email" not in src
    assert "JIRA_EMAIL" in src  # help text about .env / save-clears


def test_e2e_env_email_uses_basic_until_settings_save(tmp_path, monkeypatch):
    """Exact flow: .env email → Basic; Settings save → empty email → Bearer."""
    from src.config import load_runtime_settings, settings

    env_path = tmp_path / ".env"
    env_path.write_text(
        "JIRA_HOST=https://cloud.example.atlassian.net\n"
        "JIRA_EMAIL=dev@example.com\n"
        "JIRA_API_TOKEN=cloud-token\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "jira_host", "https://cloud.example.atlassian.net")
    monkeypatch.setattr(settings, "jira_email", "dev@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "cloud-token")

    with patch("src.jira.client.httpx.Client") as mock_cls:
        JiraClient()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs.get("auth") == ("dev@example.com", "cloud-token")
        assert "Authorization" not in kwargs.get("headers", {})

    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    http = TestClient(app)

    before = http.get("/api/settings")
    assert before.status_code == 200
    assert before.json()["jira_email"] == "dev@example.com"
    assert before.json()["jira_email_configured"] is True

    # Any Settings save (even poll interval only) must wipe email.
    saved = http.patch("/api/settings", json={"poll_interval_seconds": 45})
    assert saved.status_code == 200
    body = saved.json()
    assert body["jira_email"] == ""
    assert body["jira_email_configured"] is False
    assert settings.jira_email == ""
    assert settings.poll_interval_seconds == 45

    env_text = env_path.read_text(encoding="utf-8")
    assert "dev@example.com" not in env_text
    assert "JIRA_EMAIL=" in env_text

    runtime = load_runtime_settings()
    assert runtime.get("jira_email") == ""

    after = http.get("/api/settings")
    assert after.json()["jira_email"] == ""
    assert after.json()["jira_email_configured"] is False

    with patch("src.jira.client.httpx.Client") as mock_cls:
        JiraClient()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs.get("auth") is None
        assert kwargs["headers"]["Authorization"] == "Bearer cloud-token"


def test_e2e_settings_save_ignores_posted_jira_email(tmp_path, monkeypatch):
    """A client that still sends jira_email cannot keep Cloud Basic after save."""
    from src.config import settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "JIRA_EMAIL=keep-me@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "jira_email", "keep-me@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok")
    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")

    view = apply_settings_update(
        SettingsUpdate(jira_email="still-here@example.com", poll_interval_seconds=30)
    )
    assert settings.jira_email == ""
    assert view.jira_email == ""
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "keep-me@example.com" not in env_text
    assert "still-here@example.com" not in env_text


def test_e2e_settings_save_refreshes_processor_client_to_bearer(tmp_path, monkeypatch):
    """Live processor Jira client must switch from Basic to Bearer after save."""
    from src.config import settings
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "jira_host", "https://x.atlassian.net")
    monkeypatch.setattr(settings, "jira_email", "ops@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok-123")

    created: list[dict] = []

    class _FakeClient:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.headers = kwargs.get("headers") or {}
            self.auth = kwargs.get("auth")

        def close(self):
            return None

    with patch("src.jira.client.httpx.Client", side_effect=_FakeClient):
        proc = JobProcessor()
        assert created
        first = created[0]
        assert first.get("auth") == ("ops@example.com", "tok-123")

        sm = JiraStateManager(state_dir=tmp_path / "state")
        app = create_dashboard_app(processor=proc, state_manager=sm)
        http = TestClient(app)
        r = http.patch("/api/settings", json={"trigger_labels": "bot"})
        assert r.status_code == 200
        assert settings.jira_email == ""
        assert len(created) >= 2
        last = created[-1]
        assert last.get("auth") is None
        assert last.get("headers", {}).get("Authorization") == "Bearer tok-123"
