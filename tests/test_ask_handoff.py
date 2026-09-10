"""``@bot /ask`` is another agent. GitLab and Azure webhooks must not start a job."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gitlab.mentions import (
    ASK_HANDOFF_REASON,
    flatten_comment_text,
    note_is_ask_handoff,
)
from src.azure.webhook import decide_azure_comment_webhook, decide_azure_pr_webhook
from src.gitlab.webhook import decide_gitlab_mr_webhook, decide_gitlab_note_webhook

from tests.test_azure_webhook import _pr_comment_payload, _pr_lifecycle_payload
from tests.test_gitlab_webhook import _mr_lifecycle_payload, _mr_payload


BOTS_GL = ["@berat_ai"]
BOTS_AZ = ["@yaver"]
GL_HEADERS = {"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"}
AZ_HEADERS = {"X-Azure-Token": "s"}


def _gl(note: str, *, bots=None):
    return decide_gitlab_note_webhook(
        _mr_payload(note=note),
        headers=GL_HEADERS,
        secret="s",
        bot_mentions=bots if bots is not None else BOTS_GL,
        jira_project_keys=["KAN"],
    )


def _az(note: str, *, bots=None):
    return decide_azure_comment_webhook(
        _pr_comment_payload(note=note),
        headers=AZ_HEADERS,
        secret="s",
        bot_mentions=bots if bots is not None else BOTS_AZ,
        jira_project_keys=["KAN"],
    )


# --- helper: what counts as /ask ---------------------------------------------

@pytest.mark.parametrize(
    "note,bots,expect",
    [
        ("@berat_ai /ask what is auth?", ["berat_ai"], True),
        ("@berat_ai /ask", ["berat_ai"], True),
        ("@Berat_AI /ASK please", ["@berat_ai"], True),
        ("@berat_ai/ask no space", ["berat_ai"], True),
        ("@berat_ai\t/ask tab", ["berat_ai"], True),
        ("@berat_ai\n/ask newline", ["berat_ai"], True),
        ("  leading @berat_ai /ask mid", ["berat_ai"], True),
        ("please look\n@berat_ai /ask about tests", ["berat_ai"], True),
        ("@berat_ai  /ask   extra spaces", ["berat_ai"], True),
        ("@Yaver Bot /ask why", ["Yaver Bot"], True),
        ("@yaver /ask", ["Yaver Bot", "yaver"], True),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver</a> /ask html',
            ["yaver"],
            True,
        ),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a> /ask',
            ["yaver"],
            True,
        ),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a> /ask',
            ["Yaver Bot"],
            True,
        ),
        ("<p>@berat_ai /ask</p> wrapped", ["berat_ai"], True),
        ("@berat_ai /ask?", ["berat_ai"], True),
        ("@berat_ai /ask.", ["berat_ai"], True),
        ("@berat_ai /ask!", ["berat_ai"], True),
        # not a handoff
        ("@berat_ai please look", ["berat_ai"], False),
        ("@berat_ai implement login", ["berat_ai"], False),
        ("@berat_ai /asking is this a question", ["berat_ai"], False),
        ("@berat_ai /ask-review the diff", ["berat_ai"], False),
        ("@berat_ai /ask_me later", ["berat_ai"], False),
        ("@berat_ai please /ask later", ["berat_ai"], False),
        ("berat_ai /ask no at-sign", ["berat_ai"], False),
        ("@other /ask not our bot", ["berat_ai"], False),
        ("@berat_ai2 /ask similar name", ["berat_ai"], False),
        ("@berat_ai /AskMe", ["berat_ai"], False),
        ("", ["berat_ai"], False),
        ("@berat_ai /ask", [], False),
        ("@berat_ai /ask", None, False),
        ("lgtm", ["berat_ai"], False),
        # other bot /ask + our bot without /ask still not a handoff
        ("@oracle /ask\n@berat_ai please implement", ["berat_ai"], False),
    ],
)
def test_note_is_ask_handoff_matrix(note, bots, expect):
    assert note_is_ask_handoff(note, bots) is expect


def test_flatten_comment_text_keeps_chip_inner_and_strips_tags():
    html = (
        '<a href="#" data-vss-mention="version:2.0,guid">@Yaver</a> /ask'
    )
    flat = flatten_comment_text(html)
    assert "@Yaver" in flat
    assert "<a" not in flat
    assert flatten_comment_text("a&nbsp;b\xa0c") == "a b c"


def test_ask_handoff_reason_constant():
    assert ASK_HANDOFF_REASON == "ignored /ask handoff"


# --- GitLab decide -----------------------------------------------------------

@pytest.mark.parametrize(
    "note",
    [
        "@berat_ai /ask what does login do?",
        "@Berat_AI /ASK summarise this MR",
        "@berat_ai/ask compact",
        "@berat_ai\n/ask multiline",
        "hey team\n@berat_ai /ask about the API",
        "<p>@berat_ai /ask</p>",
    ],
)
def test_gitlab_decide_rejects_ask_handoff(note):
    d = _gl(note)
    assert d.accepted is False
    assert d.reason == ASK_HANDOFF_REASON
    assert d.event is None
    assert d.http_status == 200


@pytest.mark.parametrize(
    "note",
    [
        "@berat_ai /execute what does login do?",
        "@berat_ai /execute please implement the plan",
        "@berat_ai /execute /asking is this ok",
        "@berat_ai /execute /ask-review the diff",
        "@oracle /ask\n@berat_ai /execute ship it",
    ],
)
def test_gitlab_decide_still_accepts_normal_mention(note):
    d = _gl(note)
    assert d.accepted is True
    assert d.event is not None


def test_gitlab_ask_does_not_run_before_auth():
    d = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai /ask"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "bad"},
        secret="good",
        bot_mentions=BOTS_GL,
    )
    assert d.accepted is False
    assert d.http_status == 401
    assert d.reason == "invalid webhook token"


def test_gitlab_ask_after_bot_not_mentioned():
    d = _gl("@someone /ask hello")
    assert d.accepted is False
    assert d.reason == "bot not mentioned"


def test_gitlab_ask_second_configured_bot():
    d = _gl("@devbot /ask ping", bots=["berat_ai", "devbot"])
    assert d.accepted is False
    assert d.reason == ASK_HANDOFF_REASON


def test_gitlab_mr_lifecycle_not_affected_by_ask_text():
    d = decide_gitlab_mr_webhook(
        _mr_lifecycle_payload(title="feat(KAN-12): @berat_ai /ask"),
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.accepted is True
    assert d.event is not None
    assert d.event.is_merged


# --- Azure decide ------------------------------------------------------------

@pytest.mark.parametrize(
    "note",
    [
        "@yaver /ask what does login do?",
        "@Yaver /ASK summarise this PR",
        "@yaver/ask compact",
        "@yaver\n/ask multiline",
        "hey team\n@yaver /ask about the API",
        '<a href="#" data-vss-mention="version:2.0,g">@Yaver</a> /ask html',
        '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a> /ask',
    ],
)
def test_azure_decide_rejects_ask_handoff(note):
    d = _az(note)
    assert d.accepted is False
    assert d.reason == ASK_HANDOFF_REASON
    assert d.event is None
    assert d.http_status == 200


@pytest.mark.parametrize(
    "note",
    [
        "@yaver /execute what does login do?",
        "@yaver /execute please implement the plan",
        "@yaver /execute /asking is this ok",
        "@yaver /execute /ask-review the diff",
        "@oracle /ask\n@yaver /execute ship it",
    ],
)
def test_azure_decide_still_accepts_normal_mention(note):
    d = _az(note)
    assert d.accepted is True
    assert d.event is not None


def test_azure_display_name_ask_handoff():
    d = _az("@Yaver Bot /ask why is this failing", bots=["Yaver Bot"])
    assert d.accepted is False
    assert d.reason == ASK_HANDOFF_REASON


def test_azure_ask_ignored_without_webhook_secret():
    d = decide_azure_comment_webhook(
        _pr_comment_payload(note="@yaver /ask"),
        headers={},
        secret="ignored",
        bot_mentions=BOTS_AZ,
    )
    assert d.accepted is False
    assert d.reason == ASK_HANDOFF_REASON


def test_azure_ask_after_bot_not_mentioned():
    d = _az("@someone /ask hello")
    assert d.accepted is False
    assert d.reason == "bot not mentioned"


def test_azure_pr_lifecycle_not_affected_by_ask_title():
    d = decide_azure_pr_webhook(
        _pr_lifecycle_payload(title="feat(KAN-12): @yaver /ask"),
        headers=AZ_HEADERS,
        enabled=True,
        secret="s",
        jira_project_keys=["KAN"],
    )
    assert d.accepted is True
    assert d.event is not None
    assert d.event.is_merged


def test_azure_bot_reply_still_wins_over_ask():
    d = _az("*Yaver*\n\n@yaver /ask already replied")
    assert d.accepted is False
    assert d.reason == "ignored bot reply"


def test_gitlab_bot_reply_still_wins_over_ask():
    d = _gl("*Yaver*\n\n@berat_ai /ask already replied")
    assert d.accepted is False
    assert d.reason == "ignored bot reply"


# --- HTTP: processor must not be called --------------------------------------

def test_gitlab_http_ask_does_not_enqueue(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock()
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/gitlab",
            json=_mr_payload(note="@berat_ai /ask what is this?"),
            headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == ASK_HANDOFF_REASON
    proc.enqueue_gitlab_note.assert_not_awaited()


def test_azure_http_ask_does_not_enqueue(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock()
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/azure",
            json=_pr_comment_payload(note="@yaver /ask what is this?"),
            headers={"X-Azure-Token": "tok"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == ASK_HANDOFF_REASON
    proc.enqueue_azure_comment.assert_not_awaited()


def test_gitlab_http_normal_mention_still_enqueues(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "queue_id": "q1",
            "issue_key": "GL-ACME-DEMO-4",
            "status": "queued",
        }
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/gitlab",
            json=_mr_payload(note="@berat_ai /execute please implement"),
            headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "tok"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    proc.enqueue_gitlab_note.assert_awaited_once()


def test_azure_http_normal_mention_still_enqueues(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "queue_id": "q1",
            "issue_key": "AZ-1",
            "status": "queued",
        }
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/azure",
            json=_pr_comment_payload(note="@yaver /execute please implement"),
            headers={"X-Azure-Token": "tok"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    proc.enqueue_azure_comment.assert_awaited_once()


def test_azure_http_html_chip_ask_does_not_enqueue(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock()
    note = (
        '<a href="#" data-vss-mention="version:2.0,guid">@Yaver</a> /ask '
        "redirect this"
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/azure",
            json=_pr_comment_payload(note=note),
            headers={"X-Azure-Token": "tok"},
        )
    assert resp.status_code == 200
    assert resp.json()["reason"] == ASK_HANDOFF_REASON
    proc.enqueue_azure_comment.assert_not_awaited()
