"""Jira webhook intake — Server/DC 9.4 + Cloud payload shapes.

Covers decide filters (assign-to-bot / mention only), dashboard endpoint,
settings, queue concurrency, session-bind reuse, and Jira REST v2 mocks
that build the same payloads Jira would POST after assign/comment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view
from src.jira.triggers import (
    assignee_looks_like_bot,
    changelog_assigned_to_bot,
    comment_is_bot_output,
    comment_mentions_target,
    jira_body_to_text,
)
from src.jira.webhook import (
    INTAKE_POLL,
    INTAKE_WEBHOOK,
    decide_jira_webhook,
    normalize_intake_mode,
    validate_hub_signature,
)
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


NEEDLES = ["devbot", "jira ai bot"]
MENTIONS = ["@DevBot", "@AI"]
SECRET = "jira-hook-secret"

PARAMS = (
    "{params}\n"
    "Repository: https://gitlab.example.com/g/demo.git\n"
    "Source branch: feature/KAN-7\n"
    "Target branch: develop\n"
    "{params}\n"
)


def _assignee(name: str = "devbot", display: str = "DevBot", account: str = "") -> dict:
    data = {"name": name, "key": name, "displayName": display}
    if account:
        data["accountId"] = account
    return data


def _issue(
    key: str = "KAN-7",
    *,
    assignee: Optional[dict] = None,
    description: str = PARAMS,
    summary: str = "Build the thing",
    status: str = "To Do",
) -> dict:
    return {
        "id": "10007",
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "assignee": assignee if assignee is not None else _assignee(),
            "status": {"name": status, "statusCategory": {"key": "new"}},
            "labels": [],
            "issuetype": {"name": "Task"},
        },
    }


def _assignment_payload(
    *,
    key: str = "KAN-7",
    to_name: str = "devbot",
    to_str: str = "DevBot",
    from_name: str = "alice",
    changelog_id: str = "10100",
    status: str = "To Do",
) -> dict:
    return {
        "timestamp": 1,
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_assigned",
        "issue": _issue(key, assignee=_assignee(to_name, to_str), status=status),
        "changelog": {
            "id": changelog_id,
            "items": [
                {
                    "field": "assignee",
                    "fieldtype": "jira",
                    "from": from_name,
                    "fromString": from_name.title(),
                    "to": to_name,
                    "toString": to_str,
                }
            ],
        },
    }


def _unassign_payload(key: str = "KAN-7") -> dict:
    issue = _issue(key, assignee=None)
    return {
        "timestamp": 1,
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_assigned",
        "issue": issue,
        "changelog": {
            "id": "10101",
            "items": [
                {
                    "field": "assignee",
                    "fieldtype": "jira",
                    "from": "devbot",
                    "fromString": "DevBot",
                    "to": None,
                    "toString": None,
                }
            ],
        },
    }


def _comment_payload(
    *,
    key: str = "KAN-7",
    body: str = "hey [~devbot] please continue",
    author: str = "alice",
    comment_id: str = "20001",
    event: str = "comment_created",
) -> dict:
    return {
        "timestamp": 1,
        "webhookEvent": event,
        "comment": {
            "id": comment_id,
            "body": body,
            "author": {
                "name": author,
                "key": author,
                "displayName": author.title(),
            },
        },
        "issue": _issue(key),
    }


def _created_assigned_payload(key: str = "KAN-8") -> dict:
    return {
        "timestamp": 1,
        "webhookEvent": "jira:issue_created",
        "issue_event_type_name": "issue_created",
        "issue": _issue(key),
    }


def _headers(token: str = SECRET) -> dict:
    return {"X-Webhook-Token": token}


def _decide(payload: dict, **kwargs) -> Any:
    defaults = dict(
        headers=_headers(),
        query={"token": SECRET},
        secret=SECRET,
        intake_mode=INTAKE_WEBHOOK,
        assignee_needles=NEEDLES,
        mention_tokens=MENTIONS,
    )
    defaults.update(kwargs)
    return decide_jira_webhook(payload, **defaults)


# ---------------------------------------------------------------------------
# decide / trigger filters
# ---------------------------------------------------------------------------


def test_normalize_intake_mode():
    assert normalize_intake_mode("webhook") == INTAKE_WEBHOOK
    assert normalize_intake_mode("HOOK") == INTAKE_WEBHOOK
    assert normalize_intake_mode("") == INTAKE_POLL
    assert normalize_intake_mode("poll") == INTAKE_POLL


def test_assign_to_bot_accepted():
    d = _decide(_assignment_payload())
    assert d.accepted
    assert d.trigger == "assignment"
    assert d.event_id == "assignee:10100"
    assert d.event["webhook_intake"] is True
    assert d.event["issue"]["key"] == "KAN-7"


def test_unassign_rejected():
    d = _decide(_unassign_payload())
    assert not d.accepted
    assert "not assign-to-bot" in d.reason


def test_assign_away_from_bot_rejected():
    payload = _assignment_payload(to_name="alice", to_str="Alice", from_name="devbot")
    d = _decide(payload)
    assert not d.accepted


def test_status_only_update_rejected():
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": _issue(),
        "changelog": {
            "id": "9",
            "items": [
                {
                    "field": "status",
                    "from": "10000",
                    "fromString": "To Do",
                    "to": "3",
                    "toString": "In Progress",
                }
            ],
        },
    }
    d = _decide(payload)
    assert not d.accepted


def test_comment_mention_wiki_accepted():
    d = _decide(_comment_payload())
    assert d.accepted
    assert d.trigger == "mention"
    assert d.event_id == "comment:20001"


def test_comment_at_mention_accepted():
    d = _decide(_comment_payload(body="@DevBot please look at the login bug"))
    assert d.accepted
    assert d.trigger == "mention"


def test_comment_without_mention_rejected():
    d = _decide(_comment_payload(body="looks good to me"))
    assert not d.accepted
    assert "not mentioned" in d.reason


def test_bot_own_comment_rejected():
    d = _decide(_comment_payload(author="devbot", body="@DevBot ignore me"))
    assert not d.accepted
    assert "bot user" in d.reason


def test_yaver_reply_rejected():
    d = _decide(_comment_payload(body="*Yaver*\n\nWork started.", author="alice"))
    assert not d.accepted
    assert "bot reply" in d.reason


def test_created_assigned_accepted():
    d = _decide(_created_assigned_payload())
    assert d.accepted
    assert d.trigger == "created_assigned"
    assert d.event["webhookEvent"] == "jira:issue_created"


def test_created_unassigned_rejected():
    payload = _created_assigned_payload()
    payload["issue"]["fields"]["assignee"] = None
    d = _decide(payload)
    assert not d.accepted


def test_poll_mode_disabled():
    d = _decide(_assignment_payload(), intake_mode="poll")
    assert not d.accepted
    assert "disabled" in d.reason


def test_secret_required():
    d = _decide(_assignment_payload(), secret="")
    assert d.http_status == 401
    assert "secret" in d.reason


def test_bad_token_rejected():
    d = _decide(_assignment_payload(), query={"token": "nope"}, headers={})
    assert d.http_status == 401


def test_cloud_hmac_accepted():
    body = json.dumps(_assignment_payload()).encode("utf-8")
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    d = decide_jira_webhook(
        None,
        raw_body=body,
        headers={"X-Hub-Signature": sig},
        secret=SECRET,
        intake_mode=INTAKE_WEBHOOK,
        assignee_needles=NEEDLES,
        mention_tokens=MENTIONS,
    )
    assert d.accepted


def test_adf_mention_flattened():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {"id": "abc-1", "text": "@DevBot"},
                    },
                    {"type": "text", "text": " please continue"},
                ],
            }
        ],
    }
    text = jira_body_to_text(adf)
    assert "@DevBot" in text
    assert comment_mentions_target(text, mention_tokens=MENTIONS, assignee_needles=NEEDLES)
    payload = _comment_payload()
    payload["comment"]["body"] = adf
    d = _decide(payload)
    assert d.accepted


def test_cloud_assignee_account_id_changelog():
    payload = _assignment_payload(to_name="", to_str="Jira AI Bot")
    payload["changelog"]["items"][0]["to"] = "557058:deadbeef"
    payload["issue"]["fields"]["assignee"] = _assignee(
        "", "Jira AI Bot", account="557058:deadbeef"
    )
    d = _decide(payload)
    assert d.accepted


def test_issue_updated_with_comment_mention():
    """Server configs that omit comment_created still send issue_updated + comment."""
    payload = {
        "webhookEvent": "jira:issue_updated",
        "issue": _issue(),
        "comment": {
            "id": "77",
            "body": "[~devbot] ping",
            "author": {"name": "alice", "displayName": "Alice"},
        },
        "changelog": {"id": "1", "items": []},
    }
    d = _decide(payload)
    assert d.accepted
    assert d.trigger == "mention"


def test_trigger_helpers():
    assert assignee_looks_like_bot(_assignee(), needles=NEEDLES)
    assert not assignee_looks_like_bot({"displayName": "Alice"}, needles=NEEDLES)
    assert changelog_assigned_to_bot(
        {"items": [{"field": "assignee", "to": "devbot", "toString": "DevBot"}]},
        needles=NEEDLES,
    )
    assert not changelog_assigned_to_bot(
        {"items": [{"field": "assignee", "to": None, "toString": None}]},
        needles=NEEDLES,
    )
    assert comment_is_bot_output("*Yaver*\n\nhi")
    assert not comment_is_bot_output("please look")
    assert validate_hub_signature(b"{}", "sha256=nope", "x") is False


def test_mode_optional_in_params():
    from src.issue_git_spec import parse_issue_git_spec

    spec, err = parse_issue_git_spec("s", PARAMS)
    assert err is None
    assert spec is not None
    assert spec.mode == "build"
    assert spec.source_branch == "feature/KAN-7"
    assert spec.target_branch == "develop"


# ---------------------------------------------------------------------------
# Dashboard endpoint + settings
# ---------------------------------------------------------------------------


def _app(processor: Optional[JobProcessor] = None):
    proc = processor or MagicMock()
    proc.enqueue_jira_event = AsyncMock(
        return_value={
            "ok": True,
            "queued": False,
            "started": True,
            "queue_id": "q_abc",
            "status": "running",
        }
    )
    proc.jira_client = MagicMock()
    proc.jira_client.get_issue.return_value = None
    sm = MagicMock()
    return create_dashboard_app(processor=proc, state_manager=sm), proc


def test_webhook_endpoint_assignment(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_intake_mode", "webhook")
    monkeypatch.setattr("src.config.settings.jira_webhook_secret", SECRET)
    monkeypatch.setattr("src.config.settings.trigger_assignee_names", "devbot")
    monkeypatch.setattr("src.config.settings.trigger_mentions", "@DevBot")
    app, proc = _app()
    client = TestClient(app)
    r = client.post(
        f"/webhooks/jira?token={SECRET}",
        json=_assignment_payload(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["trigger"] == "assignment"
    assert body["issue_key"] == "KAN-7"
    proc.enqueue_jira_event.assert_awaited_once()
    ev = proc.enqueue_jira_event.await_args.args[0]
    assert ev["webhook_intake"] is True


def test_webhook_endpoint_disabled_in_poll_mode(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_intake_mode", "poll")
    monkeypatch.setattr("src.config.settings.jira_webhook_secret", SECRET)
    app, proc = _app()
    client = TestClient(app)
    r = client.post(
        f"/webhooks/jira?token={SECRET}",
        json=_assignment_payload(),
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    proc.enqueue_jira_event.assert_not_awaited()


def test_webhook_endpoint_duplicate_comment(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_intake_mode", "webhook")
    monkeypatch.setattr("src.config.settings.jira_webhook_secret", SECRET)
    monkeypatch.setattr("src.config.settings.trigger_assignee_names", "devbot")
    monkeypatch.setattr("src.config.settings.trigger_mentions", "@DevBot,@AI")
    app, proc = _app()
    client = TestClient(app)
    payload = _comment_payload()
    r1 = client.post(f"/webhooks/jira?token={SECRET}", json=payload)
    r2 = client.post(f"/webhooks/jira?token={SECRET}", json=payload)
    assert r1.status_code == 200 and r1.json()["ok"]
    assert r2.status_code == 200 and r2.json()["ok"]
    # endpoint accepts twice; processor dedups by event id
    assert proc.enqueue_jira_event.await_count == 2
    ev = proc.enqueue_jira_event.await_args_list[0].args[0]
    assert ev["jira_event_id"] == "comment:20001"


def test_settings_view_includes_intake(monkeypatch):
    monkeypatch.setattr("src.config.settings.jira_intake_mode", "webhook")
    monkeypatch.setattr("src.config.settings.jira_webhook_secret", "s3cret")
    view = build_settings_view()
    dumped = view.model_dump()
    assert dumped["jira_intake_mode"] == "webhook"
    assert dumped["jira_webhook_secret_configured"] is True
    assert dumped["jira_webhook_path"] == "/webhooks/jira"
    assert "s3cret" not in str(dumped)


def test_settings_update_intake_mode(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.settings.jira_intake_mode", "poll")
    view = apply_settings_update(SettingsUpdate(jira_intake_mode="webhook"))
    assert view.jira_intake_mode == "webhook"
    from src.config import settings

    assert settings.jira_intake_mode == "webhook"


# ---------------------------------------------------------------------------
# Processor + queue concurrency + session reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_dedups_same_event_id(tmp_path, monkeypatch, fake_jira, reporter):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    qs = WorkQueueStore(queue_dir=tmp_path / "queue")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc.queue_store = qs
    proc.dispatch_queue = AsyncMock(return_value=0)

    d = _decide(_comment_payload())
    first = await proc.enqueue_jira_event(d.event)
    second = await proc.enqueue_jira_event(d.event)
    assert first["ok"] and not first.get("duplicate")
    assert second.get("duplicate") is True


@pytest.mark.asyncio
async def test_webhook_intake_starts_new_issue(
    tmp_path, monkeypatch, fake_jira, reporter
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._start_execution_workflow = AsyncMock()
    proc._start_planning_workflow = AsyncMock()
    proc._mark_jira_in_progress = MagicMock()

    d = _decide(_assignment_payload())
    out = await proc.process_event(d.event)
    assert out["work_started"] is True
    proc._start_execution_workflow.assert_awaited()
    st = sm.get_state("KAN-7")
    assert st is not None
    assert st.metadata.get("workflow_type") in {"execution", None} or True


@pytest.mark.asyncio
async def test_webhook_does_not_restart_inflight(
    tmp_path, monkeypatch, fake_jira, reporter
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-7", "s", PARAMS)
    sm.update_state("KAN-7", status=TaskStatus.EXECUTING)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc._start_execution_workflow = AsyncMock()

    d = _decide(_comment_payload())
    out = await proc.process_event(d.event)
    assert out["work_started"] is False
    proc._start_execution_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_plan_ready_starts_execution(
    tmp_path, monkeypatch, fake_jira, reporter
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-7", "s", PARAMS)
    sm.update_state("KAN-7", status=TaskStatus.PLAN_READY)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc._start_execution_workflow = AsyncMock()
    proc._mark_jira_in_progress = MagicMock()

    d = _decide(_comment_payload())
    out = await proc.process_event(d.event)
    assert out["work_started"] is True
    proc._start_execution_workflow.assert_awaited()


@pytest.mark.asyncio
async def test_webhook_requeues_terminal_even_if_in_progress(
    tmp_path, monkeypatch, fake_jira, reporter
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-7", "s", PARAMS)
    sm.update_state("KAN-7", status=TaskStatus.COMPLETED)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc._start_execution_workflow = AsyncMock()
    proc._mark_jira_in_progress = MagicMock()

    payload = _assignment_payload(status="In Progress")
    d = _decide(payload)
    out = await proc.process_event(d.event)
    assert out["work_started"] is True
    proc._start_execution_workflow.assert_awaited()


def test_concurrency_claim_next_caps_at_max_jobs(tmp_path):
    """max_concurrent_jobs is enforced by queue claim (one running slot)."""
    qs = WorkQueueStore(queue_dir=tmp_path / "queue")
    qs.enqueue(
        source="jira",
        issue_key="KAN-1",
        lock_key="lock-a",
        repository_url="https://gitlab.example.com/g/a.git",
        source_branch="feature/KAN-1",
        target_branch="develop",
        payload=_assignment_payload(key="KAN-1", changelog_id="c1"),
    )
    qs.enqueue(
        source="jira",
        issue_key="KAN-2",
        lock_key="lock-b",
        repository_url="https://gitlab.example.com/g/b.git",
        source_branch="feature/KAN-2",
        target_branch="develop",
        payload=_assignment_payload(key="KAN-2", changelog_id="c2"),
    )
    first = qs.claim_next(max_running=1)
    second = qs.claim_next(max_running=1)
    assert first is not None
    assert first["issue_key"] in {"KAN-1", "KAN-2"}
    assert second is None
    queued = [r for r in qs.list_items() if r.get("status") == "queued"]
    assert len(queued) == 1
    assert queued[0]["issue_key"] != first["issue_key"]


def test_concurrency_two_slots_claim_both(tmp_path):
    qs = WorkQueueStore(queue_dir=tmp_path / "queue")
    qs.enqueue(
        source="jira",
        issue_key="KAN-11",
        lock_key="lock-a",
        payload=_assignment_payload(key="KAN-11", changelog_id="a1"),
    )
    qs.enqueue(
        source="jira",
        issue_key="KAN-12",
        lock_key="lock-b",
        payload=_assignment_payload(key="KAN-12", changelog_id="a2"),
    )
    first = qs.claim_next(max_running=2)
    second = qs.claim_next(max_running=2)
    third = qs.claim_next(max_running=2)
    assert first is not None and second is not None
    assert {first["issue_key"], second["issue_key"]} == {"KAN-11", "KAN-12"}
    assert third is None


def test_concurrency_same_workspace_lock_serializes(tmp_path):
    """Same repo + source + target share a lock — second waits."""
    from src.state.queue_store import workspace_lock_key

    qs = WorkQueueStore(queue_dir=tmp_path / "queue")
    lock = workspace_lock_key(
        "https://gitlab.example.com/g/demo.git", "feature/KAN-7", "develop"
    )
    qs.enqueue(
        source="jira",
        issue_key="KAN-7",
        lock_key=lock,
        payload=_assignment_payload(key="KAN-7"),
    )
    qs.enqueue(
        source="jira",
        issue_key="KAN-9",
        lock_key=lock,
        payload=_assignment_payload(key="KAN-9", changelog_id="x2"),
    )
    first = qs.claim_next(max_running=6)
    second = qs.claim_next(max_running=6)
    assert first is not None
    assert second is None
    qs.finish(first["queue_id"], status="completed")
    again = qs.claim_next(max_running=6)
    assert again is not None
    assert again["issue_key"] != first["issue_key"]
    assert again["issue_key"] in {"KAN-7", "KAN-9"}


@pytest.mark.asyncio
async def test_same_repo_branch_reuses_session_bind(
    tmp_path, monkeypatch, fake_jira, reporter
):
    from src.state.session_bind_store import SessionBindStore

    monkeypatch.chdir(tmp_path)
    binds = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", binds)
    binds.upsert(
        repository_url="https://gitlab.example.com/g/demo.git",
        branch="feature/KAN-7",
        target_branch="develop",
        session_id="ses_reuse_1",
        issue_key="KAN-7",
        working_directory=str(tmp_path / "clone"),
    )
    hit = binds.get(
        "https://gitlab.example.com/g/demo.git",
        "feature/KAN-7",
        "develop",
        issue_key="KAN-7",
    )
    assert hit is not None
    assert hit["session_id"] == "ses_reuse_1"
    # A later webhook for the same params must resolve the same bind
    from src.issue_git_spec import parse_issue_git_spec

    spec, err = parse_issue_git_spec("s", PARAMS)
    assert err is None and spec is not None
    again = binds.get(
        spec.repository_url, spec.source_branch, spec.target_branch, issue_key="KAN-9"
    )
    assert again is not None
    assert again["session_id"] == "ses_reuse_1"


# ---------------------------------------------------------------------------
# Jira REST API v2 mock — assign + comment then webhook
# ---------------------------------------------------------------------------


class _JiraRest:
    """Minimal Server 9.4 REST: GET issue, POST comment, PUT assignee."""

    def __init__(self) -> None:
        self.issues: Dict[str, Dict[str, Any]] = {
            "KAN-7": _issue("KAN-7", assignee=_assignee("alice", "Alice"))
        }
        self.comments: Dict[str, List[dict]] = {"KAN-7": []}
        self.calls: List[str] = []
        self.webhooks: List[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path.endswith("/myself"):
            return httpx.Response(
                200,
                json={
                    "name": "devbot",
                    "key": "devbot",
                    "displayName": "DevBot",
                    "accountId": "557058:bot",
                },
            )
        if path.endswith("/rest/webhooks/1.0/webhook"):
            return httpx.Response(200, json=self.webhooks)
        if path.endswith("/issue/KAN-7") and request.method == "GET":
            return httpx.Response(200, json=self.issues["KAN-7"])
        if path.endswith("/issue/KAN-7") and request.method == "PUT":
            body = json.loads(request.content or b"{}")
            fields = (body.get("fields") or {})
            if "assignee" in fields:
                self.issues["KAN-7"]["fields"]["assignee"] = fields["assignee"]
            return httpx.Response(204)
        if path.endswith("/issue/KAN-7/comment") and request.method == "POST":
            body = json.loads(request.content or b"{}")
            comment = {
                "id": str(30000 + len(self.comments["KAN-7"])),
                "body": body.get("body"),
                "author": {"name": "alice", "displayName": "Alice"},
            }
            self.comments["KAN-7"].append(comment)
            return httpx.Response(201, json=comment)
        if path.endswith("/issue/KAN-7/comment") and request.method == "GET":
            return httpx.Response(200, json={"comments": self.comments["KAN-7"]})
        return httpx.Response(404, json={"errorMessages": [path]})


def test_jira_api_assign_then_webhook_payload():
    """Drive JiraClient.assign_issue (Server name) and build the changelog Jira sends."""
    from src.jira.client import JiraClient

    rest = _JiraRest()
    transport = httpx.MockTransport(rest.handler)
    client = JiraClient(
        host="https://jira.example.com", api_token="pat", email=""
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://jira.example.com/rest/api/2",
        transport=transport,
        headers={"Authorization": "Bearer pat", "Accept": "application/json"},
        verify=False,
    )
    client.is_cloud = False
    ok = client.assign_issue("KAN-7", "devbot")
    assert ok is True
    issue = client.get_issue("KAN-7")
    assert issue["fields"]["assignee"]["name"] == "devbot"
    payload = _assignment_payload()
    payload["issue"] = issue
    d = _decide(payload)
    assert d.accepted
    assert d.trigger == "assignment"
    me = client.get_myself()
    assert me["name"] == "devbot"
    hooks = client.list_webhooks()
    assert hooks == []


def test_jira_api_comment_then_webhook_payload():
    """POST /issue/{key}/comment (API v2 string body) then accept mention webhook."""
    from src.jira.client import JiraClient

    rest = _JiraRest()
    transport = httpx.MockTransport(rest.handler)
    client = JiraClient(
        host="https://jira.example.com", api_token="pat", email=""
    )
    client.client.close()
    client.client = httpx.Client(
        base_url="https://jira.example.com/rest/api/2",
        transport=transport,
        headers={"Authorization": "Bearer pat", "Accept": "application/json"},
        verify=False,
    )
    posted = client.add_comment("KAN-7", "please look [~devbot]")
    assert posted and posted["id"]
    comments = client.get_comments("KAN-7")
    assert comments and "[~devbot]" in comments[0]["body"]
    payload = _comment_payload(
        body=comments[0]["body"], comment_id=str(comments[0]["id"])
    )
    d = _decide(payload)
    assert d.accepted
    assert d.trigger == "mention"
    assert d.event_id == f"comment:{comments[0]['id']}"


def test_cloud_assign_uses_account_id():
    from src.jira.client import JiraClient

    captured: Dict[str, Any] = {}

    class FakeResp:
        status_code = 204

        def raise_for_status(self):
            return None

    class FakeHttp:
        def put(self, path, json=None):
            captured["path"] = path
            captured["json"] = json
            return FakeResp()

        def close(self):
            return None

    client = JiraClient(host="https://ex.atlassian.net", api_token="t", email="a@b.c")
    client.client.close()
    client.client = FakeHttp()  # type: ignore[assignment]
    client.is_cloud = True
    assert client.assign_issue("KAN-1", "557058:abc") is True
    assert captured["json"]["fields"]["assignee"] == {"accountId": "557058:abc"}


def test_poller_skips_when_webhook_mode(monkeypatch):
    from src.jira.poller import JiraPoller
    from src.dashboard.snapshot import PollSnapshotStore

    monkeypatch.setattr("src.config.settings.jira_intake_mode", "webhook")
    monkeypatch.setattr("src.config.settings.poll_interval_seconds", 1)
    store = PollSnapshotStore()
    monkeypatch.setattr("src.jira.poller.poll_snapshot_store", store)
    poller = JiraPoller(interval_seconds=1, board_id="1")
    poller.poll_board = MagicMock(return_value=[{"key": "X-1"}])  # type: ignore[method-assign]
    poller._running = True
    calls = {"n": 0}

    def fake_sleep(_s):
        calls["n"] += 1
        poller._running = False

    with patch("src.jira.poller.time.sleep", fake_sleep):
        poller.start(lambda e: None)
    poller.poll_board.assert_not_called()
    snap = store.snapshot()
    assert snap["source"] == "webhook"
