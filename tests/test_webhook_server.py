"""Full branch coverage for webhook server."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.jira.webhook_server import create_webhook_app

TEST_SECRET = "test-webhook-secret"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post_signed(client, path: str, payload: dict, secret: str = TEST_SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        path,
        content=body,
        headers={"X-Hub-Signature": _sign(body, secret), "Content-Type": "application/json"},
    )


@pytest.fixture
def handlers():
    return {"created": [], "updated": [], "comment": []}


@pytest.fixture
def client_signed(handlers):
    app = create_webhook_app(
        on_issue_created=lambda e: handlers["created"].append(e),
        on_issue_updated=lambda e: handlers["updated"].append(e),
        on_comment_added=lambda e: handlers["comment"].append(e),
        secret=TEST_SECRET,
    )
    return TestClient(app)


def test_health(client_signed):
    r = client_signed.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_invalid_json(client_signed):
    body = b"not-json"
    r = client_signed.post(
        "/webhook/jira",
        content=body,
        headers={"X-Hub-Signature": _sign(body)},
    )
    assert r.status_code == 400


def test_missing_secret_rejects_all():
    """Unauthenticated webhooks must fail closed when secret is not configured."""
    app = create_webhook_app(secret=None)
    c = TestClient(app)
    from src.config import settings

    r = c.post(settings.webhook_path, json={"webhookEvent": "jira:issue_created"})
    assert r.status_code == 401


def test_issue_created_processed_by_label(handlers, client_signed):
    body = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "PROJ-1",
            "fields": {
                "project": {"key": "PROJ"},
                "labels": ["ai-assist"],
                "assignee": None,
            },
        },
    }
    with patch("src.jira.webhook_server.settings") as s:
        s.jira_projects_list = ["PROJ"]
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_on_assignment = False
        r = _post_signed(client_signed, "/webhook/jira", body)
    assert r.status_code == 200
    assert r.json()["status"] in ("processed", "ignored")


def test_signature_required_when_secret_set(handlers):
    secret = "supersecret"
    app = create_webhook_app(
        on_issue_created=lambda e: handlers["created"].append(e),
        secret=secret,
    )
    from src.config import settings

    c = TestClient(app)
    body = json.dumps(
        {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "P-1",
                "fields": {
                    "project": {"key": "PROJ"},
                    "labels": ["ai-assist"],
                },
            },
        }
    ).encode()
    r = c.post(settings.webhook_path, content=body)
    assert r.status_code == 401

    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r2 = c.post(settings.webhook_path, content=body, headers={"X-Hub-Signature": sig})
    assert r2.status_code == 200


def test_wrong_project_ignored():
    from src.config import settings

    app = create_webhook_app(secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "OTHER-1",
            "fields": {
                "project": {"key": "OTHER"},
                "labels": [],
                "assignee": {"name": "u"},
            },
        },
    }
    with patch(
        "src.jira.webhook_server.settings",
        _settings_mock(jira_projects_list=["ONLY"], trigger_on_assignment=True),
    ):
        r = _post_signed(c, settings.webhook_path, body)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"


def _settings_mock(**kwargs):
    from src.config import settings as real

    m = MagicMock()
    m.webhook_path = real.webhook_path
    m.webhook_secret = TEST_SECRET
    m.jira_projects_list = kwargs.get("jira_projects_list", ["PROJ"])
    m.trigger_labels_list = kwargs.get("trigger_labels_list", ["ai-assist"])
    m.trigger_on_assignment = kwargs.get("trigger_on_assignment", False)
    m.trigger_mentions_list = kwargs.get("trigger_mentions_list", ["@DevBot"])
    return m


def test_update_relevant_field():
    from src.config import settings

    called = []
    app = create_webhook_app(on_issue_updated=lambda e: called.append(e), secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PROJ-9",
            "fields": {
                "project": {"key": "PROJ"},
                "labels": ["ai-assist"],
                "assignee": None,
            },
        },
        "changelog": {"items": [{"field": "labels", "to": "ai-assist"}]},
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = _post_signed(c, settings.webhook_path, body)
        assert r.status_code == 200


def test_update_irrelevant_field_ignored():
    from src.config import settings

    app = create_webhook_app(secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PROJ-9",
            "fields": {"project": {"key": "PROJ"}, "labels": ["ai-assist"]},
        },
        "changelog": {"items": [{"field": "summary"}]},
    }
    r = _post_signed(c, settings.webhook_path, body)
    assert r.json()["status"] == "ignored"


def test_comment_with_mention():
    from src.config import settings

    called = []
    app = create_webhook_app(on_comment_added=lambda e: called.append(e), secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "comment_created",
        "issue": {"key": "PROJ-1"},
        "comment": {"body": "hey @DevBot /status"},
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = _post_signed(c, settings.webhook_path, body)
        assert r.status_code == 200
        assert called


def test_comment_without_mention_ignored():
    from src.config import settings

    app = create_webhook_app(secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "comment_created",
        "issue": {"key": "PROJ-1"},
        "comment": {"body": "just a normal comment"},
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = _post_signed(c, settings.webhook_path, body)
        assert r.json()["status"] == "ignored"


def test_assignee_trigger_only_bot():
    from src.config import settings

    called = []
    app = create_webhook_app(on_issue_created=lambda e: called.append(e), secret=TEST_SECRET)
    c = TestClient(app)

    # Random user must NOT trigger
    body_user = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "PROJ-2",
            "fields": {
                "project": {"key": "PROJ"},
                "labels": [],
                "assignee": {"displayName": "Someone"},
            },
        },
    }
    with patch(
        "src.jira.webhook_server.settings",
        _settings_mock(trigger_on_assignment=True, trigger_labels_list=[]),
    ):
        r = _post_signed(c, settings.webhook_path, body_user)
        assert r.status_code == 200
        assert r.json()["status"] == "ignored"
        assert not called

        body_bot = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "PROJ-3",
                "fields": {
                    "project": {"key": "PROJ"},
                    "labels": [],
                    "assignee": {"displayName": "JIRA AI Bot"},
                },
            },
        }
        r2 = _post_signed(c, settings.webhook_path, body_bot)
        assert r2.status_code == 200
        assert r2.json()["status"] == "processed"
        assert called


def test_no_handlers_still_ok():
    from src.config import settings

    app = create_webhook_app(secret=TEST_SECRET)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "PROJ-3",
            "fields": {
                "project": {"key": "PROJ"},
                "labels": ["ai-assist"],
            },
        },
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = _post_signed(c, settings.webhook_path, body)
        assert r.status_code == 200
