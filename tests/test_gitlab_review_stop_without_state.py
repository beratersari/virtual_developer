"""Stop and restart must not leave a GitLab review executing after its state is gone.

Merge cleanup deletes the issue-state row and keeps the job row for Analytics.
The jobs list still treats status ``executing`` as in flight, so the elapsed
clock keeps running and Stop posts that issue key. The safe outcome is: the
job row remains, and it is no longer executing after cleanup, after a cold
start, or after dashboard Stop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.state.models import TaskStatus

_KEY = "GL-ACME-APP-4"
_MR = "https://gitlab.example.com/acme/app/-/merge_requests/4"
_OPEN = {"pending", "planning", "executing", "running"}


def _processor(fake_jira, reporter):
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _executing_review(proc):
    state = proc.state_manager.create_state(_KEY, "MR !4", "review the change")
    assert state is not None
    proc.state_manager.update_state(
        _KEY,
        status=TaskStatus.EXECUTING,
        metadata={
            "source": "gitlab",
            "workflow_type": "gitlab-review",
            "merge_request_url": _MR,
        },
    )
    job = proc.job_store.create_job(
        issue_key=_KEY,
        summary="MR !4",
        workflow_type="gitlab-review",
        agent="derman-reviewer",
        status="executing",
        source="gitlab",
        merge_request_url=_MR,
    )
    assert job is not None
    proc.state_manager.update_state(
        _KEY, metadata={"current_job_id": job["job_id"]}
    )
    return job


def test_merge_cleanup_must_not_keep_a_gitlab_review_executing(
    fake_jira, reporter
):
    proc = _processor(fake_jira, reporter)
    job = _executing_review(proc)
    from src.dashboard.temp_storage import _purge_merged_review_artifacts

    _purge_merged_review_artifacts(mr_url=_MR, issue_key=_KEY)

    assert proc.state_manager.get_state(_KEY) is None
    kept = proc.job_store.get_job(job["job_id"])
    assert kept is not None
    assert kept["status"] not in _OPEN
    assert kept.get("completed_at")


def test_startup_recovery_must_close_executing_job_when_state_is_gone(
    fake_jira, reporter
):
    proc = _processor(fake_jira, reporter)
    job = _executing_review(proc)
    assert proc.state_manager.delete_state(_KEY) is True

    proc.recover_orphaned_in_flight()

    kept = proc.job_store.get_job(job["job_id"])
    assert kept is not None
    assert kept["status"] not in _OPEN
    assert kept.get("completed_at")


@pytest.mark.asyncio
async def test_stop_must_cancel_executing_job_when_state_is_gone(
    fake_jira, reporter
):
    proc = _processor(fake_jira, reporter)
    job = _executing_review(proc)
    assert proc.state_manager.delete_state(_KEY) is True
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    proc._contexts[_KEY] = {"git": None, "runner": runner}
    proc._active_jobs[_KEY] = job["job_id"]

    out = await proc.cancel_job(_KEY, reason="Cancelled from ops dashboard")

    assert out["ok"] is True, out
    assert "No local state" not in str(out.get("error") or "")
    kept = proc.job_store.get_job(job["job_id"])
    assert kept is not None
    assert kept["status"] == "cancelled"
    assert kept.get("completed_at")
    assert runner.cancel_all_tasks.called
