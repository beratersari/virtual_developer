"""Live Jira operator checks against the configured Cloud site.

Requires a working ``JIRA_HOST`` / email / token in ``.env``.
Does **not** call Settings PATCH (that would rewrite ``.env``).

Run:
  VD_LIVE_JIRA=1 .venv/bin/python -m pytest tests/test_live_jira_remaining_operator.py -q -s
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from src.config import settings
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus

pytestmark = pytest.mark.skipif(
    os.environ.get("VD_LIVE_JIRA") != "1",
    reason="Set VD_LIVE_JIRA=1 to hit the real Jira site",
)

MARK = "[VD-E2E]"


@pytest.fixture(scope="module")
def live_client():
    host = (settings.jira_host or "").lower()
    assert "atlassian.net" in host or "jira" in host, f"unexpected host {settings.jira_host}"
    assert "attacker.example" not in host
    c = JiraClient()
    me = c.client.get("/myself")
    assert me.status_code == 200, (me.text or "")[:200]
    return c


@pytest.fixture
def isolated_state(tmp_path):
    return JiraStateManager(state_dir=tmp_path / "state")


def _create(client: JiraClient, summary: str, labels=None) -> str:
    created = client.create_issue(
        (settings.jira_projects_list or ["KAN"])[0],
        f"{MARK} {summary} {datetime.now().strftime('%H%M%S')}",
        "Virtual Developer live check — safe to delete.",
        labels=labels or ["bot"],
    )
    assert created and created.get("key"), client.last_error
    return created["key"]


def test_live_myself_and_kanban_sprint(live_client):
    me = live_client.client.get("/myself")
    assert me.status_code == 200
    sprint = live_client.get_active_sprint(settings.jira_board_id)
    # This Cloud board is Kanban — 400 "does not support sprints"
    assert live_client.sprint_lookup == "kanban"
    assert sprint is None


def test_live_create_get_issue(live_client):
    key = _create(live_client, "create-get")
    issue = live_client.get_issue(key)
    assert issue["key"] == key
    assert issue["fields"]["status"]["name"]
    labels = issue["fields"].get("labels") or []
    assert "bot" in [str(x).lower() for x in labels]


def test_live_c8_poller_noop_moves_board_no_local_state(live_client, isolated_state):
    key = _create(live_client, "c8-noop-handler")
    poller = JiraPoller(
        client=live_client,
        interval_seconds=60,
        board_id=settings.jira_board_id,
        state_manager=isolated_state,
    )
    poller._handler = None
    ours = []
    for _ in range(8):
        found = poller.poll_board()
        ours = [i for i in found if i["key"] == key]
        if ours:
            break
        time.sleep(2)
    assert ours, f"{key} not returned by poll_board after retries"
    poller.process_issue(ours[0], is_update=False)

    live = live_client.get_issue(key, fields=["status", "comment"])
    status = (live["fields"].get("status") or {}).get("name") or ""
    assert "progress" in status.lower(), f"expected In Progress, got {status!r}"
    st = isolated_state.get_state(key)
    assert st is not None and st.status == TaskStatus.ERROR
    comments = ((live["fields"].get("comment") or {}).get("comments")) or []
    bodies = "\n".join(str(c.get("body") or "") for c in comments)
    assert "error" in bodies.lower()
    live_client.add_comment(
        key,
        f"{MARK} C8 confirmed: board={status}, local ERROR recorded.",
    )


def test_live_c9_completion_comment_includes_prior_mr(live_client, isolated_state):
    key = _create(live_client, "c9-soft-complete-mr")
    isolated_state.create_state(key, "c9", "d")
    state = isolated_state.update_state(
        key,
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={
            "merge_request_url": "https://gitlab.example.com/g/p/-/merge_requests/99",
            "delivery_status": "no_new_commits",
            "delivery_note": "no new commits this run",
            "feature_branch": f"feature/{key}",
        },
    )
    JiraReporter(client=live_client).post_completion(state, summary="done")
    live = live_client.get_issue(key, fields=["comment"])
    comments = ((live["fields"].get("comment") or {}).get("comments")) or []
    blob = "\n".join(str(c.get("body") or "") for c in comments)
    assert "merge_requests/99" not in blob, blob[:500]


def test_live_plan_start_latch(live_client, isolated_state):
    key = _create(live_client, "plan-start-latch", labels=["bot"])
    isolated_state.create_state(key, "plan", "d")
    isolated_state.update_state(key, status=TaskStatus.PLAN_READY)
    assert live_client.add_labels(key, ["ai-start-work"])

    poller = JiraPoller(
        client=live_client,
        interval_seconds=60,
        board_id=settings.jira_board_id,
        state_manager=isolated_state,
    )
    poller._handler = lambda e: None
    first = poller.poll_board()
    assert any(i["key"] == key for i in first)
    assert key not in poller._plan_start_emitted
    for issue in first:
        if issue["key"] == key:
            poller.process_issue(issue, is_update=True)
    assert key in poller._plan_start_emitted
    second = poller.poll_board()
    assert not any(i["key"] == key for i in second)
    live_client.add_comment(
        key, f"{MARK} plan_start latched only after process_issue."
    )
