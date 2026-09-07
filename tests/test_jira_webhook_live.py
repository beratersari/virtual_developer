"""Live Jira REST checks for webhook intake (opt-in).

Creates a throwaway issue, assigns / comments via API v2 (Server 9.4 and
Cloud), then feeds the real response shapes into ``decide_jira_webhook``.
Does **not** start an agent job on the live daemon.

Run:
  VD_LIVE_JIRA=1 .venv/bin/python -m pytest tests/test_jira_webhook_live.py -q -s
"""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from src.config import settings
from src.jira.client import JiraClient
from src.jira.webhook import decide_jira_webhook
from src.jira.triggers import jira_body_to_text


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


pytestmark = pytest.mark.skipif(
    os.environ.get("VD_LIVE_JIRA") != "1",
    reason="Set VD_LIVE_JIRA=1 to hit the real Jira site",
)

MARK = "[VD-WH]"
SECRET = "live-test-not-used-for-http"


@pytest.fixture(scope="module")
def live_client():
    host = (settings.jira_host or "").lower()
    assert settings.is_configured(), "JIRA_HOST + JIRA_API_TOKEN required"
    assert "attacker.example" not in host
    c = JiraClient()
    me = c.get_myself()
    assert me, "GET /myself failed — check token / host"
    return c


def _needles():
    return list(settings.trigger_assignee_names_list)


def _mentions():
    return list(settings.trigger_mentions_list)


def _params() -> str:
    return (
        f"{MARK} webhook live check — safe to delete.\n"
        "{params}\n"
        "Repository: https://gitlab.example.com/g/demo.git\n"
        "Source branch: feature/KAN-7\n"
        "Target branch: develop\n"
        "{params}\n"
    )


def _create(client: JiraClient) -> str:
    created = client.create_issue(
        (settings.jira_projects_list or ["KAN"])[0],
        f"{MARK} webhook {datetime.now().strftime('%H%M%S')}",
        _params(),
        labels=["vd-webhook-e2e"],
    )
    assert created and created.get("key"), client.last_error
    return created["key"]


def test_live_myself_and_webhooks(live_client: JiraClient):
    me = live_client.get_myself()
    assert me.get("displayName") or me.get("name") or me.get("accountId")
    # Permission may be missing on Cloud free — must not raise
    hooks = live_client.list_webhooks()
    assert isinstance(hooks, list)


def test_live_comment_mention_payload(live_client: JiraClient):
    key = _create(live_client)
    mention = (_mentions() or ["@DevBot"])[0]
    body = f"{MARK} please look {mention}"
    posted = live_client.add_comment(key, body)
    assert posted, "add_comment failed"
    issue = live_client.get_issue(key)
    assert issue and issue.get("key") == key
    comments = live_client.get_comments(key)
    assert comments
    last = comments[-1]
    text = jira_body_to_text(last.get("body"))
    assert MARK in text
    payload = {
        "webhookEvent": "comment_created",
        "comment": last,
        "issue": issue,
        "timestamp": 1,
    }
    d = decide_jira_webhook(
        payload,
        query={"token": SECRET},
        secret=SECRET,
        intake_mode="webhook",
        assignee_needles=_needles(),
        mention_tokens=_mentions(),
    )
    assert d.accepted, d.reason
    assert d.trigger == "mention"


def test_live_assign_changelog_shape(live_client: JiraClient):
    """Assign via REST then synthesize the Server/Cloud changelog Jira would send."""
    key = _create(live_client)
    me = live_client.get_myself() or {}
    ident = (
        str(me.get("accountId") or me.get("name") or me.get("key") or "")
    ).strip()
    display = str(me.get("displayName") or ident)
    if not ident:
        pytest.skip("myself has no name/accountId")
    ok = live_client.assign_issue(key, ident)
    # Cloud may reject self-assign depending on permissions; still check GET
    issue = live_client.get_issue(key, fields=["assignee", "summary", "description"])
    assert issue
    assignee = (issue.get("fields") or {}).get("assignee") or {}
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_assigned",
        "issue": issue,
        "changelog": {
            "id": "live-1",
            "items": [
                {
                    "field": "assignee",
                    "fieldtype": "jira",
                    "from": None,
                    "fromString": None,
                    "to": ident,
                    "toString": display,
                }
            ],
        },
    }
    d = decide_jira_webhook(
        payload,
        query={"token": SECRET},
        secret=SECRET,
        intake_mode="webhook",
        assignee_needles=_needles() + [display.lower(), ident.lower()],
        mention_tokens=_mentions(),
    )
    # If assign failed and ticket is still unassigned, changelog `to` is still us
    assert d.accepted, (d.reason, ok, assignee)
    assert d.trigger == "assignment"
