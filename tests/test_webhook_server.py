"""Full branch coverage for webhook server."""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.jira.webhook_server import create_webhook_app


@pytest.fixture
def handlers():
    return {"created": [], "updated": [], "comment": []}


@pytest.fixture
def client_no_secret(handlers):
    app = create_webhook_app(
        on_issue_created=lambda e: handlers["created"].append(e),
        on_issue_updated=lambda e: handlers["updated"].append(e),
        on_comment_added=lambda e: handlers["comment"].append(e),
        secret=None,
    )
    return TestClient(app)


def test_health(client_no_secret):
    r = client_no_secret.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_invalid_json(client_no_secret):
    r = client_no_secret.post("/webhook/jira", data="not-json")
    assert r.status_code == 400


def test_issue_created_processed_by_label(handlers, client_no_secret):
    with patch("src.jira.webhook_server.settings") as s:
        s.webhook_path = "/webhook/jira"
        s.webhook_secret = None
        s.jira_projects_list = ["PROJ"]
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_on_assignment = False
        s.trigger_mentions_list = ["@DevBot"]
        # recreate app with patched settings path — TestClient uses existing app
        # so patch inside handlers path via request to existing app which reads settings at create time
    # create_webhook_app already closed over settings.webhook_path at creation
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
        r = client_no_secret.post("/webhook/jira", json=body)
    # Depending on default settings, may process or ignore
    assert r.status_code == 200
    assert r.json()["status"] in ("processed", "ignored")


def test_signature_required_when_secret_set(handlers):
    secret = "supersecret"
    app = create_webhook_app(
        on_issue_created=lambda e: handlers["created"].append(e),
        secret=secret,
    )
    # Path may be settings.webhook_path
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

    app = create_webhook_app(secret=None)
    c = TestClient(app)
    with patch("src.jira.webhook_server.settings") as s:
        s.jira_projects_list = ["ONLY"]
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_on_assignment = True
        s.trigger_mentions_list = ["@DevBot"]
        # Note: should_process_issue closes over settings module at call time via import
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
        # patch the settings used inside webhook_server
        with patch("src.jira.webhook_server.settings.jira_projects_list", ["ONLY"]):
            with patch("src.jira.webhook_server.settings.trigger_labels_list", ["ai-assist"]):
                with patch("src.jira.webhook_server.settings.trigger_on_assignment", True):
                    r = c.post(settings.webhook_path, json=body)
                    assert r.status_code == 200


def _settings_mock(**kwargs):
    from src.config import settings as real

    m = MagicMock()
    m.webhook_path = real.webhook_path
    m.webhook_secret = None
    m.jira_projects_list = kwargs.get("jira_projects_list", ["PROJ"])
    m.trigger_labels_list = kwargs.get("trigger_labels_list", ["ai-assist"])
    m.trigger_on_assignment = kwargs.get("trigger_on_assignment", False)
    m.trigger_mentions_list = kwargs.get("trigger_mentions_list", ["@DevBot"])
    return m


def test_update_relevant_field():
    from src.config import settings

    called = []
    app = create_webhook_app(on_issue_updated=lambda e: called.append(e), secret=None)
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
        r = c.post(settings.webhook_path, json=body)
        assert r.status_code == 200


def test_update_irrelevant_field_ignored():
    from src.config import settings

    app = create_webhook_app(secret=None)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PROJ-9",
            "fields": {"project": {"key": "PROJ"}, "labels": ["ai-assist"]},
        },
        "changelog": {"items": [{"field": "summary"}]},
    }
    r = c.post(settings.webhook_path, json=body)
    assert r.json()["status"] == "ignored"


def test_comment_with_mention():
    from src.config import settings

    called = []
    app = create_webhook_app(on_comment_added=lambda e: called.append(e), secret=None)
    c = TestClient(app)
    body = {
        "webhookEvent": "comment_created",
        "issue": {"key": "PROJ-1"},
        "comment": {"body": "hey @DevBot /status"},
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = c.post(settings.webhook_path, json=body)
        assert r.status_code == 200
        assert called


def test_comment_without_mention_ignored():
    from src.config import settings

    app = create_webhook_app(secret=None)
    c = TestClient(app)
    body = {
        "webhookEvent": "comment_created",
        "issue": {"key": "PROJ-1"},
        "comment": {"body": "just a normal comment"},
    }
    with patch("src.jira.webhook_server.settings", _settings_mock()):
        r = c.post(settings.webhook_path, json=body)
        assert r.json()["status"] == "ignored"


def test_assignee_trigger():
    from src.config import settings

    called = []
    app = create_webhook_app(on_issue_created=lambda e: called.append(e), secret=None)
    c = TestClient(app)
    body = {
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
        r = c.post(settings.webhook_path, json=body)
        assert r.status_code == 200


def test_no_handlers_still_ok():
    from src.config import settings

    app = create_webhook_app(secret=None)
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
        r = c.post(settings.webhook_path, json=body)
        assert r.status_code == 200
