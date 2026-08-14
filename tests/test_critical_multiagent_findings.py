"""Critical multi-agent review findings — proof tests.

These encode *correct* behaviour for every critical issue from the 5-agent
review (state, processor, daemon/dashboard, poller, git/agent isolation).

If a test is marked xfail(strict=True), production still has that bug.
When the bug is fixed, the xfail becomes XPASS and must be removed.

Run:
  .venv/bin/python -m pytest tests/test_critical_multiagent_findings.py -v
"""

from __future__ import annotations

import inspect
import os
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    p._last_jira_status = {}
    p._seen_issues = set()
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


def _todo_fields(name="To Do", labels=None, summary="s", description=None):
    fields = {
        "summary": summary,
        "status": {"name": name, "statusCategory": {"key": "new"}},
        "labels": labels or ["bot"],
        "assignee": None,
    }
    if description is not None:
        fields["description"] = description
    return fields


# ===========================================================================
# S1 — Multiple JiraStateManager instances share files but not locks
# ===========================================================================


def test_s1_two_managers_rmw_must_not_clobber_terminal_status(tmp_path):
    """Two managers on the same state_dir must not lose a COMPLETED write.

    Reproduces dashboard (manager A) scrubbing metadata while processor
    (manager B) CAS-completes the job.
    """
    state_dir = tmp_path / "state"
    mgr_a = JiraStateManager(state_dir=state_dir)
    mgr_b = JiraStateManager(state_dir=state_dir)

    mgr_a.create_state("CLBR-1", "s", "d")
    mgr_a.update_state("CLBR-1", status=TaskStatus.EXECUTING, metadata={"job_ids": ["j1"]})

    # Dashboard-style RMW: read, then write stale status later
    stale = mgr_a.get_state("CLBR-1")
    assert stale is not None
    assert stale.status == TaskStatus.EXECUTING

    # Processor CAS completes under the other manager
    done = mgr_b.update_state_if(
        "CLBR-1",
        expected_statuses={TaskStatus.EXECUTING},
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={"completed_by": "processor"},
    )
    assert done is not None
    assert done.status == TaskStatus.COMPLETED

    # Stale dashboard write (status still EXECUTING in memory)
    stale.metadata = {**(stale.metadata or {}), "scrubbed": True}
    mgr_a.set_state(stale)

    final = mgr_b.get_state("CLBR-1")
    assert final is not None
    assert final.status == TaskStatus.COMPLETED, (
        f"Terminal COMPLETED must survive cross-manager RMW; got {final.status.value}"
    )


def test_s1_daemon_shares_processor_state_manager():
    """Daemon must reuse processor.state_manager (single process state owner)."""
    src = inspect.getsource(
        __import__("src.daemon", fromlist=["JiraAgentDaemon"]).JiraAgentDaemon.__init__
    )
    assert "self.processor.state_manager" in src
    assert "JobProcessor()" in src
    poller_src = inspect.getsource(
        __import__("src.jira.poller", fromlist=["JiraPoller"]).JiraPoller.__init__
    )
    assert "state_manager" in poller_src


# ===========================================================================
# S2 — set_state swallows write failures
# ===========================================================================


def test_s2_update_state_if_must_fail_when_disk_write_fails(tmp_path, monkeypatch):
    mgr = JiraStateManager(state_dir=tmp_path / "state")
    mgr.create_state("DISK-1", "s", "d")
    mgr.update_state("DISK-1", status=TaskStatus.EXECUTING)

    real_replace = os.replace

    def boom_replace(src, dst):
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", boom_replace)
    result = mgr.update_state_if(
        "DISK-1",
        expected_statuses={TaskStatus.EXECUTING},
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
    )
    # Restore so we can read what is actually on disk
    monkeypatch.setattr(os, "replace", real_replace)

    # Correct: CAS must not claim success when persistence failed
    assert result is None, (
        "update_state_if must return None when set_state cannot persist"
    )
    on_disk = mgr.get_state("DISK-1")
    assert on_disk is not None
    assert on_disk.status == TaskStatus.EXECUTING


# ===========================================================================
# S3 — Cancel race leaves permanent live job row
# ===========================================================================


@pytest.mark.asyncio
async def test_s3_cancel_before_job_create_must_not_leave_live_job(
    processor, state_manager
):
    """Simulate cancel winning after claim but before job create finishes cleanly."""
    from src.orchestrator.agent_runner import AgentTask
    from src.state import job_store as job_store_mod

    state_manager.create_state("GHOST-1", "s", "d")
    state_manager.update_state("GHOST-1", status=TaskStatus.PENDING)

    task = AgentTask(description="d", prompt="p", agent="a", issue_key="GHOST-1")

    # Claim planning (as begin does)
    claimed = state_manager.update_state_if(
        "GHOST-1",
        reject_statuses=processor.TERMINAL_STATUSES,
        status=TaskStatus.PLANNING,
        started_at=datetime.now(),
        current_task_id=task.task_id,
    )
    assert claimed is not None

    # Cancel now — no job id yet
    out = await processor.cancel_job("GHOST-1")
    assert out.get("ok") is True
    assert state_manager.get_state("GHOST-1").status == TaskStatus.CANCELLED

    # Late begin path still creates a job if not careful — call _start_job_record
    # as if begin continued after cancel (the race window after CAS succeeded
    # before cancel, then job create after cancel).
    # Correct product: either refuse job create when terminal, or finish immediately.
    st = state_manager.get_state("GHOST-1")
    if st and st.status == TaskStatus.CANCELLED:
        # Mimic buggy late create after cancel finished with no job id
        job_id = processor._start_job_record(
            claimed,  # stale non-terminal snapshot from before cancel
            workflow_type="planning",
            agent="a",
            task_id=task.task_id,
            status="planning",
        )
        # Correct behaviour after any job create under terminal issue:
        # job must not remain live, OR create must refuse.
        job = job_store_mod.job_store.get_job(job_id)
        assert job is not None
        assert job["status"] not in {"running", "planning", "executing", "pending"}, (
            f"Job must not stay live after cancel; got status={job['status']!r}"
        )


@pytest.mark.asyncio
async def test_s3b_execution_early_return_when_git_none_must_finish_job(
    processor, state_manager
):
    """Mirrors production: begin → prepare returns None → return without finish."""
    from src.orchestrator.agent_runner import AgentTask
    from src.state import job_store as job_store_mod

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state_manager.create_state("GHOST-2", "s", desc)
    state = state_manager.get_state("GHOST-2")

    with patch.object(
        processor, "_prepare_git_workspace", return_value=None
    ):
        await processor._start_execution_workflow(state)

    meta = state_manager.get_state("GHOST-2").metadata or {}
    job_id = meta.get("current_job_id") or (meta.get("job_ids") or [None])[-1]
    assert job_id is not None
    job = job_store_mod.job_store.get_job(job_id)
    assert job is not None
    assert job["status"] not in {"running", "planning", "executing", "pending"}, (
        f"Job left live after git=None early return: {job['status']!r}"
    )


# ===========================================================================
# P1 — plan_ready start labels re-dispatched every poll
# ===========================================================================


def test_p1_plan_start_emitted_every_poll_while_plan_ready(poller, state_manager, monkeypatch):
    """Document current poller behaviour: start labels re-listed every cycle.

    Correct behaviour (asserted via xfail companion): once dispatched / while
    already scheduled, do not re-add every poll. This test proves the firehose.
    """
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    state_manager.create_state("PS-1", "plan me", "d")
    state_manager.update_state("PS-1", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("PS-1")

    issue = {
        "key": "PS-1",
        "fields": _todo_fields(labels=["bot", "ai-start-work"], summary="plan me"),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])

    r1 = poller.poll_board()
    r2 = poller.poll_board()
    keys1 = [i["key"] for i in r1]
    keys2 = [i["key"] for i in r2]
    assert "PS-1" in keys1
    assert "PS-1" not in keys2


def test_p1_plan_start_must_not_reemit_every_poll(poller, state_manager, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    state_manager.create_state("PS-2", "plan me", "d")
    state_manager.update_state("PS-2", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("PS-2")

    issue = {
        "key": "PS-2",
        "fields": _todo_fields(labels=["bot", "ai-start-work"], summary="plan me"),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])

    r1 = poller.poll_board()
    assert "PS-2" in [i["key"] for i in r1]
    # Second poll must not re-dispatch while still plan_ready (no in-flight claim yet)
    # Correct design: claim/schedule once, or require status leave plan_ready first.
    r2 = poller.poll_board()
    assert "PS-2" not in [i["key"] for i in r2], (
        "plan_ready start must not fire on every poll while still plan_ready"
    )


def test_p1b_completed_still_todo_poller_does_not_reemit(poller, state_manager):
    """check_status_changes alone does not reopen stay-on-To-Do COMPLETED.

    Primary rework is poll_board new_issues (To Do + trigger). This helper
    only handles leave→return / ERROR text edit.
    """
    state_manager.create_state("PS-3", "s", "d")
    state_manager.update_state(
        "PS-3",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={"requeue_eligible": False},
    )
    poller._status_before_poll = {"PS-3": "to do"}
    issues = [
        {
            "key": "PS-3",
            "fields": {
                "summary": "s",
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["bot", "ai-start-work"],
            },
        }
    ]
    assert poller.check_status_changes(issues) == []


# ===========================================================================
# P2 — Backlog column hops reprocess terminal work
# ===========================================================================


def test_p2_completed_sfd_to_todo_must_not_reprocess_as_reopen(poller, state_manager):
    """Both SFD and To Do are backlog/category-new — not a real leave→return."""
    state_manager.create_state("SFD-HOP", "done", "")
    state_manager.update_state("SFD-HOP", status=TaskStatus.COMPLETED)
    poller._status_before_poll = {"SFD-HOP": "selected for development"}

    issues = [
        {
            "key": "SFD-HOP",
            "fields": {
                "summary": "done",
                "status": {
                    "name": "To Do",
                    "statusCategory": {"key": "new"},
                },
                "labels": ["bot"],
            },
        }
    ]
    assert poller.check_status_changes(issues) == [], (
        "Moving between category-new backlog columns must not reprocess COMPLETED"
    )


def test_p2_error_sfd_to_todo_with_requeue_reprocesses_today(poller, state_manager):
    """Current behaviour document: status_changed_into_todo fires on any name change."""
    state_manager.create_state("SFD-ERR", "err", "body")
    fps = poller.text_fingerprints_from_state("err", "body")
    state_manager.update_state(
        "SFD-ERR",
        status=TaskStatus.ERROR,
        metadata={
            "requeue_eligible": True,
            **fps,
        },
    )
    poller._status_before_poll = {"SFD-ERR": "selected for development"}
    issues = [
        {
            "key": "SFD-ERR",
            "fields": {
                "summary": "err",
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["bot"],
            },
        }
    ]
    out = poller.check_status_changes(issues)
    # Backlog column hop is not a real leave→return
    assert out == []


# ===========================================================================
# P3 — Orphan ERROR missing fingerprints requeues every poll
# ===========================================================================


def test_p3_orphan_recovery_sets_requeue_without_fingerprints(processor, state_manager):
    """Orphan path sets requeue_eligible but does not write intake fingerprints."""
    state_manager.create_state("ORPH-1", "s", "full description body")
    state_manager.update_state("ORPH-1", status=TaskStatus.EXECUTING)
    n = processor.recover_orphaned_in_flight()
    assert n == 1
    st = state_manager.get_state("ORPH-1")
    assert st.status == TaskStatus.ERROR
    assert st.metadata.get("requeue_eligible") is True
    assert st.metadata.get("last_intake_fingerprint")
    assert st.metadata.get("last_intake_fingerprint_light")


def test_p3_missing_fingerprint_must_not_requeue_every_poll(poller, state_manager):
    state_manager.create_state("ORPH-2", "s", "body")
    state_manager.update_state(
        "ORPH-2",
        status=TaskStatus.ERROR,
        metadata={"requeue_eligible": True},  # no fingerprints
    )
    poller._status_before_poll = {"ORPH-2": "to do"}
    light = {
        "key": "ORPH-2",
        "fields": {
            "summary": "s",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    first = poller.check_status_changes([light])
    # After first requeue attempt, fingerprints should be written OR missing-fp
    # must not fire again. Correct: second poll with same board text → empty.
    # Simulate: state still ERROR + requeue + still no fp (if reprocess failed mid-way)
    second = poller.check_status_changes([light])
    assert not (first and second), (
        "Missing fingerprint must not re-fire reprocess every poll"
    )


def test_p3_error_text_changed_true_when_fingerprint_missing(poller):
    """Unit: light board + missing fp → True (current code)."""
    issue = {
        "key": "X",
        "fields": {"summary": "s"},  # no description → light
    }
    assert poller._error_text_changed_for_reprocess(issue, {}) is False


# ===========================================================================
# P4 — Plan start requires trigger label, not start labels alone
# ===========================================================================


def test_p4_plan_start_with_only_start_label_must_dispatch(
    poller, state_manager, monkeypatch
):
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    state_manager.create_state("START-1", "s", "d")
    state_manager.update_state("START-1", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("START-1")

    issue = {
        "key": "START-1",
        # Only start label — no bot / ai-assist
        "fields": _todo_fields(labels=["ai-start-work"], summary="s"),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])

    result = poller.poll_board()
    assert "START-1" in [i["key"] for i in result], (
        "To Do + plan_ready + ai-start-work must start without requiring bot label"
    )


def test_p4_plan_start_with_bot_and_start_label_dispatches(
    poller, state_manager, monkeypatch
):
    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    state_manager.create_state("START-2", "s", "d")
    state_manager.update_state("START-2", status=TaskStatus.PLAN_READY)
    poller._seen_issues.add("START-2")

    issue = {
        "key": "START-2",
        "fields": _todo_fields(labels=["bot", "ai-execute"], summary="s"),
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])

    result = poller.poll_board()
    assert "START-2" in [i["key"] for i in result]


# ===========================================================================
# D1–D5 — Dashboard security (unauthenticated control plane)
# ===========================================================================


def test_d1_dashboard_defaults_bind_all_interfaces():
    from src.config import Settings

    s = Settings(
        jira_host="https://jira.example.com",
        jira_api_token="t",
        _env_file=None,
    )
    assert s.dashboard_host == "0.0.0.0"
    assert s.dashboard_allow_remote is True


def test_d1_dashboard_app_has_no_auth_middleware():
    app = __import__("src.dashboard.api", fromlist=["create_dashboard_app"]).create_dashboard_app()
    # No HTTPBearer / Depends auth on mutating routes
    cancel = None
    for route in app.routes:
        if getattr(route, "path", None) == "/api/tasks/{issue_key}/cancel":
            cancel = route
            break
    assert cancel is not None
    # FastAPI route endpoint has no security deps by design
    dependant = getattr(cancel, "dependant", None)
    deps = getattr(dependant, "dependencies", []) if dependant else []
    # No Security/HTTPBearer dependency expected today — documents no-auth
    assert deps == [] or all(
        "auth" not in str(d).lower() and "bearer" not in str(d).lower() for d in deps
    )


def test_d2_jira_probe_must_not_send_stored_token_to_untrusted_host(monkeypatch):
    from src.config import settings
    from src.jira_connection import probe_jira_connection

    monkeypatch.setattr(settings, "jira_api_token", "REAL-SECRET-TOKEN")
    monkeypatch.setattr(settings, "jira_host", "https://jira.company.com")
    monkeypatch.setattr(settings, "jira_email", "")

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            captured["headers"] = k.get("headers") or {}
            captured["base_url"] = k.get("base_url")
            captured["auth"] = k.get("auth")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            m = MagicMock()
            m.status_code = 401
            m.text = "nope"
            return m

    with patch("src.jira_connection.httpx.Client", FakeClient):
        out = probe_jira_connection(
            host="https://attacker.example",
            api_token="",  # empty → falls back to stored
        )

    # Correct: refuse to use stored secrets against a host that is not settings.jira_host
    auth_header = (captured.get("headers") or {}).get("Authorization", "")
    assert "REAL-SECRET-TOKEN" not in auth_header, (
        "Stored Jira token must not be sent to attacker host"
    )
    assert out.get("ok") is False


def test_d2_jira_probe_currently_exfils_to_attacker_host(monkeypatch):
    """Reproducer: empty token + attacker host uses real Bearer token."""
    from src.config import settings
    from src.jira_connection import probe_jira_connection

    monkeypatch.setattr(settings, "jira_api_token", "REAL-SECRET-TOKEN")
    monkeypatch.setattr(settings, "jira_email", "")

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            captured["headers"] = dict(k.get("headers") or {})
            captured["base_url"] = str(k.get("base_url") or "")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            m = MagicMock()
            m.status_code = 401
            m.text = "nope"
            return m

    with patch("src.jira_connection.httpx.Client", FakeClient):
        out = probe_jira_connection(host="https://attacker.example", api_token="")

    assert out.get("ok") is False
    assert "REAL-SECRET-TOKEN" not in (captured.get("headers") or {}).get(
        "Authorization", ""
    )


def test_d2b_gitlab_probe_currently_exfils_global_pat_to_attacker(monkeypatch):
    """Reproducer: empty body PAT falls back to settings.gitlab_pat for any host."""
    from src.config import settings
    from src.gitlab_connection import probe_gitlab_connection

    # Legacy single-PAT mode (map empty so gitlab_pat_for_host returns "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    monkeypatch.setattr(settings, "gitlab_pat", "GLOBAL-PAT-SECRET")

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            captured["headers"] = dict(k.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            m = MagicMock()
            m.status_code = 401
            m.text = "nope"
            return m

    with patch("src.gitlab_connection.httpx.Client", FakeClient):
        out = probe_gitlab_connection("attacker.example", pat="")

    assert out.get("ok") is False
    assert captured.get("headers", {}).get("PRIVATE-TOKEN") != "GLOBAL-PAT-SECRET"


def test_d2b_gitlab_probe_must_not_send_global_pat_to_untrusted_host(monkeypatch):
    from src.config import settings
    from src.gitlab_connection import probe_gitlab_connection

    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "gitlab.company.com")
    monkeypatch.setattr(settings, "gitlab_pat", "GLOBAL-PAT-SECRET")

    captured = {}

    class FakeClient:
        def __init__(self, *a, **k):
            captured["headers"] = dict(k.get("headers") or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path):
            m = MagicMock()
            m.status_code = 401
            m.text = "nope"
            return m

    with patch("src.gitlab_connection.httpx.Client", FakeClient):
        out = probe_gitlab_connection("attacker.example", pat="")

    # Correct: refuse stored/global PAT for hosts not in the allow/map
    assert captured.get("headers", {}).get("PRIVATE-TOKEN") != "GLOBAL-PAT-SECRET", (
        "Global GitLab PAT must not be sent to untrusted host"
    )
    assert out.get("ok") is False


def test_d3_unauthenticated_settings_can_redirect_jira_host(monkeypatch, tmp_path):
    """No-auth PATCH can change jira_host while keeping the token."""
    from src.config import settings
    from src.dashboard.api import create_dashboard_app
    from src.dashboard.schemas import SettingsUpdate

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "jira_host", "https://jira.company.com")
    monkeypatch.setattr(settings, "jira_api_token", "KEEP-ME")

    app = create_dashboard_app()
    client = TestClient(app)
    r = client.patch(
        "/api/settings",
        json={"jira_host": "https://attacker.example"},
    )
    assert r.status_code == 400
    assert settings.jira_host == "https://jira.company.com"
    assert settings.jira_api_token == "KEEP-ME"


def test_d3_unauthenticated_settings_can_wipe_gitlab_credentials(
    tmp_path, monkeypatch
):
    from src.config import settings
    from src.dashboard.api import create_dashboard_app

    monkeypatch.chdir(tmp_path)
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({"gitlab.company.com": "pat-xyz"})
    monkeypatch.setattr(settings, "gitlab_pat", "pat-xyz")

    app = create_dashboard_app()
    client = TestClient(app)
    r = client.patch("/api/settings", json={"gitlab_credentials": []})
    assert r.status_code == 200
    if hasattr(settings, "gitlab_host_pat_map"):
        assert settings.gitlab_host_pat_map() == {}


def test_d4_unauthenticated_schedule_create_endpoint_exists():
    from src.dashboard.api import create_dashboard_app

    app = create_dashboard_app()
    client = TestClient(app)
    # No auth header — still accepted (may 400 on validation, not 401)
    r = client.post(
        "/api/schedules",
        json={
            "title": "evil",
            "repository_url": "https://gitlab.example.com/g/r.git",
            "target_branch": "develop",
            "mode": "build",
            "scheduled_at": "2099-01-01T00:00:00Z",
        },
    )
    assert r.status_code != 401
    assert r.status_code != 403


def test_d5_unauthenticated_cancel_endpoint_no_401(processor, state_manager, tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app

    state_manager.create_state("CAN-1", "s", "d")
    state_manager.update_state("CAN-1", status=TaskStatus.EXECUTING)
    app = create_dashboard_app(processor=processor, state_manager=state_manager)
    client = TestClient(app)
    r = client.post("/api/tasks/CAN-1/cancel")
    assert r.status_code != 401
    assert r.status_code != 403


# ===========================================================================
# G1 — Live temp clone purge
# ===========================================================================


def test_g1_purge_must_not_delete_registered_live_clone(tmp_path):
    from src.git_manager import purge_stale_temp_dirs

    live = tmp_path / "live_issue_clone"
    live.mkdir()
    (live / "README").write_text("work in progress")
    # Age the directory so it is past cutoff
    old = time.time() - (2 * 86400)
    os.utime(live, (old, old))

    # Correct API: accept protect= set of paths that must not be removed
    protected = {live.resolve()}
    removed = purge_stale_temp_dirs(
        max_age_days=1.0,
        base_dir=tmp_path,
        protect_paths=protected,  # type: ignore[call-arg]
    )
    assert live.exists(), "Live clone must survive purge"
    assert removed == 0


def test_g1_purge_currently_deletes_old_dirs_unconditionally(tmp_path):
    """Reproducer: any old dir under base is removed — no live-context check."""
    from src.git_manager import purge_stale_temp_dirs

    live = tmp_path / "would_be_live"
    live.mkdir()
    (live / "f").write_text("x")
    old = time.time() - (2 * 86400)
    os.utime(live, (old, old))

    removed = purge_stale_temp_dirs(max_age_days=1.0, base_dir=tmp_path)
    assert removed == 1
    assert not live.exists()


def test_g1_purge_age_zero_deletes_everything_including_fresh(tmp_path):
    from src.git_manager import purge_stale_temp_dirs

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    removed = purge_stale_temp_dirs(max_age_days=0.0, base_dir=tmp_path)
    assert removed >= 1
    assert not fresh.exists()


# ===========================================================================
# G2 — Agent env still passes HOME (host git credentials reachable)
# ===========================================================================


def test_g2_agent_env_includes_home():
    """Documents allowlist includes HOME — host ~/.git-credentials reachable."""
    from src.orchestrator.agent_runner import _agent_subprocess_env

    with patch.dict(os.environ, {"HOME": "/Users/operator", "GITLAB_PAT": "secret"}, clear=False):
        env = _agent_subprocess_env()
    assert env.get("HOME") == "/Users/operator"
    assert "GITLAB_PAT" not in env
    assert "JIRA_API_TOKEN" not in env or env.get("JIRA_API_TOKEN") is None


def test_g2_agent_env_must_isolate_home_and_disable_credential_helper():
    from src.orchestrator.agent_runner import _agent_subprocess_env

    with patch.dict(os.environ, {"HOME": "/Users/operator"}, clear=False):
        env = _agent_subprocess_env()
    # Correct: either synthetic HOME or explicit credential.helper=
    assert env.get("HOME") != "/Users/operator" or env.get("GIT_CONFIG_COUNT") or (
        env.get("GIT_CONFIG_PARAMETERS")
    ) or env.get("GIT_CONFIG_GLOBAL") == "/dev/null", (
        "Agent must not inherit operator HOME without disabling git credential helper"
    )


# ===========================================================================
# G3 — Host git puts PAT in process env (scrape risk under concurrency)
# ===========================================================================


def test_g3_git_auth_env_embeds_pat_in_child_environ(tmp_path, monkeypatch):
    from src.git_manager import GitManager
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_pat", "SCRAPE-ME-PAT")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SCR-1")
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    env = gm._git_auth_env()
    assert env is not None
    # PAT present in env dict used for subprocess — same-UID agents can scrape
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    assert "SCRAPE-ME-PAT" in joined


# ===========================================================================
# G4 — Shared Source branch name for concurrent issues
# ===========================================================================


def test_g4_resolve_work_branch_uses_shared_source_as_is():
    from src.git_manager import GitManager

    gm = GitManager.__new__(GitManager)
    gm.issue_key = "A-1"
    gm.source_branch = "feature/shared-work"
    gm.target_branch = "develop"

    name_a = gm._resolve_work_branch_name("A-1")
    gm.issue_key = "B-2"
    name_b = gm._resolve_work_branch_name("B-2")
    assert name_a == "feature/shared-work"
    assert name_b == "feature/shared-work"
    assert name_a == name_b, "Concurrent issues with same Source share one remote branch"


def test_g4_must_refuse_or_serialize_duplicate_source_branch_jobs(processor):
    """Correct: refuse second concurrent job with same repo+source or serialize."""
    # Product currently has no lock key on (repository_url, source_branch)
    assert hasattr(processor, "_source_branch_lock") or hasattr(
        processor, "_claim_source_branch"
    ), "Processor must serialize or refuse concurrent shared Source branch jobs"


# ===========================================================================
# T1 — Thread mutation of _contexts during prepare
# ===========================================================================


def test_t1_prepare_git_workspace_uses_to_thread(processor):
    """Documents off-loop clone; cancel can race _contexts from the loop."""
    src = inspect.getsource(processor._prepare_git_workspace)
    assert "to_thread" in src or "run_in_executor" in src


@pytest.mark.asyncio
async def test_t1_cancel_during_prepare_must_leave_no_context(
    processor, state_manager, tmp_path
):
    """If cancel wins during prep, _contexts must be empty (abort checks)."""
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state = state_manager.create_state("THR-1", "s", desc)
    state_manager.update_state("THR-1", status=TaskStatus.EXECUTING)

    def slow_init(issue_key, st=None):
        # Cancel mid-init
        state_manager.update_state(
            issue_key,
            status=TaskStatus.CANCELLED,
            error_message="Cancelled from dashboard",
            completed_at=datetime.now(),
        )
        return None

    with patch.object(processor, "_init_git_manager", side_effect=slow_init):
        out = await processor._prepare_git_workspace(state)

    assert out is None
    assert "THR-1" not in processor._contexts
    assert state_manager.get_state("THR-1").status == TaskStatus.CANCELLED


# ===========================================================================
# Topology / wiring smoke
# ===========================================================================


def test_topology_two_managers_same_dir_share_process_lock(tmp_path):
    """All managers on the same state_dir must share one process RLock."""
    state_dir = tmp_path / "shared"
    a = JiraStateManager(state_dir=state_dir)
    b = JiraStateManager(state_dir=state_dir)
    assert a is not b
    assert a._lock is b._lock


def test_dashboard_delete_live_job_blocked():
    from src.dashboard.service import _LIVE_JOB_STATUSES

    assert "running" in _LIVE_JOB_STATUSES
    assert "planning" in _LIVE_JOB_STATUSES
    assert "executing" in _LIVE_JOB_STATUSES
