"""Regression tests for cancel lock, CAS fail/cancel, age cleanup, assignee names."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import GitManager, purge_stale_temp_dirs
from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.state.models import TaskStatus


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


# ---------------------------------------------------------------------------
# Dashboard cancel must not wait on the long-held workflow lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_job_while_issue_lock_held(processor, state_manager, fake_jira):
    """Cancel must kill + CAS without awaiting the workflow lock."""
    state_manager.create_state("LK-1", "s", "d")
    state_manager.update_state(
        "LK-1", status=TaskStatus.EXECUTING, current_task_id="t-lock"
    )
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    processor._contexts["LK-1"] = {"git": MagicMock(), "runner": runner}
    processor.agent_runner = runner

    lock = processor._get_issue_lock("LK-1")
    await lock.acquire()
    try:
        # Must complete even while lock is held by a simulated long workflow
        out = await asyncio.wait_for(
            processor.cancel_job("LK-1", reason="dashboard cancel under lock"),
            timeout=2.0,
        )
    finally:
        lock.release()

    assert out["ok"] is True
    assert state_manager.get_state("LK-1").status == TaskStatus.CANCELLED
    assert "LK-1" not in processor._contexts
    runner.cancel_task.assert_called()


# ---------------------------------------------------------------------------
# CAS: fail / cancel must not overwrite COMPLETED
# ---------------------------------------------------------------------------


def test_fail_issue_does_not_overwrite_completed(processor, state_manager, fake_jira):
    state_manager.create_state("CAS-1", "s", "d")
    state_manager.update_state("CAS-1", status=TaskStatus.COMPLETED)
    processor._fail_issue("CAS-1", "late watchdog fail")
    assert state_manager.get_state("CAS-1").status == TaskStatus.COMPLETED


def test_fail_issue_does_not_overwrite_cancelled(processor, state_manager, fake_jira):
    state_manager.create_state("CAS-2", "s", "d")
    state_manager.update_state("CAS-2", status=TaskStatus.CANCELLED)
    processor._fail_issue("CAS-2", "late fail after cancel")
    assert state_manager.get_state("CAS-2").status == TaskStatus.CANCELLED


def test_cancel_issue_state_does_not_overwrite_completed(processor, state_manager, fake_jira):
    state_manager.create_state("CAS-3", "s", "d")
    state_manager.update_state("CAS-3", status=TaskStatus.COMPLETED)
    ok = processor._cancel_issue_state(
        "CAS-3", message="late cancel", status=TaskStatus.CANCELLED
    )
    assert ok is False
    assert state_manager.get_state("CAS-3").status == TaskStatus.COMPLETED


def test_fail_issue_still_errors_in_flight(processor, state_manager, fake_jira):
    state_manager.create_state("CAS-4", "s", "d")
    state_manager.update_state("CAS-4", status=TaskStatus.EXECUTING)
    processor._fail_issue("CAS-4", "real failure")
    assert state_manager.get_state("CAS-4").status == TaskStatus.ERROR


# ---------------------------------------------------------------------------
# plan_ready: Mode:build alone does not start; start label does
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_ready_mode_build_alone_does_not_start(processor, state_manager):
    state_manager.create_state(
        "PR-M1",
        "s",
        "{params}\nRepository: https://g.example/r.git\n"
        "Source branch: feature/x\nTarget branch: develop\nMode: build\n{params}",
    )
    state_manager.update_state("PR-M1", status=TaskStatus.PLAN_READY)
    started = {"ok": False}

    async def fake_exec(st):
        started["ok"] = True

    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PR-M1",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["ai-assist"],
                "summary": "s",
                "description": (
                    "{params}\nRepository: https://g.example/r.git\n"
                    "Source branch: feature/x\nTarget branch: develop\n"
                    "Mode: build\n{params}"
                ),
            },
        },
    }
    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        await processor._handle_issue_updated(event)
    assert started["ok"] is False
    assert state_manager.get_state("PR-M1").status == TaskStatus.PLAN_READY


# ---------------------------------------------------------------------------
# Assignee match uses configurable names
# ---------------------------------------------------------------------------


def test_assignee_matches_devbot_via_config():
    poller = JiraPoller(client=MagicMock(), interval_seconds=1, board_id="1")
    with patch("src.jira.poller.settings") as s:
        s.trigger_assignee_names_list = ["devbot", "jira ai bot"]
        assert poller._assignee_looks_like_bot({"displayName": "DevBot"}) is True
        assert poller._assignee_looks_like_bot({"displayName": "Alice"}) is False
        assert poller._assignee_looks_like_bot({"name": "devbot-svc"}) is True
        assert poller._assignee_looks_like_bot(None) is False


# ---------------------------------------------------------------------------
# Age cleanup / purge
# ---------------------------------------------------------------------------


def test_purge_stale_temp_dirs_removes_old_only(tmp_path):
    old = tmp_path / "old_clone"
    new = tmp_path / "new_clone"
    old.mkdir()
    new.mkdir()
    # Make old dir look > 1 day old
    old_mtime = time.time() - (2 * 86400)
    import os

    os.utime(old, (old_mtime, old_mtime))

    removed = purge_stale_temp_dirs(max_age_days=1.0, base_dir=tmp_path)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_cleanup_age_keeps_fresh_dir(tmp_path):
    d = tmp_path / "fresh"
    d.mkdir()
    gm = GitManager.__new__(GitManager)
    gm.temp_dir = d
    with patch("src.git_manager.settings") as s:
        s.temp_cleanup_policy = "age"
        s.temp_cleanup_max_age_days = 1.0
        assert gm.cleanup(success=True) is True
        assert d.exists()
