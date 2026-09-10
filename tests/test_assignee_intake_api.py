"""Integration tests for assignee-only Jira intake (TRIGGER_LABELS removed).

Always-on: dashboard Settings / poll HTTP API.
Live (opt-in): real Jira Cloud REST — create, assign, GET — then the real
``JiraPoller.poll_board`` on those payloads.

    VD_LIVE_JIRA=1  or  VD_LIVE_PLAN_BUILD=1
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.snapshot import PollSnapshotStore
from src.jira.triggers import poller_triggers_on
from src.state.manager import JiraStateManager


E2E_LABEL = "vd-intake-e2e"


def _dotenv_map() -> Dict[str, str]:
    env = Path(__file__).resolve().parents[1] / ".env"
    out: Dict[str, str] = {}
    if not env.is_file():
        return out
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _live_flag() -> bool:
    for name in ("VD_LIVE_JIRA", "VD_LIVE_PLAN_BUILD"):
        if (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes"}:
            return True
    return False


def _isolate_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime_settings.json"
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: path)
    return path


def test_settings_api_has_no_trigger_labels(tmp_path, monkeypatch):
    """GET /api/settings must not expose the removed TRIGGER_LABELS field."""
    _isolate_runtime(tmp_path, monkeypatch)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    http = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    r = http.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert "trigger_labels" not in body
    assert "jira_trigger_user" in body
    assert "gitlab_trigger_user" in body
    assert "azure_trigger_user" in body
    assert "trigger_assignee_names" in body
    assert "trigger_mentions" in body
    assert "trigger_on_assignment" not in body


def test_settings_api_ignores_trigger_labels_patch(tmp_path, monkeypatch):
    """Old clients sending trigger_labels must not 422 or persist a label list."""
    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "trigger_assignee_names", "devbot")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    http = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    r = http.patch(
        "/api/settings",
        json={
            "trigger_labels": "bot,ai-assist",
            "trigger_on_assignment": False,
            "trigger_assignee_names": "beratersari,devbot",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "trigger_labels" not in body
    assert "trigger_on_assignment" not in body
    assert "beratersari" in (body.get("jira_trigger_user") or "")
    assert "beratersari" in (body.get("trigger_assignee_names") or "")
    again = http.get("/api/settings")
    assert again.status_code == 200
    assert "trigger_labels" not in again.json()


def test_poll_api_has_no_matched_label_fields(tmp_path):
    """Poll DTO no longer carries trigger-label match flags."""
    from src.dashboard.service import build_poll_status

    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = PollSnapshotStore()
    store.end_poll(
        source="board 1",
        issues=[
            {
                "key": "API-1",
                "summary": "assigned",
                "jira_status": "To Do",
                "labels": ["bot"],
                "assignee": "Beratersari",
                "matched_assignee": True,
                "is_todo": True,
                "will_process": True,
            }
        ],
        interval_seconds=30,
    )
    poll = build_poll_status(store, sm)
    item = poll.issues[0].model_dump()
    assert "matched_label" not in item
    assert "matched_labels" not in item
    assert item["matched_assignee"] is True
    http = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    with patch("src.dashboard.api.poll_snapshot_store", store):
        with patch("src.dashboard.service.poll_snapshot_store", store):
            r = http.get("/api/poll")
    assert r.status_code == 200
    row = r.json()["issues"][0]
    assert "matched_label" not in row
    assert "matched_labels" not in row
    assert row["matched_assignee"] is True


def _live_jira():
    from src.jira.client import JiraClient
    from src.jira_connection import probe_jira_connection

    vals = _dotenv_map()
    host = (vals.get("JIRA_HOST") or "").strip()
    email = (vals.get("JIRA_EMAIL") or "").strip()
    token = (vals.get("JIRA_API_TOKEN") or "").strip()
    if not host or not token or "your-jira.example" in host:
        pytest.skip("JIRA_HOST / JIRA_API_TOKEN not configured in .env")
    if "atlassian.net" in host.lower() and not email:
        pytest.skip("Jira Cloud needs JIRA_EMAIL in .env")
    probe = probe_jira_connection(host=host, email=email, api_token=token)
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    return JiraClient(host=host, email=email, api_token=token)


def test_live_jira_assignee_only_intake_via_rest_api(tmp_path, monkeypatch):
    """Real Jira REST: labels do not start work; assignee match does.

    Creates two To Do tickets:
    * label-only (bot + ai-assist, unassigned) → poller skip
    * assignee-only (no trigger labels, assigned to PAT user) → poller accept
    """
    if not _live_flag():
        pytest.skip("Set VD_LIVE_JIRA=1 or VD_LIVE_PLAN_BUILD=1")

    from src.jira.poller import JiraPoller

    jira = _live_jira()
    me = jira.get_myself()
    assert me and isinstance(me, dict), "GET /myself failed"
    account_id = str(me.get("accountId") or "").strip()
    display = str(me.get("displayName") or me.get("emailAddress") or "").strip()
    assert account_id, "Cloud assign needs accountId from /myself"
    assert display, "need displayName for TRIGGER_ASSIGNEE_NAMES"

    monkeypatch.setattr(settings, "trigger_assignee_names", display.lower())

    project = ((_dotenv_map().get("JIRA_PROJECTS") or "KAN").split(",")[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    params = (
        "vd assignee-intake e2e (automated; safe to close).\n"
        "{params}\n"
        "Repository: https://gitlab.com/beratersari0/test_project.git\n"
        f"Source branch: feature/vd-intake-{stamp}\n"
        "Target branch: main\n"
        "Mode: plan\n"
        "{params}\n"
    )

    labeled = jira.create_issue(
        project,
        f"[vd-intake] label-only skip {stamp}",
        params,
        issue_type="Task",
        labels=[E2E_LABEL, "bot", "ai-assist"],
    )
    assigned = jira.create_issue(
        project,
        f"[vd-intake] assignee-only accept {stamp}",
        params,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    assert labeled and labeled.get("key"), jira.last_error
    assert assigned and assigned.get("key"), jira.last_error
    label_key = labeled["key"]
    assign_key = assigned["key"]
    print(f"\n[live intake] label-only {label_key}", flush=True)
    print(f"[live intake] assignee-only {assign_key}", flush=True)

    ok = jira.assign_issue(assign_key, account_id)
    assert ok, f"PUT assignee failed for {assign_key}: {jira.last_error}"

    live_label = jira.get_issue(label_key, fields=["summary", "labels", "assignee", "status"])
    live_assign = jira.get_issue(assign_key, fields=["summary", "labels", "assignee", "status"])
    assert live_label and live_assign
    label_fields = live_label.get("fields") or {}
    assign_fields = live_assign.get("fields") or {}

    label_names = {str(x).lower() for x in (label_fields.get("labels") or [])}
    assert "bot" in label_names
    assert "ai-assist" in label_names
    assert label_fields.get("assignee") in (None, {})

    assignee = assign_fields.get("assignee") or {}
    assert assignee.get("accountId") == account_id
    assign_labels = {str(x).lower() for x in (assign_fields.get("labels") or [])}
    assert "bot" not in assign_labels
    assert "ai-assist" not in assign_labels

    poller = JiraPoller(client=jira, board_id="1", interval_seconds=30)
    poller.state_manager = JiraStateManager(state_dir=tmp_path / "state")

    label_bot = poller._is_assigned_to_jira_ai_bot(label_key, label_fields)
    assign_bot = poller._is_assigned_to_jira_ai_bot(assign_key, assign_fields)
    assert label_bot is False
    assert assign_bot is True
    assert poller_triggers_on(assigned_to_bot=label_bot) is False
    assert poller_triggers_on(assigned_to_bot=assign_bot) is True

    # Real poller loop on the live REST payloads (board fetch stubbed).
    with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
        with patch.object(
            jira,
            "get_sprint_issues",
            return_value=[live_label, live_assign],
        ):
            accepted = poller.poll_board()
    keys = [i["key"] for i in accepted]
    assert assign_key in keys, f"assignee-only ticket not accepted: {keys}"
    assert label_key not in keys, f"label-only ticket was accepted: {keys}"

    snap = None
    from src.dashboard.snapshot import poll_snapshot_store

    snap = poll_snapshot_store.snapshot()
    rows = {r.get("key"): r for r in (snap.get("issues") or [])}
    if assign_key in rows:
        assert rows[assign_key].get("matched_assignee") is True
        assert "matched_label" not in rows[assign_key]
    if label_key in rows:
        assert rows[label_key].get("matched_assignee") is False
        assert rows[label_key].get("will_process") is False


def _params_block(stamp: str, mode: str) -> str:
    return (
        "vd mode-handoff e2e (automated; safe to close).\n"
        "{params}\n"
        "Repository: https://gitlab.com/beratersari0/test_project.git\n"
        f"Source branch: feature/vd-mode-{stamp}\n"
        "Target branch: main\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def test_dashboard_start_disabled_points_at_mode_build(tmp_path, monkeypatch):
    """POST /api/tasks/{key}/start stays 410; operator uses plan_execute."""
    _isolate_runtime(tmp_path, monkeypatch)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-START", "s", _params_block("x", "plan"))
    http = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    r = http.post("/api/tasks/KAN-START/start")
    assert r.status_code == 410
    detail = r.json().get("detail") or ""
    assert "plan_execute" in detail
    assert "ai-start-work" not in detail
    assert "ai-execute" not in detail


@pytest.mark.asyncio
async def test_live_jira_mode_plan_does_not_build_until_plan_execute(
    tmp_path, monkeypatch
):
    """Real Jira REST: plan_ready never builds; plan_execute + In Progress does.

    1. POST issue with Mode: plan, assign PAT user
    2. Local plan_ready + poller on live GET → no implementation
    3. Add plan_execute (In Progress)
    4. Poller + processor on live GET → execution starts
    """
    if not _live_flag():
        pytest.skip("Set VD_LIVE_JIRA=1 or VD_LIVE_PLAN_BUILD=1")

    from unittest.mock import AsyncMock

    from src.jira.poller import JiraPoller
    from src.processor import JobProcessor
    from src.reporter.jira_reporter import JiraReporter
    from src.state.models import TaskStatus

    jira = _live_jira()
    me = jira.get_myself()
    assert me and isinstance(me, dict), "GET /myself failed"
    account_id = str(me.get("accountId") or "").strip()
    display = str(me.get("displayName") or me.get("emailAddress") or "").strip()
    assert account_id and display
    monkeypatch.setattr(settings, "trigger_assignee_names", display.lower())

    project = ((_dotenv_map().get("JIRA_PROJECTS") or "KAN").split(",")[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    created = jira.create_issue(
        project,
        f"[vd-mode] plan then build {stamp}",
        _params_block(stamp, "plan"),
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    assert created and created.get("key"), jira.last_error
    key = created["key"]
    print(f"\n[live mode] created {key}", flush=True)
    assert jira.assign_issue(key, account_id), jira.last_error

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state(key, f"[vd-mode] plan then build {stamp}", _params_block(stamp, "plan"))
    plan_file = tmp_path / f"{key}.md"
    plan_file.write_text("# plan\n", encoding="utf-8")
    sm.update_state(key, status=TaskStatus.PLAN_READY, plan_path=str(plan_file))

    live_plan = jira.get_issue(
        key, fields=["summary", "description", "labels", "assignee", "status"]
    )
    assert live_plan
    poller = JiraPoller(client=jira, board_id="1", interval_seconds=30)
    poller.state_manager = sm
    poller._seen_issues.add(key)
    with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
        with patch.object(jira, "get_sprint_issues", return_value=[live_plan]):
            skipped = poller.poll_board()
    assert key not in [i["key"] for i in skipped], (
        f"Mode: plan ticket was sent to implement: {[i['key'] for i in skipped]}"
    )
    print(f"[live mode] {key} plan_ready + Mode: plan → poller skip", flush=True)

    assert jira.add_labels(key, ["plan_execute"]), jira.last_error
    live_build = jira.get_issue(
        key, fields=["summary", "description", "labels", "assignee", "status"]
    )
    assert live_build
    live_build.setdefault("fields", {})["status"] = {
        "name": "In Progress",
        "statusCategory": {"key": "indeterminate"},
    }
    poller._plan_start_emitted.discard(key)
    with patch.object(jira, "get_active_sprint", return_value={"id": 1, "name": "S"}):
        with patch.object(jira, "get_sprint_issues", return_value=[live_build]):
            accepted = poller.poll_board()
    assert key in [i["key"] for i in accepted], (
        f"plan_execute ticket not accepted: {[i['key'] for i in accepted]}"
    )
    print(f"[live mode] {key} plan_execute → poller accept", flush=True)

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = JiraReporter(client=jira)
    proc.jira_client = jira
    proc._start_execution_workflow = AsyncMock(return_value=None)

    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": live_build,
    }
    outcome = await proc.process_event(event)
    assert outcome.get("work_started") is True, outcome
    proc._start_execution_workflow.assert_awaited()
    print(f"[live mode] {key} processor started execution", flush=True)
