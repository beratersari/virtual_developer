"""Scheduled jobs: store, create (Jira hard-fail), dispatch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.scheduler.service import (
    build_issue_description,
    cancel_scheduled_job,
    create_scheduled_job,
    dispatch_due_schedules,
    inflight_dispatch_ids,
    parse_schedule_at,
    wait_inflight_dispatches,
)
from src.state.manager import JiraStateManager
from src.state.schedule_store import SCHEDULE_LABEL, ScheduleStore


def test_build_issue_description_includes_params():
    text = build_issue_description(
        description="Do the thing",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="build",
    )
    assert "Do the thing" in text
    assert "{params}" in text
    assert "Repository: https://gitlab.com/a/b.git" in text
    assert "Source branch: feature/x" in text
    assert "Target branch: develop" in text
    assert "Mode: build" in text
    assert "Model:" not in text


def test_build_issue_description_includes_optional_model():
    text = build_issue_description(
        description="Do the thing",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="build",
        model="opencode/hy3-free",
    )
    assert "Model: opencode/hy3-free" in text


def test_parse_schedule_at_variants():
    assert parse_schedule_at("2026-08-03T15:00:00").year == 2026
    assert parse_schedule_at("2026-08-03 15:00:00").hour == 15
    with pytest.raises(ValueError):
        parse_schedule_at("")


def test_schedule_store_claim_and_due(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    a = store.create(
        title="past",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-1",
        issue_description="x",
    )
    store.create(
        title="future",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at=future,
        issue_key="KAN-2",
        issue_description="y",
    )
    due = store.list_due()
    assert len(due) == 1
    assert due[0]["schedule_id"] == a["schedule_id"]

    claimed = store.claim_due(a["schedule_id"])
    assert claimed is not None
    assert claimed["status"] == "dispatching"
    assert store.claim_due(a["schedule_id"]) is None


def test_list_due_zulu_future_is_not_due_against_local_now(tmp_path):
    """UTC Z must not be compared by labeling naive local now as UTC."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future_utc = datetime.now().astimezone() + timedelta(hours=2)
    scheduled_at = (
        future_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    rec = store.create(
        title="zulu-future",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=scheduled_at,
        issue_key="KAN-Z1",
        issue_description="x",
    )
    assert store.list_due() == []
    later = datetime.now() + timedelta(hours=3)
    due = store.list_due(now=later)
    assert len(due) == 1
    assert due[0]["schedule_id"] == rec["schedule_id"]


def test_list_due_naive_local_still_uses_wall_clock(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future_local = (datetime.now() + timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    store.create(
        title="naive-future",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future_local,
        issue_key="KAN-N1",
        issue_description="x",
    )
    assert store.list_due() == []


def test_create_scheduled_job_hard_fails_without_issue(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = None
    out = create_scheduled_job(
        title="T",
        description="D",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is False
    assert "Failed to create Jira issue" in out["error"]


def test_work_branch_for_issue_key():
    from src.scheduler.service import work_branch_for_issue_key

    assert work_branch_for_issue_key("KAN-42") == "feature/KAN-42"
    assert work_branch_for_issue_key("PROJ/1") == "feature/PROJ-1"


def test_create_scheduled_job_custom_source_branch(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-9"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Custom branch job",
        description="body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        source_branch_mode="custom",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["source_branch"] == "develop"
    assert out["schedule"]["source_branch"] == "develop"
    # Description on create already has develop
    desc = client.create_issue.call_args.kwargs.get("description") or ""
    assert "Source branch: develop" in desc
    client.update_issue.assert_not_called()


def test_create_scheduled_job_persists_model(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-91"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Model job",
        description="body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="main",
        mode="build",
        model="opencode/mimo-v2.5-free",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        source_branch_mode="custom",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["model"] == "opencode/mimo-v2.5-free"
    desc = client.create_issue.call_args.kwargs.get("description") or ""
    assert "Model: opencode/mimo-v2.5-free" in desc


def test_create_scheduled_job_source_from_issue_key(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-99"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Issue-key branch job",
        description="Implement feature",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="",  # ignored for issue_key mode
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        source_branch_mode="issue_key",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["issue_key"] == "KAN-99"
    assert out["source_branch"] == "feature/KAN-99"
    assert out["schedule"]["source_branch"] == "feature/KAN-99"
    assert "feature/KAN-99" in (out["schedule"].get("issue_description") or "")
    # After create, description rewritten with real key
    client.update_issue.assert_called_once()
    upd_fields = client.update_issue.call_args.kwargs.get("fields") or {}
    if not upd_fields and client.update_issue.call_args.args:
        # positional (issue_key, fields=...)
        pass
    # kwargs form: update_issue(issue_key, fields={...})
    call_kw = client.update_issue.call_args
    fields = call_kw.kwargs.get("fields")
    if fields is None and len(call_kw.args) >= 2:
        fields = call_kw.args[1]
    assert fields is not None
    assert "Source branch: feature/KAN-99" in fields.get("description", "")


def test_create_scheduled_job_issue_key_mode_requires_no_custom_source(tmp_path):
    """custom mode without source_branch fails; issue_key mode does not need it."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "X-1"}
    client.update_issue.return_value = True
    missing = create_scheduled_job(
        title="T",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="",
        target_branch="develop",
        mode="plan",
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
        project_key="X",
        source_branch_mode="custom",
        jira_client=client,
        store=store,
    )
    assert missing["ok"] is False
    assert "source_branch" in missing["error"]
    assert store.list_schedules() == []
    client.create_issue.assert_not_called()


def test_create_scheduled_job_soft_transition_and_saves(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-99", "id": "1"}
    client.transition_to_in_progress.side_effect = RuntimeError("jira down")

    out = create_scheduled_job(
        title="Title",
        description="Body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="plan",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["issue_key"] == "KAN-99"
    assert rec["mode"] == "plan"
    assert rec["status"] == "scheduled"
    kwargs = client.create_issue.call_args.kwargs
    assert SCHEDULE_LABEL in (kwargs.get("labels") or [])


@pytest.mark.asyncio
async def test_dispatch_due_schedules(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    rec = store.create(
        title="Go",
        description="d",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-10",
        issue_description=build_issue_description(
            description="d",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
        ),
    )
    processor = MagicMock()
    # Structured outcome (real JobProcessor shape)
    processor.process_event = AsyncMock(
        return_value={"ok": True, "work_started": True, "skipped": None}
    )

    result = await dispatch_due_schedules(
        processor=processor,
        store=store,
        jira_client=None,
    )
    assert result["launched"] == 1
    await wait_inflight_dispatches()
    processor.process_event.assert_awaited_once()
    event = processor.process_event.await_args.args[0]
    assert event["webhookEvent"] == "jira:issue_created"
    assert event["issue"]["key"] == "KAN-10"
    assert event["scheduled_job"] is True

    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "dispatched"


@pytest.mark.asyncio
async def test_dispatch_marks_error_when_process_event_noops(tmp_path):
    """Do not mark dispatched when processor reports no work started."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    rec = store.create(
        title="Skip",
        description="d",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-NOOP",
        issue_description=build_issue_description(
            description="d",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
        ),
    )
    processor = MagicMock()
    processor.process_event = AsyncMock(
        return_value={
            "ok": True,
            "work_started": False,
            "skipped": "already in progress (executing)",
        }
    )

    result = await dispatch_due_schedules(
        processor=processor,
        store=store,
        jira_client=None,
    )
    assert result["launched"] == 1
    await wait_inflight_dispatches()
    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "error"
    assert "already in progress" in (refreshed.get("error_message") or "")


@pytest.mark.asyncio
async def test_dispatch_does_not_block_other_due_schedules(tmp_path):
    """One long process_event must not delay claiming other already-due jobs."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    earlier = (datetime.now() - timedelta(minutes=2)).isoformat(timespec="seconds")
    later = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rec_a = store.create(
        title="slow",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=earlier,
        issue_key="KAN-SLOW",
        issue_description="a",
    )
    rec_b = store.create(
        title="fast",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=later,
        issue_key="KAN-FAST",
        issue_description="b",
    )

    release_slow = asyncio.Event()
    started: list[str] = []

    async def process_event(event):
        key = event["issue"]["key"]
        started.append(key)
        if key == "KAN-SLOW":
            await release_slow.wait()
        return {"ok": True, "work_started": True, "skipped": None}

    processor = MagicMock()
    processor.process_event = process_event

    result = await dispatch_due_schedules(
        processor=processor, store=store, jira_client=None
    )
    assert result["launched"] == 2
    # Let both tasks reach process_event (slow waits on the event).
    for _ in range(50):
        if len(started) >= 2:
            break
        await asyncio.sleep(0.01)
    assert started == ["KAN-SLOW", "KAN-FAST"] or set(started) == {
        "KAN-SLOW",
        "KAN-FAST",
    }
    assert rec_a["schedule_id"] in inflight_dispatch_ids()
    assert store.get(rec_a["schedule_id"])["status"] == "dispatching"
    # Fast job must be allowed to finish while slow is still running.
    for _ in range(50):
        if store.get(rec_b["schedule_id"])["status"] == "dispatched":
            break
        await asyncio.sleep(0.01)
    assert store.get(rec_b["schedule_id"])["status"] == "dispatched"
    assert store.get(rec_a["schedule_id"])["status"] == "dispatching"

    release_slow.set()
    await wait_inflight_dispatches()
    assert store.get(rec_a["schedule_id"])["status"] == "dispatched"


@pytest.mark.asyncio
async def test_next_dispatch_tick_while_previous_still_running(tmp_path):
    """Daemon 15s tick must pick up newly due jobs while another is dispatching."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    rec_a = store.create(
        title="first",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-A",
        issue_description="a",
    )
    rec_c = store.create(
        title="later",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-C",
        issue_description="c",
    )

    release_a = asyncio.Event()
    seen: list[str] = []

    async def process_event(event):
        key = event["issue"]["key"]
        seen.append(key)
        if key == "KAN-A":
            await release_a.wait()
        return {"ok": True, "work_started": True, "skipped": None}

    processor = MagicMock()
    processor.process_event = process_event

    r1 = await dispatch_due_schedules(
        processor=processor, store=store, jira_client=None
    )
    assert r1["launched"] == 1
    for _ in range(50):
        if "KAN-A" in seen:
            break
        await asyncio.sleep(0.01)
    assert "KAN-A" in seen

    store.update(
        rec_c["schedule_id"],
        scheduled_at=(datetime.now() - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        ),
    )
    r2 = await dispatch_due_schedules(
        processor=processor, store=store, jira_client=None
    )
    assert r2["launched"] == 1
    for _ in range(50):
        if "KAN-C" in seen:
            break
        await asyncio.sleep(0.01)
    assert "KAN-C" in seen
    assert rec_a["schedule_id"] in inflight_dispatch_ids()

    release_a.set()
    await wait_inflight_dispatches()
    assert store.get(rec_a["schedule_id"])["status"] == "dispatched"
    assert store.get(rec_c["schedule_id"])["status"] == "dispatched"


@pytest.mark.asyncio
async def test_recover_skips_inflight_dispatching_rows(tmp_path):
    """Age recovery must not reset a schedule whose worker is still running."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    rec = store.create(
        title="live",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-LIVE-REC",
        issue_description="x",
    )
    gate = asyncio.Event()

    async def process_event(event):
        await gate.wait()
        return {"ok": True, "work_started": True, "skipped": None}

    processor = MagicMock()
    processor.process_event = process_event

    await dispatch_due_schedules(processor=processor, store=store, jira_client=None)
    sid = rec["schedule_id"]
    assert sid in inflight_dispatch_ids()
    assert store.get(sid)["status"] == "dispatching"

    from src.scheduler.service import recover_stuck_schedules

    n = recover_stuck_schedules(
        store=store,
        max_age_seconds=0.0,
        exclude_ids=inflight_dispatch_ids(),
    )
    assert n == 0
    assert store.get(sid)["status"] == "dispatching"

    gate.set()
    await wait_inflight_dispatches()
    assert store.get(sid)["status"] == "dispatched"


def test_cancel_scheduled_job(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="c",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2026-12-01T00:00:00",
        issue_key="KAN-3",
        issue_description="x",
    )
    out = cancel_scheduled_job(rec["schedule_id"], store=store)
    assert out["ok"] is True
    assert store.get(rec["schedule_id"])["status"] == "cancelled"


def test_recover_stuck_dispatching_reopens_for_list_due(tmp_path):
    """Crash after claim left status=dispatching — must not stay black-holed."""
    from datetime import datetime

    from src.scheduler.service import recover_stuck_schedules

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="stuck",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="plan",
        scheduled_at="2000-01-01T00:00:00",
        issue_key="KAN-STUCK",
        issue_description="x",
    )
    claimed = store.claim_due(rec["schedule_id"])
    assert claimed["status"] == "dispatching"
    assert store.list_due(now=datetime(2026, 1, 1)) == []

    n = recover_stuck_schedules(store=store, max_age_seconds=0.0)
    assert n == 1
    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "scheduled"
    due = store.list_due(now=datetime(2026, 1, 1))
    assert len(due) == 1
    assert due[0]["schedule_id"] == rec["schedule_id"]


def test_cancel_dispatching_schedule_allowed(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="c",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2026-12-01T00:00:00",
        issue_key="KAN-DISP",
        issue_description="x",
    )
    store.claim_due(rec["schedule_id"])
    out = cancel_scheduled_job(rec["schedule_id"], store=store)
    assert out["ok"] is True
    assert store.get(rec["schedule_id"])["status"] == "cancelled"


def test_preview_existing_issue_valid_template():
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-20",
        "fields": {
            "summary": "Existing feature",
            "description": (
                "Do work\n\n"
                "{params}\n"
                "Repository: https://gitlab.com/a/b.git\n"
                "Source branch: develop\n"
                "Target branch: main\n"
                "Mode: build\n"
                "{params}"
            ),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "labels": [],
        },
    }
    from src.scheduler.service import preview_existing_issue

    out = preview_existing_issue("kan-20", jira_client=client)
    assert out["ok"] is True
    assert out["template_valid"] is True
    assert out["mode"] == "build"
    assert out["repository_url"].endswith("b.git")
    assert out["issue_type"] == "Story"


def test_preview_existing_issue_invalid_template():
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-21",
        "fields": {
            "summary": "No params",
            "description": "just text",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
        },
    }
    from src.scheduler.service import preview_existing_issue

    out = preview_existing_issue("KAN-21", jira_client=client)
    assert out["ok"] is False
    assert "params" in out["error"].lower() or "template" in out["error"].lower() or "could not" in out["error"].lower()


def test_schedule_existing_issue(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-22",
        "fields": {
            "summary": "Existing",
            "description": (
                "{params}\n"
                "Repository: https://gitlab.com/a/b.git\n"
                "Source branch: develop\n"
                "Target branch: develop\n"
                "Mode: plan\n"
                "{params}"
            ),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": ["ai-assist"],
        },
    }
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True

    from src.scheduler.service import schedule_existing_issue

    out = schedule_existing_issue(
        "KAN-22",
        scheduled_at="2026-12-01T10:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["source"] == "existing"
    assert rec["issue_key"] == "KAN-22"
    assert rec["mode"] == "plan"
    client.create_issue.assert_not_called()
    client.transition_to_in_progress.assert_called()
    client.add_labels.assert_called()

    # Duplicate pending rejected
    out2 = schedule_existing_issue(
        "KAN-22",
        scheduled_at="2026-12-02T10:00:00",
        jira_client=client,
        store=store,
    )
    assert out2["ok"] is False
    assert "already has a pending schedule" in out2["error"]


def test_create_with_custom_issue_type(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-77"}
    client.transition_to_in_progress.return_value = True

    out = create_scheduled_job(
        title="Bugfix",
        description="x",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        issue_type="ExtBug",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["issue_type"] == "ExtBug"
    assert client.create_issue.call_args.kwargs["issue_type"] == "ExtBug"


def test_api_schedules(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")

    client_mock = MagicMock()
    client_mock.create_issue.return_value = {"key": "KAN-55"}
    client_mock.transition_to_in_progress.return_value = True
    client_mock.get_project_issue_types.return_value = [
        {"id": "1", "name": "Task", "subtask": False},
        {"id": "2", "name": "Story", "subtask": False},
        {"id": "3", "name": "ExtBug", "subtask": False},
        {"id": "4", "name": "Sub-task", "subtask": True},
    ]

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.schedule_store", store)
        m.setattr(
            "src.scheduler.service.create_jira_client",
            lambda: client_mock,
            raising=False,
        )
        # create_scheduled_job imports create_jira_client inside when client None —
        # pass via patching create_scheduled_job path by injecting client through
        # the module used by API (it calls create_scheduled_job without client).
        m.setattr(
            "src.dashboard.api.create_scheduled_job",
            lambda **kwargs: create_scheduled_job(
                **kwargs, jira_client=client_mock, store=store
            ),
        )
        m.setattr(
            "src.dashboard.api.list_project_issue_types",
            lambda **kwargs: {
                "ok": True,
                "project_key": "KAN",
                "issue_types": [
                    {"id": "1", "name": "Task", "subtask": False},
                    {"id": "3", "name": "ExtBug", "subtask": False},
                ],
            },
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)

        r_types = tc.get("/api/jira/issue-types")
        assert r_types.status_code == 200
        assert any(t["name"] == "ExtBug" for t in r_types.json()["issue_types"])

        r_list = tc.get("/api/schedules")
        assert r_list.status_code == 200
        assert r_list.json()["total"] == 0

        r = tc.post(
            "/api/schedules",
            json={
                "title": "From API",
                "description": "desc",
                "repository_url": "https://gitlab.com/org/repo.git",
                "source_branch": "develop",
                "target_branch": "main",
                "mode": "build",
                "issue_type": "ExtBug",
                "scheduled_at": "2026-12-25T12:00:00",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        sid = body["schedule"]["schedule_id"]
        assert body["issue_key"] == "KAN-55"
        assert body["schedule"]["issue_type"] == "ExtBug"

        r2 = tc.get("/api/schedules")
        assert r2.json()["total"] == 1

        r3 = tc.post(f"/api/schedules/{sid}/cancel")
        assert r3.status_code == 200
        assert r3.json()["schedule"]["status"] == "cancelled"

        # Preview + schedule existing
        client_mock.get_issue.return_value = {
            "key": "KAN-88",
            "fields": {
                "summary": "From board",
                "description": (
                    "{params}\n"
                    "Repository: https://gitlab.com/org/repo.git\n"
                    "Source branch: develop\n"
                    "Target branch: develop\n"
                    "Mode: build\n"
                    "{params}"
                ),
                "status": {"name": "To Do"},
                "issuetype": {"name": "ExtBug"},
                "labels": [],
            },
        }
        m.setattr(
            "src.dashboard.api.preview_existing_issue",
            lambda issue_key: __import__(
                "src.scheduler.service", fromlist=["preview_existing_issue"]
            ).preview_existing_issue(issue_key, jira_client=client_mock),
        )
        m.setattr(
            "src.dashboard.api.schedule_existing_issue",
            lambda issue_key, scheduled_at, store=None, **kw: __import__(
                "src.scheduler.service", fromlist=["schedule_existing_issue"]
            ).schedule_existing_issue(
                issue_key,
                scheduled_at=scheduled_at,
                jira_client=client_mock,
                store=store,
                **kw,
            ),
        )
        r_prev = tc.get("/api/schedules/preview", params={"issue_key": "KAN-88"})
        assert r_prev.status_code == 200, r_prev.text
        assert r_prev.json()["template_valid"] is True

        r_ex = tc.post(
            "/api/schedules/from-issue",
            json={"issue_key": "KAN-88", "scheduled_at": "2026-12-26T08:00:00"},
        )
        assert r_ex.status_code == 200, r_ex.text
        assert r_ex.json()["schedule"]["source"] == "existing"


def test_claim_for_dispatch_ignores_due_time_and_retries_error(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
    rec = store.create(
        title="later",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-NOW",
        issue_description="x",
    )
    assert store.list_due() == []
    claimed = store.claim_for_dispatch(rec["schedule_id"])
    assert claimed is not None
    assert claimed["status"] == "dispatching"
    assert store.claim_for_dispatch(rec["schedule_id"]) is None

    err = store.create(
        title="failed",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-ERR",
        issue_description="x",
    )
    store.update(err["schedule_id"], status="error", error_message="boom")
    retried = store.claim_for_dispatch(err["schedule_id"])
    assert retried["status"] == "dispatching"
    assert retried.get("error_message") is None


@pytest.mark.asyncio
async def test_dispatch_schedule_now_fires_future_job(tmp_path):
    from src.scheduler.service import dispatch_schedule_now

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=4)).isoformat(timespec="seconds")
    rec = store.create(
        title="soon",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-INSTANT",
        issue_description="x",
    )
    processor = MagicMock()
    processor.process_event = AsyncMock(
        return_value={"ok": True, "work_started": True, "skipped": None}
    )
    out = dispatch_schedule_now(
        rec["schedule_id"], processor=processor, store=store, jira_client=None
    )
    assert out["ok"] is True
    assert out["schedule"]["status"] == "dispatching"
    await wait_inflight_dispatches()
    processor.process_event.assert_awaited_once()
    event = processor.process_event.await_args.args[0]
    assert event["issue"]["key"] == "KAN-INSTANT"
    assert event["scheduled_job"] is True
    assert store.get(rec["schedule_id"])["status"] == "dispatched"


def test_dispatch_schedule_now_refuses_cancelled(tmp_path):
    from src.scheduler.service import dispatch_schedule_now

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="no",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2026-12-01T00:00:00",
        issue_key="KAN-NO",
        issue_description="x",
    )
    cancel_scheduled_job(rec["schedule_id"], store=store)
    out = dispatch_schedule_now(
        rec["schedule_id"],
        processor=MagicMock(),
        store=store,
    )
    assert out["ok"] is False
    assert "cancelled" in (out.get("error") or "")


def test_api_dispatch_now(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    future = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    rec = store.create(
        title="api-now",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="KAN-API-NOW",
        issue_description="x",
    )
    processor = MagicMock()
    processor.process_event = AsyncMock(
        return_value={"ok": True, "work_started": True, "skipped": None}
    )
    monkeypatch.setattr("src.dashboard.api.schedule_store", store)
    app = create_dashboard_app(processor=processor, state_manager=sm)
    tc = TestClient(app)
    r = tc.post(f"/api/schedules/{rec['schedule_id']}/dispatch")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["schedule"]["status"] == "dispatching"
    r_bad = tc.post(f"/api/schedules/{rec['schedule_id']}/dispatch")
    assert r_bad.status_code == 409
    r_miss = tc.post("/api/schedules/sched_missing/dispatch")
    assert r_miss.status_code == 404


def test_api_from_issue_run_now_stamps_scheduled_at_now(tmp_path, monkeypatch):
    """Run now must not keep the form default (now + 5 minutes)."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    picker = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")

    def _schedule(issue_key, scheduled_at, store=None, **_kw):
        rec = (store or ScheduleStore(schedules_dir=tmp_path / "schedules")).create(
            title="run-now",
            description="",
            repository_url="https://example.com/r.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
            scheduled_at=scheduled_at,
            issue_key=issue_key,
            issue_description="x",
        )
        return {"ok": True, "schedule": rec, "issue_key": issue_key}

    processor = MagicMock()
    processor.process_event = AsyncMock(
        return_value={"ok": True, "work_started": True, "skipped": None}
    )
    monkeypatch.setattr("src.dashboard.api.schedule_store", store)
    monkeypatch.setattr("src.dashboard.api.schedule_existing_issue", _schedule)
    app = create_dashboard_app(processor=processor, state_manager=sm)
    tc = TestClient(app)
    before = datetime.now()
    r = tc.post(
        "/api/schedules/from-issue",
        json={
            "issue_key": "KAN-RUNNOW",
            "scheduled_at": picker,
            "dispatch_now": True,
        },
    )
    after = datetime.now()
    assert r.status_code == 200, r.text
    got = parse_schedule_at(r.json()["schedule"]["scheduled_at"])
    if got.tzinfo is not None:
        got = got.replace(tzinfo=None)
    assert got >= before.replace(microsecond=0) - timedelta(seconds=2)
    assert got <= after + timedelta(seconds=2)
    assert abs((got - parse_schedule_at(picker)).total_seconds()) >= 60
    assert r.json()["dispatched"] is True
