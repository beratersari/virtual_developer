"""``@bot /yaver`` starts a job. Mention without it posts a usage note."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.gitlab.mentions import (
    EXECUTE_MISSING_REASON,
    author_is_configured_bot,
    format_execute_usage_note,
    identity_key,
    note_is_execute_command,
    strip_slash_command,
)
from src.azure.webhook import decide_azure_comment_webhook, extract_azure_thread_id
from src.gitlab.webhook import decide_gitlab_note_webhook

from tests.test_azure_webhook import _pr_comment_payload
from tests.test_gitlab_webhook import _mr_payload


BOTS_GL = ["@berat_ai"]
BOTS_AZ = ["@yaver"]
GL_HEADERS = {"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"}
AZ_HEADERS = {"X-Azure-Token": "s"}


def _gl(note: str, *, username: str = "alice", bots=None):
    return decide_gitlab_note_webhook(
        _mr_payload(note=note, username=username),
        headers=GL_HEADERS,
        secret="s",
        bot_mentions=bots if bots is not None else BOTS_GL,
        jira_project_keys=["KAN"],
    )


def _az(note: str, *, unique_name: str = "DOMAIN\\alice", bots=None):
    return decide_azure_comment_webhook(
        _pr_comment_payload(note=note, unique_name=unique_name),
        headers=AZ_HEADERS,
        secret="s",
        bot_mentions=bots if bots is not None else BOTS_AZ,
        jira_project_keys=["KAN"],
    )


@pytest.mark.parametrize(
    "note,bots,expect",
    [
        ("@berat_ai /yaver fix login", ["berat_ai"], True),
        ("@Berat_AI /YAVER please", ["@berat_ai"], True),
        ("@berat_ai/yaver compact", ["berat_ai"], True),
        ("@berat_ai\n/yaver multiline", ["berat_ai"], True),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver</a> /yaver html',
            ["yaver"],
            True,
        ),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a>&nbsp;/yaver fix',
            ["yaver"],
            True,
        ),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a>'
            "<span> </span>/yaver fix",
            ["yaver"],
            True,
        ),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@Yaver Bot</a><br>/yaver fix',
            ["CORP\\Yaver"],
            True,
        ),
        ("@Yaver Bot /yaver fix", ["yaver"], True),
        ("@yaver @alice /yaver fix", ["yaver"], False),
        (
            '<a href="#" data-vss-mention="version:2.0,g">@yaver</a> '
            '<a href="#" data-vss-mention="version:2.0,g2">@alice</a> /yaver fix',
            ["yaver"],
            False,
        ),
        ("@berat_ai please look", ["berat_ai"], False),
        ("@berat_ai /executing", ["berat_ai"], False),
        ("@berat_ai /yaver-now", ["berat_ai"], False),
        ("@other /yaver", ["berat_ai"], False),
        (
            '<a href="#" data-vss-mention="version:2.0,'
            'ad96260c-ea80-6eeb-93b0-c942399631d0"></a>&nbsp;/yaver fix',
            ["ad96260c-ea80-6eeb-93b0-c942399631d0"],
            True,
        ),
        (
            "@<ad96260c-ea80-6eeb-93b0-c942399631d0> /yaver fix",
            ["ad96260c-ea80-6eeb-93b0-c942399631d0"],
            True,
        ),
        ("", ["berat_ai"], False),
    ],
)
def test_note_is_execute_command_matrix(note, bots, expect):
    assert note_is_execute_command(note, bots) is expect


def test_strip_execute_leaves_the_task_text():
    assert strip_slash_command("/yaver fix login", "yaver") == "fix login"
    assert strip_slash_command("fix /yaver login", "yaver") == "fix login"


def test_identity_key_uses_account_tail():
    from src.gitlab.mentions import parse_mention_list

    assert identity_key("DOMAIN\\yaver") == "yaver"
    assert identity_key("yaver@corp.local") == "yaver"
    assert identity_key("@Yaver") == "yaver"
    assert parse_mention_list("CORP\\Yaver, otherbot") == ["yaver", "otherbot"]
    assert author_is_configured_bot(["DOMAIN\\yaver"], ["yaver"])
    assert not author_is_configured_bot(["DOMAIN\\alice"], ["yaver"])
    assert not author_is_configured_bot(["CORP\\alice"], ["CORP\\yaver"])


def test_gitlab_accepts_execute_and_strips_it():
    d = _gl("@berat_ai /yaver what does login do?")
    assert d.accepted is True
    assert d.usage_note is False
    assert d.event is not None
    assert d.event.prompt == "what does login do?"
    assert d.event.discussion_id == "disc-1"


def test_azure_accepts_execute_and_strips_it():
    d = _az("@yaver /yaver what does login do?")
    assert d.accepted is True
    assert d.usage_note is False
    assert d.event is not None
    assert d.event.prompt == "what does login do?"
    assert d.event.thread_id == "8"


def test_azure_accepts_tfs_chip_nbsp_then_execute():
    note = (
        '<a href="#" data-vss-mention="version:2.0,guid">@Yaver Bot</a>'
        "&nbsp;/yaver fix the tests"
    )
    d = _az(note, bots=["CORP\\Yaver"])
    assert d.accepted is True, d.reason
    assert d.usage_note is False
    assert d.event is not None
    assert "fix the tests" in d.event.prompt


def test_azure_accepts_guid_chip_when_trigger_is_guid():
    bot_id = "ad96260c-ea80-6eeb-93b0-c942399631d0"
    note = (
        f'<a href="#" data-vss-mention="version:2.0,{bot_id}"></a>'
        "&nbsp;/yaver fix the tests"
    )
    d = _az(note, bots=[bot_id])
    assert d.accepted is True, d.reason
    assert d.usage_note is False


def test_azure_accepts_markdown_guid_mention():
    bot_id = "ad96260c-ea80-6eeb-93b0-c942399631d0"
    d = _az(f"@<{bot_id}> /yaver fix the tests", bots=[bot_id])
    assert d.accepted is True, d.reason


def test_azure_two_mentions_then_execute_is_usage_note():
    d = _az("@yaver @alice /yaver fix the tests")
    assert d.accepted is False
    assert d.usage_note is True
    assert d.reason == EXECUTE_MISSING_REASON


@pytest.mark.parametrize(
    "note",
    [
        "@berat_ai please implement",
        "@berat_ai /asking is this ok",
        "@berat_ai /ask-review the diff",
    ],
)
def test_gitlab_mention_without_execute_is_usage_note(note):
    d = _gl(note)
    assert d.accepted is False
    assert d.usage_note is True
    assert d.reason == EXECUTE_MISSING_REASON
    assert d.event is not None
    assert d.event.discussion_id == "disc-1"


@pytest.mark.parametrize(
    "note",
    [
        "@yaver please implement",
        "@yaver /asking is this ok",
        '<a href="#" data-vss-mention="version:2.0,g">@Yaver</a> look at this',
    ],
)
def test_azure_mention_without_execute_is_usage_note(note):
    d = _az(note)
    assert d.accepted is False
    assert d.usage_note is True
    assert d.reason == EXECUTE_MISSING_REASON
    assert d.event is not None
    assert d.event.thread_id == "8"


def test_self_mention_is_ignored_no_usage_note():
    d = _gl("@berat_ai /yaver ping", username="berat_ai")
    assert d.accepted is False
    assert d.usage_note is False
    assert d.reason == "ignored comment from bot user"

    d2 = _az(
        "@yaver /yaver ping",
        unique_name="DOMAIN\\yaver",
        bots=["yaver"],
    )
    assert d2.accepted is False
    assert d2.usage_note is False
    assert d2.reason == "ignored comment from bot user"


def test_ask_handoff_still_silent():
    d = _gl("@berat_ai /ask what is auth?")
    assert d.accepted is False
    assert d.usage_note is False
    d2 = _az("@yaver /ask what is auth?")
    assert d2.accepted is False
    assert d2.usage_note is False


def test_extract_azure_thread_id_from_links():
    comment = {
        "id": 77,
        "parentCommentId": 0,
        "_links": {
            "self": {
                "href": (
                    "https://tfs.example.com/tfs/DefaultCollection/"
                    "_apis/git/repositories/r/pullRequests/4/threads/12/comments/77"
                )
            }
        },
    }
    assert extract_azure_thread_id(comment, {}) == "12"
    assert extract_azure_thread_id({"id": 77, "parentCommentId": 0}, {}) == ""
    assert extract_azure_thread_id({"threadId": 9}, {}) == "9"
    assert extract_azure_thread_id({"id": 1}, {"threadId": 4}) == "4"


def test_azure_finds_thread_id_from_comment_list(monkeypatch):
    from src.azure.client import AzureDevOpsClient

    class FakeResp:
        status_code = 200
        content = b'{"value":[{"id":8,"comments":[{"id":77}]}]}'
        text = '{"value":[{"id":8,"comments":[{"id":77}]}]}'

        def json(self):
            return {"value": [{"id": 8, "comments": [{"id": 77}]}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr("src.azure.client.httpx.Client", FakeClient)
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat="azpat-test",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    assert (
        c.find_thread_id_for_comment(
            project="Demo", repository="demo", pr_id=4, comment_id="77"
        )
        == "8"
    )


def test_azure_find_thread_does_not_pick_first_comment_id_one(monkeypatch):
    from src.azure.client import AzureDevOpsClient

    payload = {
        "value": [
            {"id": 8, "comments": [{"id": 1, "content": "old overview"}]},
            {
                "id": 9,
                "comments": [{"id": 1, "content": "@yaver /yaver fix tests"}],
            },
        ]
    }

    class FakeResp:
        status_code = 200
        content = b"{}"
        text = "{}"

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr("src.azure.client.httpx.Client", FakeClient)
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat="azpat-test",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    assert (
        c.find_thread_id_for_comment(
            project="Demo",
            repository="demo",
            pr_id=4,
            comment_id="1",
            comment_content="@yaver /yaver fix tests",
        )
        == "9"
    )
    assert (
        c.find_thread_id_for_comment(
            project="Demo", repository="demo", pr_id=4, comment_id="1"
        )
        == ""
    )


def test_azure_usage_note_looks_up_missing_thread(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    payload = _pr_comment_payload(note="@yaver please implement")
    payload["resource"]["comment"].pop("threadId", None)
    payload["resource"]["comment"].pop("_links", None)
    posted = {}

    def fake_find(self, **kwargs):
        return "8"

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 3}

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock()
    app = create_dashboard_app(processor=proc)
    with patch(
        "src.azure.client.AzureDevOpsClient.find_thread_id_for_comment", fake_find
    ), patch("src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post):
        with TestClient(app) as client:
            resp = client.post("/yaver/webhook/azure", json=payload)
    assert resp.status_code == 200
    assert resp.json()["usage_note"] is True
    assert posted.get("thread_id") == "8"
    assert posted.get("allow_new_thread") is False
    assert str(posted.get("parent_comment_id") or "") == "77"


def test_operator_reply_header_has_version_job_model():
    from src import __version__
    from src.brand import format_reply_header, wrap_operator_reply

    line = format_reply_header("Answer", model="glm", job_id="job_abc")
    assert line == f"**Yaver {__version__} — Answer** · `glm` · `job_abc`"
    body = wrap_operator_reply("Answer", "Fixed login.", model="glm", job_id="job_abc")
    assert body.startswith(line)
    assert "Fixed login." in body


def test_operator_reply_header_keeps_job_id_after_current_cleared():
    """Creasy always prints job.job_id. Finish clears current_job_id — still use history."""
    from src import __version__
    from src.brand import wrap_operator_reply
    from src.state.models import JiraAgentState, TaskStatus

    st = JiraAgentState(
        issue_key="KAN-1",
        issue_summary="s",
        description="d",
        status=TaskStatus.COMPLETED,
        metadata={
            "current_job_id": None,
            "job_ids": ["job_old", "job_live"],
            "model": "mimo",
        },
    )
    body = wrap_operator_reply("Answer", "done", state=st)
    assert body.startswith(
        f"**Yaver {__version__} — Answer** · `mimo` · `job_live`"
    )


def test_operator_reply_header_uses_log_context_job_id():
    from src import __version__
    from src.brand import wrap_operator_reply
    from src.log_context import clear_log_context, set_job_id

    set_job_id("job_ctx")
    try:
        body = wrap_operator_reply("Failed", "boom", model="glm")
        assert body.startswith(
            f"**Yaver {__version__} — Failed** · `glm` · `job_ctx`"
        )
    finally:
        clear_log_context()


def test_usage_note_matches_creasy_shape_without_at_mention():
    body = format_execute_usage_note("berat_ai")
    assert "<!-- yaver-usage -->" in body
    assert "**Yaver — how to run a command**" in body
    assert "I only run `/yaver`" in body
    assert "/yaver <prompt>" in body
    assert "@" not in body
    assert "@mention" not in body


def test_gitlab_http_usage_note_posts_in_thread(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 3}

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock()
    app = create_dashboard_app(processor=proc)
    with patch("src.gitlab.client.GitlabClient.post_mr_note", fake_post):
        with TestClient(app) as client:
            resp = client.post(
                "/yaver/webhook/gitlab",
                json=_mr_payload(note="@berat_ai please implement"),
                headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "tok"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == EXECUTE_MISSING_REASON
    assert body["usage_note"] is True
    assert body["usage_note_posted"] is True
    proc.enqueue_gitlab_note.assert_not_awaited()
    assert posted.get("discussion_id") == "disc-1"
    assert posted.get("allow_new_thread") is False
    assert "**Yaver — how to run a command**" in (posted.get("body") or "")
    assert "/yaver" in (posted.get("body") or "")
    assert "@" not in (posted.get("body") or "")


def test_azure_http_usage_note_posts_in_thread(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 3}

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_azure_comment = AsyncMock()
    app = create_dashboard_app(processor=proc)
    with patch("src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post):
        with TestClient(app) as client:
            resp = client.post(
                "/yaver/webhook/azure",
                json=_pr_comment_payload(note="@yaver please implement"),
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"] == EXECUTE_MISSING_REASON
    assert body["usage_note"] is True
    assert body["usage_note_posted"] is True
    proc.enqueue_azure_comment.assert_not_awaited()
    assert posted.get("thread_id") == "8"
    assert posted.get("allow_new_thread") is False
    assert str(posted.get("parent_comment_id") or "") == "77"
    assert "**Yaver — how to run a command**" in (posted.get("body") or "")
    assert "@" not in (posted.get("body") or "")


def test_gitlab_client_does_not_fallback_to_new_note(monkeypatch):
    from src.gitlab.client import GitlabClient

    calls = []

    class FakeResp:
        status_code = 400
        content = b"{}"
        text = "bad discussion"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append({"url": url, "json": json})
            return FakeResp()

    monkeypatch.setattr("src.gitlab.client.httpx.Client", FakeClient)
    c = GitlabClient(host="gitlab.example.com", pat="glpat-test")
    out = c.post_mr_note(
        project=1, mr_iid=4, body="*Yaver*\n\nhi", discussion_id="d1"
    )
    assert out is None
    assert len(calls) == 1
    assert "/discussions/d1/notes" in calls[0]["url"]
    assert calls[0]["json"] == {"body": "*Yaver*\n\nhi"}


def test_azure_client_does_not_fallback_to_new_thread(monkeypatch):
    from src.azure.client import AzureDevOpsClient

    calls = []

    class FakeResp:
        status_code = 400
        content = b"{}"
        text = "bad thread"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, params=None, json=None):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr("src.azure.client.httpx.Client", FakeClient)
    c = AzureDevOpsClient(
        host="tfs.example.com",
        pat="azpat-test",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    out = c.post_pr_comment(
        project="Demo",
        repository="demo",
        pr_id=4,
        body="*Yaver*\n\nhi",
        thread_id="8",
    )
    assert out is None
    assert calls
    assert all(url.endswith("/threads/8/comments") for url in calls)
    assert not any(url.rstrip("/").endswith("/threads") for url in calls)
