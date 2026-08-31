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


def test_daemon_does_not_auto_purge_temp_clones():
    """No automatic clone deletion (start, hourly, or job-end policy)."""
    import inspect
    from pathlib import Path

    from src import daemon as daemon_mod
    from src.config import Settings

    source = inspect.getsource(daemon_mod.JiraAgentDaemon)
    assert "_run_temp_cleanup_sweeper" not in source
    assert "purge_stale_temp_dirs" not in source
    assert not hasattr(daemon_mod.JiraAgentDaemon, "_run_temp_cleanup_sweeper")
    assert "temp_cleanup_policy" not in Settings.model_fields
    assert "temp_cleanup_max_age_days" not in Settings.model_fields
    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    assert "TEMP_CLEANUP_POLICY" not in text
    assert "TEMP_CLEANUP_MAX_AGE_DAYS" not in text


def test_cleanup_keeps_temp_dir(tmp_path):
    d = tmp_path / "fresh"
    d.mkdir()
    gm = GitManager.__new__(GitManager)
    gm.issue_key = "CL-AGE"
    gm.temp_dir = d
    assert gm.cleanup(success=True) is True
    assert d.exists()


# ---------------------------------------------------------------------------
# Cancel / abort during post-agent push (critical finding #6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_and_create_mr_skips_when_aborted(processor, state_manager):
    """Cancel/watchdog before delivery must not push or open MR."""
    state_manager.create_state("AB-1", "s", "d")
    state_manager.update_state("AB-1", status=TaskStatus.CANCELLED)
    git = MagicMock()
    processor._contexts = {"AB-1": {"git": git, "runner": MagicMock()}}

    ok = await processor._push_and_create_mr(state_manager.get_state("AB-1"))
    assert ok is False


@pytest.mark.asyncio
async def test_push_and_create_mr_cancel_during_push_skips_notify(
    processor, state_manager, fake_jira
):
    """Cancel mid-push must not treat GitCancelledError as a push-fail comment."""
    from src.git_manager import GitCancelledError

    state_manager.create_state("AB-2", "s", "d")
    state_manager.update_state("AB-2", status=TaskStatus.EXECUTING)
    git = MagicMock()
    git.work_branch = "feature/AB-2"
    git.target_branch = "develop"
    git.get_current_branch.return_value = "feature/AB-2"
    git.ensure_on_work_branch.return_value = True
    git.push.side_effect = GitCancelledError(
        "git cancelled: git remote set-url origin https://oauth2:***@h/r.git"
    )
    processor._contexts = {"AB-2": {"git": git, "runner": MagicMock()}}

    ok = await processor._push_and_create_mr(state_manager.get_state("AB-2"))
    assert ok is False
    git.push.assert_called_once()
    git.create_merge_request.assert_not_called()
    bodies = [c.get("body", "") for c in getattr(fake_jira, "comments", [])]
    assert not any("push failed" in b.lower() for b in bodies)


@pytest.mark.asyncio
async def test_push_skips_mr_when_aborted_after_push(processor, state_manager):
    """If cancel lands after git.push, do not open MR or stamp delivery."""
    state_manager.create_state("AB-2", "s", "d")
    state_manager.update_state("AB-2", status=TaskStatus.EXECUTING)
    git = MagicMock()
    git.ensure_on_work_branch = MagicMock(return_value=True)
    git.work_branch = "feature/AB-2"
    git.target_branch = "develop"
    git.push = MagicMock(return_value=True)
    git.get_last_commit_subject = MagicMock(return_value="feat: x")
    git.get_last_commit_message = MagicMock(return_value="feat: x")
    git.get_last_commit_sha = MagicMock(return_value="abc123deadbeef")
    git.create_merge_request = MagicMock(return_value="https://mr/1")
    git.build_commit_url = MagicMock(return_value=None)

    processor._contexts = {"AB-2": {"git": git, "runner": MagicMock()}}
    state = state_manager.get_state("AB-2")
    call_count = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        call_count["n"] += 1
        out = fn(*args, **kwargs)
        # ensure_on_work_branch (1) then push (2) — abort after push
        if call_count["n"] == 2:
            state_manager.update_state("AB-2", status=TaskStatus.CANCELLED)
        return out

    with patch("src.processor.asyncio.to_thread", side_effect=fake_to_thread):
        ok = await processor._push_and_create_mr(state)

    assert ok is False
    git.create_merge_request.assert_not_called()
    st = state_manager.get_state("AB-2")
    assert st.status == TaskStatus.CANCELLED
    assert (st.metadata or {}).get("delivery_status") != "delivered"
