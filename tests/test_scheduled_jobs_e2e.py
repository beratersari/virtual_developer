"""E2E / condition matrix for scheduled jobs (create, existing, dispatch, Jira soft-fail).

Covers hard-fail vs soft-fail rules:
  * Hard-fail: Jira issue create fails; existing issue missing; invalid template
  * Soft-fail: In Progress transition, labels, comments, re-fetch at dispatch
  * Local UI/state still advances when Jira comments/transitions are down
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.reporter.jira_reporter import JiraReporter
from src.scheduler.service import (
    build_issue_description,
    cancel_scheduled_job,
    create_scheduled_job,
    dispatch_due_schedules,
    preview_existing_issue,
    schedule_existing_issue,
)
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import SCHEDULE_LABEL, ScheduleStore


def _valid_params(
    *,
    repo: str = "https://gitlab.com/org/app.git",
    source: str = "develop",
    target: str = "develop",
    mode: str = "build",
) -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        "{params}"
    )


def _issue_payload(
    key: str = "KAN-100",
    *,
    summary: str = "Feature",
    description: str | None = None,
    status: str = "To Do",
    itype: str = "Task",
    labels: list | None = None,
) -> dict:
    return {
        "key": key,
        "id": "1",
        "fields": {
            "summary": summary,
            "description": description if description is not None else _valid_params(),
            "status": {"name": status},
            "issuetype": {"name": itype},
            "labels": labels if labels is not None else [],
            "assignee": None,
        },
    }


# ---------------------------------------------------------------------------
# Create-new path
# ---------------------------------------------------------------------------


def test_e2e_create_hard_fail_jira_create_down_no_schedule(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = None
    client.last_error = "Connection refused"

    out = create_scheduled_job(
        title="T",
        description="d",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is False
    assert "Schedule was not saved" in out["error"]
    assert "Connection refused" in out["error"]
    assert store.list_schedules() == []
    client.transition_to_in_progress.assert_not_called()


def test_e2e_create_soft_fail_transition_still_saves(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-SOFT"}
    client.transition_to_in_progress.side_effect = RuntimeError("Jira 503")

    out = create_scheduled_job(
        title="Soft transition",
        description="body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["status"] == "scheduled"
    assert out["schedule"]["issue_key"] == "KAN-SOFT"
    assert store.get(out["schedule"]["schedule_id"]) is not None


def test_e2e_create_validation_errors_no_jira_call(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    cases = [
        dict(
            title="",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="a",
            target_branch="b",
            mode="build",
            scheduled_at="2026-01-01T00:00:00",
        ),
        dict(
            title="t",
            repository_url="",
            source_branch="a",
            target_branch="b",
            mode="build",
            scheduled_at="2026-01-01T00:00:00",
        ),
        dict(
            title="t",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="",
            target_branch="b",
            mode="build",
            scheduled_at="2026-01-01T00:00:00",
        ),
        dict(
            title="t",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="a",
            target_branch="b",
            mode="nope",
            scheduled_at="2026-01-01T00:00:00",
        ),
        dict(
            title="t",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="a",
            target_branch="b",
            mode="build",
            scheduled_at="not-a-date",
        ),
    ]
    for kwargs in cases:
        out = create_scheduled_job(
            description="",
            project_key="KAN",
            jira_client=client,
            store=store,
            **kwargs,
        )
        assert out["ok"] is False, kwargs
    client.create_issue.assert_not_called()
    assert store.list_schedules() == []


# ---------------------------------------------------------------------------
# Existing-issue path
# ---------------------------------------------------------------------------


def test_e2e_preview_jira_unavailable():
    client = MagicMock()
    client.get_issue.return_value = None
    client.last_error = "timeout"

    out = preview_existing_issue("KAN-404", jira_client=client)
    assert out["ok"] is False
    assert "Could not load issue" in out["error"]
    assert "KAN-404" in out["error"]


def test_e2e_preview_missing_mode_hard_fail():
    client = MagicMock()
    client.get_issue.return_value = _issue_payload(
        "KAN-NOMODE",
        description=(
            "{params}\n"
            "Repository: https://gitlab.com/a/b.git\n"
            "Source branch: develop\n"
            "Target branch: develop\n"
            "{params}"
        ),
    )
    out = preview_existing_issue("KAN-NOMODE", jira_client=client)
    assert out["ok"] is False
    assert out.get("template_valid") is False


def test_e2e_schedule_existing_soft_fail_transition_and_labels(tmp_path):
    """Transition + labels throw; schedule record must still be saved."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue_payload("KAN-SOFT2")
    client.transition_to_in_progress.side_effect = RuntimeError("transition down")
    client.add_labels.side_effect = RuntimeError("labels down")

    out = schedule_existing_issue(
        "KAN-SOFT2",
        scheduled_at="2026-12-01T12:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["source"] == "existing"
    assert out["schedule"]["status"] == "scheduled"
    assert store.get(out["schedule"]["schedule_id"])["issue_key"] == "KAN-SOFT2"


def test_e2e_schedule_existing_hard_fail_invalid_template_no_record(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue_payload(
        "KAN-BAD", description="no params here"
    )
    out = schedule_existing_issue(
        "KAN-BAD",
        scheduled_at="2026-12-01T12:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is False
    assert store.list_schedules() == []
    client.transition_to_in_progress.assert_not_called()


def test_e2e_cancel_dispatched_refused(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="",
        repository_url="https://x/y.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2026-01-01T00:00:00",
        issue_key="KAN-C",
        issue_description="x",
    )
    store.update(rec["schedule_id"], status="dispatched")
    out = cancel_scheduled_job(rec["schedule_id"], store=store)
    assert out["ok"] is False
    assert "Cannot cancel" in out["error"]


# ---------------------------------------------------------------------------
# Dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_dispatch_uses_local_snapshot_when_jira_get_fails(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    desc = build_issue_description(
        description="local only",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
    )
    rec = store.create(
        title="Local snap",
        description="local only",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-LOCAL",
        issue_description=desc,
        source="existing",
    )
    client = MagicMock()
    client.get_issue.side_effect = RuntimeError("Jira offline at dispatch")

    processor = MagicMock()
    processor.process_event = AsyncMock(
        return_value={"ok": True, "work_started": True, "skipped": None}
    )

    result = await dispatch_due_schedules(
        processor=processor,
        store=store,
        jira_client=client,
    )
    assert result["started"] == 1
    processor.process_event.assert_awaited_once()
    event = processor.process_event.await_args.args[0]
    assert event["issue"]["key"] == "KAN-LOCAL"
    assert "local only" in (event["issue"]["fields"]["description"] or "")
    assert store.get(rec["schedule_id"])["status"] == "dispatched"


@pytest.mark.asyncio
async def test_e2e_dispatch_plan_ready_scheduled_job_starts_execution(
    tmp_path, monkeypatch
):
    """CRITICAL #6: schedule fire on plan_ready must start work, not false-dispatch."""
    monkeypatch.chdir(tmp_path)
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-PR", "plan done", _valid_params(mode="build"))
    sm.update_state("KAN-PR", status=TaskStatus.PLAN_READY)

    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rec = store.create(
        title="later",
        description="",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-PR",
        issue_description=build_issue_description(
            description="x",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
        ),
        source="existing",
    )

    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client"):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc._start_execution_workflow = AsyncMock()
    proc._start_planning_workflow = AsyncMock()
    proc._mark_jira_in_progress = MagicMock()

    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=None
    )

    assert result["started"] == 1
    assert result["failed"] == 0
    assert store.get(rec["schedule_id"])["status"] == "dispatched"
    proc._start_execution_workflow.assert_awaited_once()
    proc._start_planning_workflow.assert_not_awaited()
    # Workflow begin would set EXECUTING; we mocked it, so still plan_ready
    # unless _begin_workflow_run ran — mock means status may stay plan_ready,
    # but the execution entrypoint was invoked (the real success signal).
    assert proc._start_execution_workflow.await_count == 1


@pytest.mark.asyncio
async def test_e2e_dispatch_in_flight_is_not_false_success(tmp_path, monkeypatch):
    """In-flight issue: schedule must not report dispatched without starting."""
    monkeypatch.chdir(tmp_path)
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-LIVE", "busy", _valid_params())
    sm.update_state("KAN-LIVE", status=TaskStatus.EXECUTING)

    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rec = store.create(
        title="busy",
        description="",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-LIVE",
        issue_description=_valid_params(),
        source="existing",
    )

    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client"):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc._start_execution_workflow = AsyncMock()
    proc._start_planning_workflow = AsyncMock()

    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=None
    )

    assert result["started"] == 0
    assert result["failed"] == 1
    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "error"
    assert "in progress" in (refreshed.get("error_message") or "").lower()
    proc._start_execution_workflow.assert_not_awaited()
    assert sm.get_state("KAN-LIVE").status == TaskStatus.EXECUTING


@pytest.mark.asyncio
async def test_e2e_dispatch_process_event_crash_marks_schedule_error(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rec = store.create(
        title="Boom",
        description="",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-BOOM",
        issue_description=_valid_params(),
    )
    processor = MagicMock()
    processor.process_event = AsyncMock(side_effect=RuntimeError("workflow exploded"))

    result = await dispatch_due_schedules(
        processor=processor,
        store=store,
        jira_client=None,
    )
    assert result["failed"] == 1
    assert result["started"] == 0
    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "error"
    assert "workflow exploded" in (refreshed.get("error_message") or "")


@pytest.mark.asyncio
async def test_e2e_dispatch_skips_future_and_cancelled(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
    past = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    store.create(
        title="future",
        description="",
        repository_url="https://x/y.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-F",
        issue_description="x",
    )
    cancelled = store.create(
        title="cancelled",
        description="",
        repository_url="https://x/y.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-X",
        issue_description="x",
    )
    store.update(cancelled["schedule_id"], status="cancelled")

    processor = MagicMock()
    processor.process_event = AsyncMock()
    result = await dispatch_due_schedules(
        processor=processor, store=store, jira_client=None
    )
    assert result["due"] == 0
    assert result["started"] == 0
    processor.process_event.assert_not_called()


# ---------------------------------------------------------------------------
# Full pipeline: schedule → dispatch → process_event with Jira comments DOWN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_full_pipeline_comments_fail_local_state_still_updates(
    tmp_path, monkeypatch
):
    """Jira add_comment always fails; schedule still dispatches and local state exists.

    Mocks agent workflows so we do not run OpenCode; focuses on Jira soft-fail
    during process_event (acknowledgment / errors) after schedule fire.
    """
    monkeypatch.chdir(tmp_path)
    schedules_dir = tmp_path / "schedules"
    state_dir = tmp_path / "state"
    jobs_dir = tmp_path / "jobs"
    store = ScheduleStore(schedules_dir=schedules_dir)
    sm = JiraStateManager(state_dir=state_dir)
    js = JobStore(jobs_dir=jobs_dir)

    # Flaky Jira: create works; comments/transitions fail
    jira = MagicMock()
    jira.create_issue.return_value = {"key": "KAN-E2E", "id": "99"}
    jira.transition_to_in_progress.side_effect = RuntimeError("transition 503")
    jira.add_comment.side_effect = RuntimeError("comments unavailable")
    jira.get_issue.side_effect = RuntimeError("get down after create")
    jira.add_labels.side_effect = RuntimeError("labels down")
    jira.is_cloud = True

    # 1) Create schedule (soft transition fail OK)
    created = create_scheduled_job(
        title="E2E scheduled",
        description="Run the feature",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        jira_client=jira,
        store=store,
    )
    assert created["ok"] is True
    sid = created["schedule"]["schedule_id"]
    assert created["schedule"]["status"] == "scheduled"

    # 2) Real JobProcessor with reporter wired to flaky Jira
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = js
    proc.jira_client = jira
    # Reporter that talks to flaky client (comments raise → soft return None)
    proc.reporter = JiraReporter(client=jira)

    # Skip heavy agent workflows; still exercise soft-fail comments
    async def _fake_exec(state):
        # Acknowledgment already soft-failed above; try completion + progress
        st = sm.get_state(state.issue_key)
        proc.reporter.post_completion(st, summary="done locally")
        proc.reporter.post_error(st, "optional noise")  # soft-fail ok
        sm.update_state(
            state.issue_key,
            status=TaskStatus.COMPLETED,
            progress_percentage=100,
            completed_at=datetime.now(),
            error_message=None,
        )

    proc._start_execution_workflow = _fake_exec  # type: ignore[method-assign]
    proc._start_planning_workflow = AsyncMock()  # type: ignore[method-assign]

    # 3) Dispatch due schedules → process_event
    result = await dispatch_due_schedules(
        processor=proc,
        store=store,
        jira_client=jira,
    )
    assert result["started"] == 1, result
    assert store.get(sid)["status"] == "dispatched"

    # 4) Local state exists and is completed despite Jira comment failures
    state = sm.get_state("KAN-E2E")
    assert state is not None
    assert state.status == TaskStatus.COMPLETED
    assert state.issue_summary == "E2E scheduled"

    # Comments were attempted and failed (soft)
    assert jira.add_comment.called
    # create happened once; no create on dispatch
    assert jira.create_issue.call_count == 1


def test_e2e_reporter_comment_soft_fail_does_not_raise():
    """Unit: JiraReporter never raises when add_comment fails."""
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("Jira comments down")
    reporter = JiraReporter(client=client)

    sm = JiraStateManager(state_dir=MagicMock())  # won't use if we pass fake state
    # Build a minimal state-like object
    state = MagicMock()
    state.issue_key = "KAN-1"
    state.issue_summary = "s"
    state.metadata = {"workflow_type": "execution"}
    state.status = TaskStatus.ERROR
    state.timed_out = False
    state.max_retries = 0
    state.retry_count = 0
    state.timeout_seconds = 60
    state.current_opencode_session_id = None
    state.plan_path = None

    assert reporter.post_initial_acknowledgment(state) is None
    assert reporter.post_error(state, "boom") is None
    assert reporter.post_comment_response("KAN-1", "hello") is None
    # add_comment may return None instead of raising
    client.add_comment.side_effect = None
    client.add_comment.return_value = None
    assert reporter.post_comment_response("KAN-1", "hello") is None


def test_e2e_api_matrix_hard_and_soft(tmp_path, monkeypatch):
    """HTTP surface: create fail, preview fail, from-issue soft, cancel."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    client = MagicMock()

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.schedule_store", store)

        def _create(**kwargs):
            return create_scheduled_job(**kwargs, jira_client=client, store=store)

        def _preview(issue_key):
            return preview_existing_issue(issue_key, jira_client=client)

        def _from_issue(issue_key, scheduled_at, store=None):
            return schedule_existing_issue(
                issue_key,
                scheduled_at=scheduled_at,
                jira_client=client,
                store=store or store,
            )

        m.setattr("src.dashboard.api.create_scheduled_job", _create)
        m.setattr("src.dashboard.api.preview_existing_issue", _preview)
        m.setattr("src.dashboard.api.schedule_existing_issue", _from_issue)
        m.setattr(
            "src.dashboard.api.list_project_issue_types",
            lambda **kw: {"ok": True, "project_key": "KAN", "issue_types": []},
        )

        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)

        # Hard fail create
        client.create_issue.return_value = None
        client.last_error = "issuetype: invalid"
        r = tc.post(
            "/api/schedules",
            json={
                "title": "x",
                "repository_url": "https://gitlab.com/a/b.git",
                "source_branch": "develop",
                "target_branch": "develop",
                "mode": "build",
                "scheduled_at": "2026-12-01T00:00:00",
            },
        )
        assert r.status_code == 400
        assert store.list_schedules() == []

        # Preview missing issue
        client.get_issue.return_value = None
        r2 = tc.get("/api/schedules/preview", params={"issue_key": "KAN-NONE"})
        assert r2.status_code == 400

        # Soft: schedule existing despite transition/label failure
        client.get_issue.return_value = _issue_payload("KAN-OK")
        client.transition_to_in_progress.side_effect = RuntimeError("down")
        client.add_labels.side_effect = RuntimeError("down")
        r3 = tc.post(
            "/api/schedules/from-issue",
            json={"issue_key": "KAN-OK", "scheduled_at": "2026-12-01T00:00:00"},
        )
        assert r3.status_code == 200, r3.text
        body = r3.json()
        assert body["schedule"]["source"] == "existing"
        assert body["schedule"]["status"] == "scheduled"

        # Cancel works
        sid = body["schedule"]["schedule_id"]
        r4 = tc.post(f"/api/schedules/{sid}/cancel")
        assert r4.status_code == 200
        assert r4.json()["schedule"]["status"] == "cancelled"


def test_e2e_description_adf_and_plain_helpers():
    from src.scheduler.service import _description_to_text, _plain_template_error

    assert _description_to_text(None) == ""
    assert _description_to_text("plain") == "plain"
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "hello ADF"}],
            }
        ],
    }
    assert "hello ADF" in _description_to_text(adf)
    msg = _plain_template_error(
        "*Virtual Developer* could not start: no ``{params}`` block found.\n\n"
        "{code}\nhelp\n{code}"
    )
    assert "could not start" in msg.lower() or "params" in msg.lower()
