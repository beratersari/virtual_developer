"""Proof tests for critical state-machine / intake races (review 2026-08-13)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch, isolate_jira_agent_artifacts):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    return proc


@pytest.mark.asyncio
async def test_late_complete_cannot_overwrite_cancelled(processor, state_manager):
    """CRITICAL: cancel CAS must win over late success COMPLETED."""
    state_manager.create_state("CAS-X1", "s", "d")
    state_manager.update_state(
        "CAS-X1",
        status=TaskStatus.EXECUTING,
        current_task_id="t1",
        started_at=datetime.now(),
    )
    out = await processor.cancel_job("CAS-X1", reason="operator cancel")
    assert out["ok"] is True
    assert state_manager.get_state("CAS-X1").status == TaskStatus.CANCELLED

    updated = state_manager.update_state_if(
        "CAS-X1",
        expected_statuses={TaskStatus.EXECUTING},
        reject_statuses={TaskStatus.ERROR, TaskStatus.CANCELLED},
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        progress_percentage=100,
    )
    assert updated is None
    assert state_manager.get_state("CAS-X1").status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_late_fail_cannot_overwrite_completed(processor, state_manager, fake_jira):
    """CRITICAL: watchdog/fail must not clobber COMPLETED."""
    state_manager.create_state("CAS-X2", "s", "d")
    state_manager.update_state(
        "CAS-X2",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={"requeue_eligible": True},
    )
    processor._fail_issue("CAS-X2", "late stuck watchdog")
    assert state_manager.get_state("CAS-X2").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_concurrent_process_event_does_not_double_start_agent(
    processor, state_manager
):
    """CRITICAL: two events must not run two agents concurrently for one issue.

    While the first holds the issue lock and is EXECUTING, a second event waits
    on the lock and must not start another agent. After the first finishes
    EXECUTING (still in-flight until it exits), the second sees in-flight or
    skips rework depending on Jira status.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    concurrent = {"max": 0, "live": 0}
    calls = {"n": 0}

    async def slow_exec(state):
        calls["n"] += 1
        concurrent["live"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["live"])
        state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
            current_task_id=f"task-{calls['n']}",
        )
        processor._contexts[state.issue_key] = {
            "git": MagicMock(),
            "runner": MagicMock(),
        }
        started.set()
        await release.wait()
        concurrent["live"] -= 1
        # Leave EXECUTING so a waiter that runs after unlock still sees in-flight
        # (proves no second agent while first is still notionally live).
        # Real code would complete under CAS; here we only care about overlap.

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )
    create_event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "DBL-1",
            "fields": {
                "summary": "s",
                "description": desc,
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["bot"],
            },
        },
    }
    # Production second poll uses update (dispatch_as_update) once state exists
    update_event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "DBL-1",
            "fields": {
                "summary": "s",
                "description": desc,
                "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
                "labels": ["bot"],
            },
        },
    }

    with patch.object(processor, "_start_execution_workflow", side_effect=slow_exec):
        with patch.object(
            processor,
            "_resolve_workflow",
            return_value=__import__(
                "src.orchestrator.workflow_router", fromlist=["WorkflowType"]
            ).WorkflowType.EXECUTION,
        ):
            t1 = asyncio.create_task(processor.process_event(create_event))
            await asyncio.wait_for(started.wait(), timeout=3)
            t2 = asyncio.create_task(processor.process_event(update_event))
            await asyncio.sleep(0.05)
            assert concurrent["max"] == 1
            assert calls["n"] == 1
            release.set()
            o1 = await t1
            o2 = await t2

    assert concurrent["max"] == 1, "two agents must never overlap"
    assert calls["n"] == 1, f"second update while EXECUTING must not start agent; got {calls['n']}"
    assert o1.get("work_started") is True
    assert o2.get("work_started") is False
    assert state_manager.get_state("DBL-1").status == TaskStatus.EXECUTING


@pytest.mark.asyncio
async def test_enqueue_dedup_while_queue_running(processor, state_manager):
    """CRITICAL: second poll must not open a second queue item while first is open."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_event(event):
        started.set()
        await gate.wait()
        return {"ok": True, "work_started": True, "issue_key": "Q-1", "skipped": None}

    processor.process_event = slow_event  # type: ignore

    event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "Q-1",
            "fields": {"summary": "s", "description": "d"},
        },
    }
    t1 = asyncio.create_task(processor.enqueue_jira_event(event))
    await asyncio.wait_for(started.wait(), timeout=3)
    r2 = await processor.enqueue_jira_event(event)
    assert r2.get("duplicate") is True
    open_rows = [
        r
        for r in processor.queue_store.list_items(limit=50)
        if r.get("issue_key") == "Q-1" and r.get("status") in {"queued", "running"}
    ]
    assert len(open_rows) == 1, f"expected one open queue row, got {open_rows}"
    gate.set()
    await t1


def test_poller_skips_in_flight_on_primary_path(state_manager, fake_jira, monkeypatch):
    """CRITICAL: To Do + trigger must not re-dispatch planning/executing."""
    from src.config import settings
    from src.jira.poller import JiraPoller

    monkeypatch.setattr(settings, "trigger_labels", "bot")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    state_manager.create_state("IF-1", "s", "d")
    state_manager.update_state("IF-1", status=TaskStatus.EXECUTING)
    issue = {
        "key": "IF-1",
        "fields": {
            "summary": "s",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    p.client.get_active_sprint = MagicMock(return_value=None)
    p.client.get_board_issues = MagicMock(return_value=[issue])
    result = p.poll_board()
    assert "IF-1" not in [i["key"] for i in result]


def test_poller_skips_plan_ready_without_start_label(
    state_manager, fake_jira, monkeypatch
):
    """CRITICAL: plan_ready + bot alone must not auto-build."""
    from src.config import settings
    from src.jira.poller import JiraPoller

    monkeypatch.setattr(settings, "trigger_labels", "bot")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    state_manager.create_state("PR-1", "s", "d")
    state_manager.update_state("PR-1", status=TaskStatus.PLAN_READY)
    issue = {
        "key": "PR-1",
        "fields": {
            "summary": "s",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    p.client.get_active_sprint = MagicMock(return_value=None)
    p.client.get_board_issues = MagicMock(return_value=[issue])
    result = p.poll_board()
    assert "PR-1" not in [i["key"] for i in result]


def test_set_state_refuses_terminal_clobber_to_pending(tmp_path):
    """CRITICAL: stale RMW cannot revive COMPLETED as PENDING without force."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("CL-1", "s", "d")
    sm.update_state("CL-1", status=TaskStatus.COMPLETED)
    stale = sm.get_state("CL-1")
    assert stale is not None
    stale.status = TaskStatus.PENDING
    assert sm.set_state(stale) is False
    assert sm.get_state("CL-1").status == TaskStatus.COMPLETED


def test_metadata_merge_does_not_wipe(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("MD-1", "s", "d")
    sm.update_state("MD-1", metadata={"a": 1, "b": 2})
    sm.update_state("MD-1", metadata={"b": 3, "c": 4})
    meta = sm.get_state("MD-1").metadata
    assert meta["a"] == 1
    assert meta["b"] == 3
    assert meta["c"] == 4


@pytest.mark.asyncio
async def test_begin_refuses_after_cancel_race(processor, state_manager):
    from src.orchestrator.agent_runner import AgentTask

    state_manager.create_state("BG-1", "s", "d")
    # Cancel before begin (dashboard path, no issue lock)
    await processor.cancel_job("BG-1")
    task = AgentTask(description="d", prompt="p", agent="a", issue_key="BG-1")
    job_id = processor._begin_workflow_run(
        state_manager.get_state("BG-1"),
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="a",
        job_status="executing",
    )
    assert job_id is None
    assert state_manager.get_state("BG-1").status == TaskStatus.CANCELLED
