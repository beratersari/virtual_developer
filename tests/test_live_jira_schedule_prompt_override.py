"""LIVE Jira: dashboard model/backend/prompt override on an existing ticket.

Creates a real issue that already has Model + Backend in {params}, then
runs the same functions the Scheduled tab uses.

Run::

    VD_LIVE_JIRA=1 .venv/bin/python -m pytest \\
        tests/test_live_jira_schedule_prompt_override.py -v -s

Does not print tokens. Leaves the issue in Jira (commented) so it can be
inspected; no trigger label, so the poller should not pick it up.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.config import settings
from src.issue_git_spec import parse_issue_git_spec
from src.jira.client import JiraClient
from src.jira_connection import probe_jira_connection
from src.scheduler.service import (
    _description_to_text,
    _issue_payload_for_dispatch,
    preview_existing_issue,
    schedule_existing_issue,
)
from src.state.schedule_store import ScheduleStore


def _dotenv_jira_email() -> str:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.is_file():
        return (getattr(settings, "jira_email", "") or "").strip()
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, val = raw.split("=", 1)
        if key.strip() == "JIRA_EMAIL":
            return val.strip().strip('"').strip("'")
    return (getattr(settings, "jira_email", "") or "").strip()


def _ready() -> str:
    if os.environ.get("VD_LIVE_JIRA") != "1":
        return "Set VD_LIVE_JIRA=1"
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    if "atlassian.net" in host.lower() and not _dotenv_jira_email():
        return "Jira Cloud needs JIRA_EMAIL in .env for Basic auth"
    return ""


pytestmark = pytest.mark.skipif(bool(_ready()), reason=_ready() or "live")

E2E_LABEL = "vd-live-schedule"
TICKET_MODEL = "ticket-old-model"
TICKET_BACKEND = "opencode"
DASH_MODEL = "dashboard-new-model"
DASH_BACKEND = "codex"


@pytest.fixture
def jira_client():
    skip = _ready()
    if skip:
        pytest.skip(skip)
    email = _dotenv_jira_email()
    client = JiraClient(email=email)
    probe = probe_jira_connection(
        host=client.host,
        email=email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    return client


def test_live_preview_and_dashboard_override_beats_ticket_params(
    jira_client: JiraClient, tmp_path
):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    original = (
        "Original Jira work text. Safe to close.\n\n"
        "{params}\n"
        "Repository: https://gitlab.com/beratersari0/test_project.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        f"Model: {TICKET_MODEL}\n"
        f"Backend: {TICKET_BACKEND}\n"
        "{params}\n"
    )
    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    created = jira_client.create_issue(
        project,
        f"[vd-live-schedule] prompt override {stamp}",
        original,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    assert created and created.get("key"), jira_client.last_error
    key = created["key"]
    print(f"\n[live] created {key} on {jira_client.host}", flush=True)

    preview = preview_existing_issue(key, jira_client=jira_client)
    assert preview.get("ok") is True, preview
    assert preview.get("model") == TICKET_MODEL
    assert preview.get("backend") == TICKET_BACKEND
    assert "Original Jira work text" in (preview.get("description") or "")
    print(
        f"[live] preview model={preview.get('model')} "
        f"backend={preview.get('backend')}",
        flush=True,
    )

    edited = (
        "EDITED ON DASHBOARD before run. Safe to close.\n\n"
        "{params}\n"
        "Repository: https://gitlab.com/beratersari0/test_project.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        f"Model: {TICKET_MODEL}\n"
        f"Backend: {TICKET_BACKEND}\n"
        "{params}\n"
    )
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    out = schedule_existing_issue(
        key,
        scheduled_at="2099-01-01T00:00:00",
        model=DASH_MODEL,
        backend=DASH_BACKEND,
        description=edited,
        jira_client=jira_client,
        store=store,
    )
    assert out.get("ok") is True, out
    snap = out["schedule"]["issue_description"]
    snap_spec, err = parse_issue_git_spec("", snap)
    assert err is None and snap_spec is not None
    assert snap_spec.model == DASH_MODEL
    assert snap_spec.backend == DASH_BACKEND
    assert "EDITED ON DASHBOARD" in snap
    print(
        f"[live] schedule snapshot model={snap_spec.model} "
        f"backend={snap_spec.backend}",
        flush=True,
    )

    live = jira_client.get_issue(key)
    live_text = _description_to_text((live.get("fields") or {}).get("description"))
    live_spec, live_err = parse_issue_git_spec("", live_text)
    jira_write_ok = (
        live_err is None
        and live_spec is not None
        and live_spec.model == DASH_MODEL
        and live_spec.backend == DASH_BACKEND
        and "EDITED ON DASHBOARD" in live_text
    )
    print(
        f"[live] jira write persisted={jira_write_ok} "
        f"live_model={getattr(live_spec, 'model', None)} "
        f"live_backend={getattr(live_spec, 'backend', None)}",
        flush=True,
    )

    rec = store.get(out["schedule"]["schedule_id"])
    payload = _issue_payload_for_dispatch(rec, jira_client=jira_client)
    dispatch_text = payload["fields"]["description"]
    dispatch_spec, d_err = parse_issue_git_spec("", dispatch_text)
    assert d_err is None and dispatch_spec is not None
    assert dispatch_spec.model == DASH_MODEL
    assert dispatch_spec.backend == DASH_BACKEND
    assert "EDITED ON DASHBOARD" in dispatch_text
    print("[live] dispatch payload used dashboard model/backend/prompt", flush=True)

    jira_client.add_comment(
        key,
        "[vd-live-schedule] override check finished. Safe to delete.",
    )
    assert jira_write_ok, (
        "Dashboard override is applied at dispatch, but Jira itself still "
        f"has model={getattr(live_spec, 'model', None)} "
        f"backend={getattr(live_spec, 'backend', None)}. "
        "update_issue did not persist the edited prompt."
    )
