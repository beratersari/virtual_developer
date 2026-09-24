"""Daily-use backend bugs found by review. Assertions are the operator-safe outcome."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.azure.workitems import clear_workitem_comment_claims
from tests.test_azure_workitems import _wi_comment_payload


@pytest.fixture(autouse=True)
def _clear_claims():
    clear_workitem_comment_claims()
    yield
    clear_workitem_comment_claims()


def test_workitem_plan_execute_survives_state_change_on_same_save(monkeypatch):
    """Saving Active plus @bot /planExecute must still start plan execute.

    Azure Boards puts System.State and System.History on one workitem.updated
    hook. The comment is the only implement signal; dropping it leaves the
    plan_ready item sitting with no job.
    """
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    app = create_dashboard_app()
    proc = MagicMock()
    proc.ingest_azure_work_item = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "WIT-DEMO-42",
            "queue_id": "q-plan",
            "status": "queued",
            "reason": "execute",
        }
    )
    app.state.processor = proc
    payload = _wi_comment_payload(
        event="workitem.updated",
        note="@yaver /planExecute",
        state="Active",
    )
    payload["resource"]["fields"]["System.State"] = {
        "oldValue": "New",
        "newValue": "Active",
    }

    resp = TestClient(app).post("/yaver/webhook/azure", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True
    assert body.get("kind") == "work_item_comment"
    proc.ingest_azure_work_item.assert_awaited_once()
    event = proc.ingest_azure_work_item.await_args.args[0]
    assert event.plan_handoff == "execute"


def test_jira_comments_keep_paging_when_a_short_page_is_not_the_end():
    """plan_refactor reads the newest @mention. A short page is not the end.

    Jira documents that a page may be shorter than maxResults while ``total``
    still has more. Stopping there hides the operator's latest comment.
    """
    from src.jira.client import JiraClient

    jc = JiraClient(host="https://jira.example", api_token="tok", email="")
    first = [{"id": str(i), "body": f"note {i}"} for i in range(20)]
    second = [{"id": "newest", "body": "[~yaver] please revise the plan"}]
    seen: list[int] = []

    def _get(url, params=None):
        start = int((params or {}).get("startAt") or 0)
        seen.append(start)
        body = {
            "startAt": start,
            "maxResults": 20,
            "total": 21,
            "comments": first if start == 0 else second,
        }
        resp = MagicMock()
        resp.content = b"{}"
        resp.raise_for_status = lambda: None
        resp.json = lambda: body
        return resp

    jc.client.get = _get
    rows = jc.get_comments("KAN-1")
    assert 20 in seen
    assert any("revise the plan" in (row.get("body") or "") for row in rows)
    assert len(rows) == 21
