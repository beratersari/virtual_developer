"""Queue reap must not drop a live PENDING accept.

After dashboard Stop, leftover ``running`` rows should still be closed.
The accept window (local PENDING, empty ``_contexts``) is live work.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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


def _running(proc: JobProcessor, *, issue: str, source: str = "jira", **extra: Any):
    rec = proc.queue_store.enqueue(
        source=source,
        issue_key=issue,
        summary=f"{source} {issue}",
        **extra,
    )
    return proc.queue_store.update(rec["queue_id"], status="running")


def test_reap_keeps_pending_accept_and_blocks_same_issue_followup(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Board accept + queued /yaver: reap must not start a second worker."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    proc.state_manager.create_state("KAN-12", "feat(KAN-12)", "from board")
    proc.state_manager.update_state(
        "KAN-12", status=TaskStatus.PENDING, completed_at=None
    )
    jira = _running(proc, issue="KAN-12", source="jira")
    gitlab = proc.queue_store.enqueue(
        source="gitlab",
        issue_key="KAN-12",
        summary="mr followup",
        lock_key="gitlab:acme/app!4",
    )

    n = proc._reap_stale_queue_running()
    live = proc.queue_store.get(jira["queue_id"])
    assert n == 0
    assert live is not None and live["status"] == "running"

    claimed = proc.queue_store.claim_next(max_running=6)
    assert claimed is None or claimed["queue_id"] != gitlab["queue_id"]
    still = proc.queue_store.get(gitlab["queue_id"])
    assert still is not None and still["status"] == "queued"


def test_reap_keeps_pending_gitlab_accept(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    proc.state_manager.create_state("KAN-12", "feat", "from mr")
    proc.state_manager.update_state("KAN-12", status=TaskStatus.PENDING)
    rec = _running(proc, issue="KAN-12", source="gitlab")
    assert proc._reap_stale_queue_running() == 0
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


def test_reap_keeps_planning_and_executing(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    for key, status in (("KAN-P", TaskStatus.PLANNING), ("KAN-E", TaskStatus.EXECUTING)):
        proc.state_manager.create_state(key, "s", "d")
        proc.state_manager.update_state(key, status=status)
        rec = _running(proc, issue=key)
        assert proc._reap_stale_queue_running() == 0
        assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


def test_reap_keeps_row_when_live_in_contexts(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Stop leftover with CANCELLED is reaped; in-memory slot still wins."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    proc.state_manager.create_state("KAN-12", "s", "d")
    proc.state_manager.update_state("KAN-12", status=TaskStatus.CANCELLED)
    rec = _running(proc, issue="KAN-12")
    proc._contexts["KAN-12"] = {"git": None, "runner": None}
    assert proc._reap_stale_queue_running() == 0
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


def test_reap_skips_first_run_with_no_local_state(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = _running(proc, issue="KAN-NEW")
    assert proc.state_manager.get_state("KAN-NEW") is None
    assert proc._reap_stale_queue_running() == 0
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


def test_reap_closes_cancelled_error_completed_leftovers(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    cases = (
        ("KAN-C", TaskStatus.CANCELLED, "cancelled"),
        ("KAN-X", TaskStatus.ERROR, "error"),
        ("KAN-D", TaskStatus.COMPLETED, "completed"),
    )
    rows = []
    done = datetime.now()
    for key, status, expect in cases:
        proc.state_manager.create_state(key, "s", "d")
        proc.state_manager.update_state(key, status=status, completed_at=done)
        rec = _running(proc, issue=key)
        proc.queue_store.update(
            rec["queue_id"],
            started_at=(done - timedelta(seconds=5)).isoformat(
                timespec="milliseconds"
            ),
        )
        rows.append((rec["queue_id"], expect))

    n = proc._reap_stale_queue_running()
    assert n == 3
    for qid, expect in rows:
        live = proc.queue_store.get(qid)
        assert live["status"] == expect
        assert "not live" in (live.get("error_message") or "")


def test_reap_keeps_new_claim_started_after_cancel(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Brand-new running row after Stop must not be treated as the leftover."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    done = datetime.now()
    proc.state_manager.create_state("KAN-12", "s", "d")
    proc.state_manager.update_state(
        "KAN-12", status=TaskStatus.CANCELLED, completed_at=done
    )
    rec = _running(proc, issue="KAN-12")
    proc.queue_store.update(
        rec["queue_id"],
        started_at=(done + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
    )
    assert proc._reap_stale_queue_running() == 0
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


def test_reap_closes_plan_ready_leftover_running_row(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Plan finished; a stuck running row is leftover, not an accept window."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    proc.state_manager.create_state("KAN-12", "s", "d")
    proc.state_manager.update_state("KAN-12", status=TaskStatus.PLAN_READY)
    rec = _running(proc, issue="KAN-12")
    assert proc._reap_stale_queue_running() == 1
    assert proc.queue_store.get(rec["queue_id"])["status"] == "skipped"


def test_reap_pending_does_not_block_other_issues(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    proc.state_manager.create_state("KAN-12", "s", "d")
    proc.state_manager.update_state("KAN-12", status=TaskStatus.PENDING)
    _running(proc, issue="KAN-12")
    other = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-99",
        summary="other",
    )
    assert proc._reap_stale_queue_running() == 0
    claimed = proc.queue_store.claim_next(max_running=6)
    assert claimed is not None
    assert claimed["queue_id"] == other["queue_id"]
