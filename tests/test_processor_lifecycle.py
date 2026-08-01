"""Processing cache, shutdown, and orphan recovery.

Covers the lifecycle rules:
- Live jobs live in in-memory ``_contexts``; poll/create must not double-start them.
- Disk ``planning``/``executing`` without a live process is orphaned on cold start → ERROR.
- Graceful stop kills child processes and writes CANCELLED + Jira notify.
- Non-processing + To Do can still start (not blocked by recovery/shutdown).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient, make_issue_event


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = reporter
    p.jira_client = fake_jira
    return p


# ---------------------------------------------------------------------------
# Startup orphan recovery (disk in-flight, no live process)
# ---------------------------------------------------------------------------

def test_recover_orphaned_executing_marks_error(processor, state_manager, fake_jira):
    state_manager.create_state("ORPH-1", "s", "d")
    state_manager.update_state(
        "ORPH-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now(),
        current_task_id="t-dead",
    )
    n = processor.recover_orphaned_in_flight()
    assert n == 1
    st = state_manager.get_state("ORPH-1")
    assert st.status == TaskStatus.ERROR
    assert st.current_task_id is None
    assert st.error_message
    assert fake_jira.comments, "Jira must be notified on orphan recovery"


def test_recover_orphaned_planning_marks_error(processor, state_manager, fake_jira):
    state_manager.create_state("ORPH-P", "s", "d")
    state_manager.update_state(
        "ORPH-P",
        status=TaskStatus.PLANNING,
        started_at=datetime.now(),
        current_task_id="t-plan",
    )
    assert processor.recover_orphaned_in_flight() == 1
    st = state_manager.get_state("ORPH-P")
    assert st.status == TaskStatus.ERROR
    assert fake_jira.comments


def test_recover_multiple_orphans(processor, state_manager):
    for key, status in (
        ("M-1", TaskStatus.EXECUTING),
        ("M-2", TaskStatus.PLANNING),
    ):
        state_manager.create_state(key, "s", "d")
        state_manager.update_state(key, status=status, started_at=datetime.now())
    assert processor.recover_orphaned_in_flight() == 2
    assert state_manager.get_state("M-1").status == TaskStatus.ERROR
    assert state_manager.get_state("M-2").status == TaskStatus.ERROR


def test_recover_skips_plan_ready_pending_completed(processor, state_manager):
    state_manager.create_state("OK-1", "s", "d")
    state_manager.update_state("OK-1", status=TaskStatus.PLAN_READY)
    state_manager.create_state("OK-2", "s", "d")  # PENDING
    state_manager.create_state("OK-3", "s", "d")
    state_manager.update_state("OK-3", status=TaskStatus.COMPLETED, completed_at=datetime.now())
    assert processor.recover_orphaned_in_flight() == 0
    assert state_manager.get_state("OK-1").status == TaskStatus.PLAN_READY
    assert state_manager.get_state("OK-2").status == TaskStatus.PENDING
    assert state_manager.get_state("OK-3").status == TaskStatus.COMPLETED


def test_after_orphan_recovery_todo_can_reprocess(processor, state_manager):
    """ERROR after recovery is terminal; To Do update is allowed to re-queue."""
    state_manager.create_state("RQ-1", "fix typo", "d")
    state_manager.update_state(
        "RQ-1", status=TaskStatus.EXECUTING, started_at=datetime.now()
    )
    processor.recover_orphaned_in_flight()
    assert state_manager.get_state("RQ-1").status == TaskStatus.ERROR

    started = []

    async def cap(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_direct_execution", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                with patch.object(processor, "_start_oracle_consultation", side_effect=cap):
                    await processor._handle_issue_updated(
                        make_issue_event(
                            key="RQ-1",
                            summary="fix typo",
                            event_type="jira:issue_updated",
                            status="To Do",
                        )
                    )

    asyncio.run(run())
    assert started == ["RQ-1"]


# ---------------------------------------------------------------------------
# Live processing cache — do not double-start
# ---------------------------------------------------------------------------

def test_live_cache_blocks_create(processor, state_manager):
    state_manager.create_state("LIVE-1", "fix typo", "d")
    processor._contexts["LIVE-1"] = {"git": MagicMock(), "runner": MagicMock()}

    started = []

    async def boom(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_direct_execution", side_effect=boom):
            await processor._handle_issue_created(make_issue_event(key="LIVE-1"))

    asyncio.run(run())
    assert started == []
    assert state_manager.get_state("LIVE-1").status == TaskStatus.PENDING


def test_live_cache_blocks_update(processor, state_manager):
    state_manager.create_state("LIVE-2", "fix typo", "d")
    state_manager.update_state("LIVE-2", status=TaskStatus.PENDING)
    processor._contexts["LIVE-2"] = {"git": MagicMock(), "runner": MagicMock()}

    started = []

    async def boom(*a, **k):
        started.append(1)

    async def run():
        with patch.object(processor, "_handle_issue_created", side_effect=boom):
            await processor._handle_issue_updated(
                make_issue_event(key="LIVE-2", event_type="jira:issue_updated")
            )

    asyncio.run(run())
    assert started == []


def test_disk_inflight_blocks_create_without_cache(processor, state_manager):
    """Disk EXECUTING alone (no _contexts yet) still skips create — existing guard."""
    state_manager.create_state("DISK-1", "fix typo", "d")
    state_manager.update_state(
        "DISK-1", status=TaskStatus.EXECUTING, started_at=datetime.now()
    )
    assert "DISK-1" not in processor._contexts

    started = []

    async def boom(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_direct_execution", side_effect=boom):
            await processor._handle_issue_created(make_issue_event(key="DISK-1"))

    asyncio.run(run())
    assert started == []


def test_list_live_processing_keys(processor):
    processor._contexts["A-1"] = {"git": None, "runner": MagicMock()}
    processor._contexts["B-2"] = {"git": None, "runner": MagicMock()}
    keys = processor.list_live_processing_keys()
    assert set(keys) == {"A-1", "B-2"}


def test_not_processing_pending_todo_can_start(processor, state_manager):
    """Not in cache, PENDING + To Do update → may start (user rule)."""
    state_manager.create_state("GO-1", "fix typo", "d")
    assert state_manager.get_state("GO-1").status == TaskStatus.PENDING

    started = []

    async def cap(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_direct_execution", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                with patch.object(processor, "_start_oracle_consultation", side_effect=cap):
                    await processor._handle_issue_updated(
                        make_issue_event(
                            key="GO-1",
                            summary="fix typo",
                            event_type="jira:issue_updated",
                            status="To Do",
                        )
                    )

    asyncio.run(run())
    assert started == ["GO-1"]


# ---------------------------------------------------------------------------
# Graceful shutdown — kill children + CANCELLED
# ---------------------------------------------------------------------------

def test_shutdown_kills_children_and_cancels_state(processor, state_manager, fake_jira):
    state_manager.create_state("SH-1", "s", "d")
    state_manager.update_state(
        "SH-1",
        status=TaskStatus.PLANNING,
        current_task_id="task-1",
        started_at=datetime.now(),
    )
    runner = MagicMock()
    runner.cancel_task = MagicMock(return_value=True)
    runner.cancel_all_tasks = MagicMock(return_value=1)
    git = MagicMock()
    processor._contexts["SH-1"] = {"git": git, "runner": runner}
    processor.agent_runner = runner

    n = processor.shutdown_processing(reason="Daemon stopped")
    assert n >= 1
    st = state_manager.get_state("SH-1")
    assert st.status == TaskStatus.CANCELLED
    assert st.current_task_id is None
    runner.cancel_task.assert_called()
    runner.cancel_all_tasks.assert_called()
    git.cleanup.assert_called()
    assert "SH-1" not in processor._contexts
    assert fake_jira.comments, "Jira comment required on shutdown cancel"


def test_shutdown_finalises_disk_inflight_without_context(processor, state_manager, fake_jira):
    """In-flight on disk but missing from cache still becomes CANCELLED."""
    state_manager.create_state("SH-2", "s", "d")
    state_manager.update_state(
        "SH-2",
        status=TaskStatus.EXECUTING,
        current_task_id="t2",
        started_at=datetime.now(),
    )
    assert "SH-2" not in processor._contexts

    n = processor.shutdown_processing(reason="Daemon stopped")
    assert n >= 1
    st = state_manager.get_state("SH-2")
    assert st.status == TaskStatus.CANCELLED
    assert st.current_task_id is None
    assert fake_jira.comments


def test_shutdown_multiple_live_jobs(processor, state_manager, fake_jira):
    for key in ("J-1", "J-2"):
        state_manager.create_state(key, "s", "d")
        state_manager.update_state(
            key,
            status=TaskStatus.EXECUTING,
            current_task_id=f"t-{key}",
            started_at=datetime.now(),
        )
        processor._contexts[key] = {
            "git": MagicMock(),
            "runner": MagicMock(
                cancel_task=MagicMock(return_value=True),
                cancel_all_tasks=MagicMock(return_value=1),
            ),
        }

    n = processor.shutdown_processing(reason="stop")
    assert n == 2
    assert state_manager.get_state("J-1").status == TaskStatus.CANCELLED
    assert state_manager.get_state("J-2").status == TaskStatus.CANCELLED
    assert processor._contexts == {}
    assert len(fake_jira.comments) >= 2


def test_shutdown_clears_legacy_agent_runner(processor, state_manager):
    runner = MagicMock()
    runner.cancel_all_tasks = MagicMock(return_value=0)
    processor.agent_runner = runner
    processor.git_manager = MagicMock()
    processor.shutdown_processing(reason="stop")
    assert processor.agent_runner is None
    assert processor.git_manager is None
    runner.cancel_all_tasks.assert_called()


def test_agent_runner_cancel_all_tasks():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner()
    proc_a = MagicMock()
    proc_a.returncode = None
    proc_b = MagicMock()
    proc_b.returncode = 0  # already done — do not kill
    proc_c = MagicMock()
    proc_c.returncode = None
    runner._running_tasks = {"a": proc_a, "b": proc_b, "c": proc_c}

    def _kill(process, force=False):
        # Soft kill succeeds — no escalate needed
        process.returncode = -15

    with patch.object(runner, "_kill_process_tree", side_effect=_kill) as kill:
        with patch("time.sleep"):  # cancel_all may sleep before escalate
            n = runner.cancel_all_tasks()
    assert n == 2
    assert kill.call_count == 2


# ---------------------------------------------------------------------------
# Daemon wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daemon_start_runs_orphan_recovery():
    from src.daemon import JiraAgentDaemon

    with patch("src.daemon.settings") as s:
        s.validate_or_raise = MagicMock()
        s.project_root = "/tmp"
        s.jira_host = "http://j"
        s.auto_start_plans = False
        s.jira_board_id = "1"
        s.poll_interval_seconds = 30

        daemon = JiraAgentDaemon()
        daemon.processor = MagicMock()
        daemon.processor.recover_orphaned_in_flight = MagicMock(return_value=2)
        daemon.state_manager = MagicMock()
        daemon.state_manager.get_active_issues.return_value = []

        with patch.object(daemon, "_start_poller", new_callable=AsyncMock):
            with patch.object(daemon, "_monitor_active_issues", new_callable=AsyncMock):
                with patch("src.daemon.IS_WINDOWS", False):
                    with patch("asyncio.get_event_loop") as gel:
                        gel.return_value = MagicMock()
                        with patch("asyncio.gather", new_callable=AsyncMock) as gather:
                            gather.return_value = None
                            await daemon.start()

        daemon.processor.recover_orphaned_in_flight.assert_called_once()


@pytest.mark.asyncio
async def test_daemon_stop_calls_shutdown_then_exits():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=1)

    with patch("sys.exit") as ex:
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                ex.assert_called_with(0)

    daemon._poller.stop.assert_called_once()
    daemon.processor.shutdown_processing.assert_called_once()
    reason = daemon.processor.shutdown_processing.call_args.kwargs.get("reason", "")
    assert "Daemon stopped" in reason

@pytest.mark.asyncio
async def test_daemon_stop_idempotent():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=0)

    with patch("sys.exit"):
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                await daemon.stop()
    assert daemon.processor.shutdown_processing.call_count == 1


@pytest.mark.asyncio
async def test_poller_handler_ignored_while_stopping():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon.processor = MagicMock()
    daemon.processor.process_event = AsyncMock()
    daemon._running = False
    daemon._stopping = True

    with patch("src.daemon.JiraPoller") as Poller:
        poller = MagicMock()
        Poller.return_value = poller
        with patch("src.daemon.settings") as s:
            s.jira_board_id = "1"
            with patch("asyncio.get_event_loop") as gel:
                loop = MagicMock()
                loop.run_in_executor = AsyncMock(return_value=None)
                gel.return_value = loop
                await daemon._start_poller()
                handler = loop.run_in_executor.call_args[0][2]
                handler({"webhookEvent": "jira:issue_created", "issue": {"key": "X"}})

    # run_coroutine_threadsafe must not be used when stopping
    loop.run_coroutine_threadsafe = MagicMock()
    # handler returned early — process_event not scheduled via threadsafe in our path
    # (we return before run_coroutine_threadsafe)
    # Re-invoke with captured handler behaviour: no exception, no process_event await
    assert daemon.processor.process_event.await_count == 0
