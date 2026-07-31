"""Unit tests for stuck-state watchdog in daemon monitor."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.state.models import TaskStatus


@pytest.mark.asyncio
async def test_stuck_issue_reported_to_jira(state_manager, reporter, fake_jira):
    """Jobs stuck in EXECUTING past wall-clock limit must ERROR + Jira comment."""
    from src.daemon import JiraAgentDaemon
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = proc
    daemon.state_manager = state_manager
    daemon._running = True

    state = state_manager.create_state("STUCK-1", "hanging", "x")
    state_manager.update_state(
        "STUCK-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(hours=5),
        timeout_seconds=60,
        max_retries=0,
    )

    # Run one iteration of the monitor loop by calling the body logic
    # We patch sleep to stop after first pass
    call_count = {"n": 0}

    async def stop_after_first(_seconds):
        call_count["n"] += 1
        daemon._running = False

    with patch("asyncio.sleep", side_effect=stop_after_first):
        await daemon._monitor_active_issues()

    loaded = state_manager.get_state("STUCK-1")
    assert loaded.status == TaskStatus.ERROR
    assert any("stuck" in c["body"].lower() for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_fresh_issue_not_marked_stuck(state_manager, reporter, fake_jira):
    from src.daemon import JiraAgentDaemon
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = proc
    daemon.state_manager = state_manager
    daemon._running = True

    state_manager.create_state("OK-1", "fresh", "x")
    state_manager.update_state(
        "OK-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now(),
        timeout_seconds=1800,
        max_retries=3,
    )

    async def stop_after_first(_seconds):
        daemon._running = False

    with patch("asyncio.sleep", side_effect=stop_after_first):
        await daemon._monitor_active_issues()

    assert state_manager.get_state("OK-1").status == TaskStatus.EXECUTING
    assert not any("stuck" in c["body"].lower() for c in fake_jira.comments)
