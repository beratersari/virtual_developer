"""Proof tests from a full-tree review (2026-09-06).

These encode *correct* operator-facing behaviour on paths that would
block using the app. A failure is a real defect in current source.

No live network. Isolated by tests/conftest.py.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.issue_git_spec import parse_issue_git_spec
from src.jira.triggers import jira_body_to_text
from src.state.models import TaskStatus


REAL_GITLAB = "https://gitlab.com/example/app.git"

_ADF_PARAMS = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "{params}"}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"Repository: {REAL_GITLAB}"}
            ],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Source branch: develop"}],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Target branch: main"}],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "Mode: build"}],
        },
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "{params}"}],
        },
    ],
}


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    p._last_jira_status = {}
    return p


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


# ---------------------------------------------------------------------------
# C1 — Jira GET ADF must flatten before {params} parse (clone refresh)
# ---------------------------------------------------------------------------


def test_refresh_issue_text_from_jira_flattens_cloud_adf(processor, state_manager):
    """Clone re-reads Jira. Cloud ADF must become wiki text, not ``str(dict)``."""
    state_manager.create_state("ADF-1", "build it", "stale plain text")
    processor.jira_client.get_issue = MagicMock(
        return_value={"fields": {"summary": "build it", "description": _ADF_PARAMS}}
    )

    summary, description = processor._refresh_issue_text_from_jira("ADF-1")
    spec, err = parse_issue_git_spec(summary, description)
    assert err is None, err
    assert spec is not None
    assert spec.repository_url == REAL_GITLAB
    assert spec.source_branch == "develop"
    assert spec.target_branch == "main"
    assert spec.mode == "build"
    persisted = state_manager.get_state("ADF-1")
    assert persisted is not None
    assert persisted.description == description


def test_issue_text_helper_flattens_adf_like_handle_issue_created():
    """Intake uses ``_issue_text`` so Mode / {params} survive Cloud ADF."""
    from src.processor import _issue_text

    description = _issue_text(_ADF_PARAMS)
    spec, err = parse_issue_git_spec("implement login", description)
    assert err is None, err
    assert spec is not None
    assert spec.mode == "build"
    assert spec.repository_url == REAL_GITLAB


def test_str_of_adf_cannot_parse_params():
    """Guard: if anyone wires str(dict) again, parse must still fail."""
    spec, err = parse_issue_git_spec("s", str(_ADF_PARAMS))
    assert spec is None
    assert err is not None


# ---------------------------------------------------------------------------
# C2 — Webhook Cloud ADF is already flattened (must stay true)
# ---------------------------------------------------------------------------


def test_webhook_assignment_flattens_adf_before_enqueue():
    from src.jira.webhook import decide_jira_webhook

    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "ADF-WH",
            "fields": {
                "summary": "implement",
                "description": _ADF_PARAMS,
                "assignee": {"displayName": "Jira AI Bot", "name": "devbot"},
            },
        },
        "changelog": {
            "id": "99",
            "items": [
                {
                    "field": "assignee",
                    "to": "devbot",
                    "toString": "Jira AI Bot",
                }
            ],
        },
    }
    decision = decide_jira_webhook(
        payload,
        headers={"x-webhook-token": "secret"},
        query={"token": "secret"},
        enabled=True,
        secret="secret",
        intake_mode="webhook",
        assignee_needles=["devbot", "jira ai bot"],
        mention_tokens=["@DevBot"],
    )
    assert decision.accepted, decision.reason
    desc = (decision.event or {}).get("issue", {}).get("fields", {}).get(
        "description"
    )
    assert isinstance(desc, str)
    spec, err = parse_issue_git_spec("implement", desc)
    assert err is None, err
    assert spec is not None
    assert spec.repository_url == REAL_GITLAB


# ---------------------------------------------------------------------------
# C3 — Dashboard cancel must not wait on the workflow issue lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_cancel_succeeds_while_issue_lock_held(
    processor, state_manager
):
    state_manager.create_state("LOCK-1", "s", "d")
    state_manager.update_state("LOCK-1", status=TaskStatus.EXECUTING)
    lock = processor._get_issue_lock("LOCK-1")
    await lock.acquire()
    try:
        out = await processor.cancel_job("LOCK-1")
        assert out.get("ok") is True
        assert state_manager.get_state("LOCK-1").status == TaskStatus.CANCELLED
    finally:
        lock.release()


def test_dashboard_http_cancel_and_jobs_contract(state_manager, fake_jira):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    state_manager.create_state("HTTP-1", "s", "d")
    state_manager.update_state("HTTP-1", status=TaskStatus.EXECUTING)

    app = create_dashboard_app(processor=proc, state_manager=state_manager)
    client = TestClient(app)
    listed = client.get("/api/jobs")
    assert listed.status_code == 200
    assert "jobs" in listed.json()

    cancelled = client.post("/api/tasks/HTTP-1/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json().get("ok") is True
    assert state_manager.get_state("HTTP-1").status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# C4 — Poller must not restart terminal work every cycle
# ---------------------------------------------------------------------------


def test_poller_completed_same_todo_column_does_not_reprocess_every_poll(
    poller, state_manager
):
    state_manager.create_state("STAY-1", "done", "")
    state_manager.update_state("STAY-1", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"STAY-1": "to do"}
    poller._last_jira_status = {"STAY-1": "to do"}
    issues = [
        {
            "key": "STAY-1",
            "fields": {
                "status": {
                    "name": "To Do",
                    "statusCategory": {"key": "new"},
                },
                "labels": ["bot"],
            },
        }
    ]
    assert poller.check_status_changes(issues) == []


def test_poller_light_board_payload_does_not_look_like_error_edit(
    poller, state_manager
):
    import hashlib

    summary = "fix auth"
    full_desc = (
        "{params}\n"
        f"Repository: {REAL_GITLAB}\n"
        "Source branch: feature/x\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}"
    )
    last_fp = hashlib.sha256(
        f"{summary}\n{full_desc}".encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    light_fp = hashlib.sha256(
        f"{summary}\n".encode("utf-8", errors="replace")
    ).hexdigest()[:20]
    state_manager.create_state("E-LIGHT2", summary, full_desc)
    state_manager.update_state(
        "E-LIGHT2",
        status=TaskStatus.ERROR,
        metadata={
            "requeue_eligible": True,
            "last_intake_fingerprint": last_fp,
            "last_intake_fingerprint_light": light_fp,
        },
    )
    poller._status_before_poll = {"E-LIGHT2": "to do"}
    light = {
        "key": "E-LIGHT2",
        "fields": {
            "summary": summary,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    assert poller.check_status_changes([light]) == []


# ---------------------------------------------------------------------------
# C5 — Jira create + on-prem assign stay on the documented API shape
# ---------------------------------------------------------------------------


def test_create_issue_posts_fields_wrapper():
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            s.jira_email = ""
            c = JiraClient()
            c.client = http
            c.resolve_issuetype_ref = MagicMock(return_value={"name": "Task"})
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"key": "P-1"}
            resp.text = ""
            resp.raise_for_status = MagicMock()
            http.post.return_value = resp
            c.create_issue("PROJ", "sum", "desc")
            payload = http.post.call_args.kwargs.get("json") or {}
            assert "fields" in payload
            assert payload["fields"]["project"]["key"] == "PROJ"


def test_on_prem_assign_uses_name_not_account_id():
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.onprem.local"
            s.jira_api_token = "t"
            s.jira_email = ""
            c = JiraClient()
            c.client = http
            c.is_cloud = False
            resp = MagicMock()
            resp.status_code = 204
            resp.raise_for_status = MagicMock()
            http.put.return_value = resp
            c.assign_issue("P-1", "jdoe")
            payload = http.put.call_args.kwargs.get("json") or {}
            fields = payload.get("fields", payload)
            assignee = fields.get("assignee", {})
            assert "name" in assignee
            assert "accountId" not in assignee


# ---------------------------------------------------------------------------
# C6 — Begin must not revive CANCELLED (dashboard cancel race)
# ---------------------------------------------------------------------------


def test_begin_workflow_refuses_cancelled(processor, state_manager):
    from src.orchestrator.agent_runner import AgentTask

    state_manager.create_state("RACE-C", "s", "d")
    state_manager.update_state(
        "RACE-C",
        status=TaskStatus.CANCELLED,
        error_message="Cancelled from dashboard",
        completed_at=datetime.now(),
    )
    task = AgentTask(
        description="d", prompt="p", agent="a", issue_key="RACE-C"
    )
    job_id = processor._begin_workflow_run(
        state_manager.get_state("RACE-C"),
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="a",
        job_status="executing",
    )
    assert job_id is None
    assert state_manager.get_state("RACE-C").status == TaskStatus.CANCELLED
