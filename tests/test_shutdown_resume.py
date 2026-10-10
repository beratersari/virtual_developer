"""Process stop must leave in-flight work for the next start.

Closing the Windows executable kills the process without a graceful
finalise, so the next start still finds a running queue row and continues
it. ``systemctl stop`` sends SIGTERM. That path used to mark the issue
cancelled ("Agent process was stopped") and the dying worker then closed
the queue row, so a later ``nohup`` start left the job sitting on that
error.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest.mock import patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import make_issue_event

STOP_MESSAGE = (
    "Daemon stopped (interrupt or shutdown). "
    "Agent process was stopped; local status is no longer 'executing'."
)


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _running_jira(processor, key: str, *, summary: str, description: str) -> str:
    processor.state_manager.create_state(key, summary, description)
    processor.state_manager.update_state(
        key,
        status=TaskStatus.EXECUTING,
        current_task_id="task-1",
        started_at=datetime.now(),
    )
    event = make_issue_event(key=key, summary=summary, description=description)
    rec = processor.queue_store.enqueue(
        source="jira",
        issue_key=key,
        summary=summary,
        message=description,
        payload=event,
    )
    processor.queue_store.update(
        rec["queue_id"],
        status="running",
        started_at="2026-10-08T00:00:00.000",
    )
    return rec["queue_id"]


def test_sigterm_leaves_inflight_work_for_the_next_start(processor, state_manager):
    """Graceful stop keeps the issue and the queue row resumable.

    The next start marks the leftover ERROR, posts the re-queue comment,
    and queues the running row again. Both steps are intentional.
    """
    qid = _running_jira(
        processor,
        "KAN-9",
        summary="Fix login",
        description="Mode: build\n\nDo the work",
    )
    processor._contexts["KAN-9"] = {
        "git": type("G", (), {"cleanup": lambda self, success=None: True})(),
        "runner": type("R", (), {
            "cancel_task": lambda self, task_id: True,
            "cancel_all_tasks": lambda self: 1,
        })(),
    }

    n = processor.shutdown_processing(
        reason="Daemon stopped (interrupt or shutdown)"
    )
    assert n >= 1
    assert "KAN-9" not in processor._contexts
    st = state_manager.get_state("KAN-9")
    assert st.status == TaskStatus.EXECUTING
    assert STOP_MESSAGE.split(". ")[1] not in (st.error_message or "")
    assert processor.queue_store.get(qid)["status"] == "running"
    assert processor._reap_stale_queue_running() == 0
    assert processor.queue_store.get(qid)["status"] == "running"

    # A new process does not inherit the stop flag.
    processor._shutting_down = False
    assert processor.recover_orphaned_in_flight() == 1
    assert state_manager.get_state("KAN-9").status == TaskStatus.ERROR
    assert processor.queue_store.recover_stuck_running() == 1
    assert processor.queue_store.get(qid)["status"] == "queued"


def test_jobs_already_stopped_by_sigterm_are_queued_again(processor, state_manager):
    """A previous graceful stop already wrote the shutdown error.

    The queue row was closed. The next start must put that payload back
    on the queue. A dashboard Stop uses a different message and stays
    cancelled.
    """
    processor.state_manager.create_state("KAN-9", "Fix login", "Mode: build")
    processor.state_manager.update_state(
        "KAN-9",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
    )
    event = make_issue_event(
        key="KAN-9",
        summary="Fix login",
        description="Mode: build\n\nDo the work",
    )
    rec = processor.queue_store.enqueue(
        source="jira",
        issue_key="KAN-9",
        summary="Fix login",
        message="Mode: build",
        payload=event,
    )
    processor.queue_store.update(rec["queue_id"], status="running")
    processor.queue_store.finish(
        rec["queue_id"],
        status="cancelled",
        error_message="Reaped stale running queue row (issue not live)",
    )

    processor.state_manager.create_state("KAN-8", "Leave me", "Mode: build")
    processor.state_manager.update_state(
        "KAN-8",
        status=TaskStatus.CANCELLED,
        error_message="Cancelled from dashboard",
        completed_at=datetime.now(),
    )
    stopped = processor.queue_store.enqueue(
        source="jira",
        issue_key="KAN-8",
        summary="Leave me",
        message="Mode: build",
        payload=make_issue_event(key="KAN-8", summary="Leave me"),
    )
    processor.queue_store.update(stopped["queue_id"], status="running")
    processor.queue_store.finish(
        stopped["queue_id"],
        status="cancelled",
        error_message="Cancelled from dashboard",
    )

    # A later completed row must not resurrect an older interrupted one.
    processor.state_manager.create_state("KAN-7", "Done later", "Mode: build")
    processor.state_manager.update_state(
        "KAN-7",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
    )
    older = processor.queue_store.enqueue(
        source="jira",
        issue_key="KAN-7",
        summary="Done later",
        message="Mode: build",
        payload=make_issue_event(key="KAN-7"),
    )
    processor.queue_store.update(older["queue_id"], status="running")
    processor.queue_store.finish(older["queue_id"], status="cancelled")
    time.sleep(0.02)
    newer = processor.queue_store.enqueue(
        source="jira",
        issue_key="KAN-7",
        summary="Done later",
        message="Mode: build",
        payload=make_issue_event(key="KAN-7"),
    )
    processor.queue_store.update(newer["queue_id"], status="running")
    processor.queue_store.finish(newer["queue_id"], status="completed")

    n = processor.recover_process_stop_interrupted()
    assert n == 1
    assert processor.queue_store.get(rec["queue_id"])["status"] == "queued"
    assert processor.queue_store.get(stopped["queue_id"])["status"] == "cancelled"
    assert processor.queue_store.get(older["queue_id"])["status"] == "cancelled"
    assert processor.queue_store.get(newer["queue_id"])["status"] == "completed"
    assert state_manager.get_state("KAN-8").status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_requeued_shutdown_job_starts_work_again(processor, state_manager):
    description = (
        "{params}\n"
        "Repository: https://gitlab.example.com/acme/app.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    processor.state_manager.create_state("KAN-9", "Fix login", description)
    processor.state_manager.update_state(
        "KAN-9",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
    )
    event = make_issue_event(
        key="KAN-9",
        summary="Fix login",
        description=description,
    )
    rec = processor.queue_store.enqueue(
        source="jira",
        issue_key="KAN-9",
        summary="Fix login",
        message="Mode: build",
        payload=event,
    )
    processor.queue_store.update(rec["queue_id"], status="running")
    processor.queue_store.finish(
        rec["queue_id"],
        status="cancelled",
        error_message="Reaped stale running queue row (issue not live)",
    )
    assert processor.recover_process_stop_interrupted() == 1
    started: list[str] = []

    async def _run(state, kind="build"):
        started.append(state.issue_key)

    with patch.object(processor, "_start_execution_workflow", side_effect=_run):
        row = processor.queue_store.get(rec["queue_id"])
        await processor._run_queue_item(row)

    assert started == ["KAN-9"]
    assert state_manager.get_state("KAN-9").status != TaskStatus.CANCELLED


def test_dispatch_during_stop_does_not_close_the_running_row(
    processor, state_manager
):
    """The dying worker's queue dispatch must not reap the row it was running."""
    qid = _running_jira(
        processor,
        "KAN-9",
        summary="Fix login",
        description="Mode: build",
    )
    state_manager.update_state(
        "KAN-9",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
    )
    processor._shutting_down = True
    assert asyncio.run(processor.dispatch_queue()) == 0
    assert processor.queue_store.get(qid)["status"] == "running"


def test_process_stop_without_a_queue_row_enqueues_the_issue(
    processor, state_manager
):
    processor.state_manager.create_state("KAN-4", "Fix login", "Mode: build")
    processor.state_manager.update_state(
        "KAN-4",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
    )
    assert processor.recover_process_stop_interrupted() == 1
    rows = processor.queue_store.list_items(status="queued", limit=10)
    assert len(rows) == 1
    assert rows[0]["issue_key"] == "KAN-4"
    assert rows[0]["payload"]["webhookEvent"] == "jira:issue_created"
    assert state_manager.get_state("KAN-4").status == TaskStatus.CANCELLED


def test_gitlab_stop_without_payload_is_not_a_jira_job(processor):
    processor.state_manager.create_state("GL-4", "Review", "look at this")
    processor.state_manager.update_state(
        "GL-4",
        status=TaskStatus.CANCELLED,
        error_message=STOP_MESSAGE,
        completed_at=datetime.now(),
        metadata={"source": "gitlab"},
    )
    assert processor.recover_process_stop_interrupted() == 0
    assert processor.queue_store.list_items(status="queued", limit=10) == []


def test_failure_during_stop_keeps_the_inflight_status(processor, state_manager):
    _running_jira(
        processor,
        "KAN-9",
        summary="Fix login",
        description="Mode: build",
    )
    processor._shutting_down = True
    processor._fail_issue("KAN-9", "agent died while the process was stopping")
    assert state_manager.get_state("KAN-9").status == TaskStatus.EXECUTING
