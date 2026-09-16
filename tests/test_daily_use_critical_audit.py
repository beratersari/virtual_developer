"""Proof tests for the 2026-09-16 daily-use critical audit.

Each test asserts the *correct* operator-visible behaviour.
A failure means the production hole is still open.

Ignore this file in the green suite until the matching bugs are fixed
(same role as ``tests/test_logical_issues.py``):

  pytest tests/ --ignore=tests/test_logical_issues.py \\
      --ignore=tests/test_daily_use_critical_audit.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from src.config import settings
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def _processor(tmp_path: Path, isolate: Dict[str, Any], monkeypatch) -> JobProcessor:
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    return proc


def test_reap_must_not_drop_pending_accept_or_free_the_issue_lock(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """PENDING accept is live work. Reap must not let a second row claim the key.

    Daily race: poller has just written PENDING and is awaiting Jira In
    Progress. A finishing job kicks ``dispatch_queue``, which reaps the
    running Jira row, then ``claim_next`` can take a GitLab ``/yaver`` for
    the same issue. Two workers then mutate one clone.
    """
    isolate = isolate_jira_agent_artifacts
    proc = _processor(tmp_path, isolate, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12)", "from board")
    sm.update_state("KAN-12", status=TaskStatus.PENDING, completed_at=None)

    jira_row = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-12",
        summary="board accept",
    )
    proc.queue_store.update(jira_row["queue_id"], status="running")
    gitlab_row = proc.queue_store.enqueue(
        source="gitlab",
        issue_key="KAN-12",
        summary="mr followup",
        lock_key="gitlab:acme/app!4",
    )

    n = proc._reap_stale_queue_running()
    live_jira = proc.queue_store.get(jira_row["queue_id"])
    assert n == 0, (
        f"reap finished {n} row(s) during PENDING accept "
        f"(jira status={live_jira.get('status') if live_jira else None})"
    )
    assert live_jira is not None and live_jira["status"] == "running"

    claimed = proc.queue_store.claim_next(max_running=6)
    if claimed is not None:
        assert claimed["queue_id"] != gitlab_row["queue_id"], (
            "claim_next handed the same issue to a GitLab row while the "
            "PENDING Jira accept is still running"
        )
