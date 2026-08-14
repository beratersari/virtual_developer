"""Legacy session jobs are never shown; retries nest under JobStore jobs."""

from __future__ import annotations

from pathlib import Path

from src.dashboard.service import build_jobs, _legacy_jobs_from_sessions
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_open_job_never_shows_legacy_for_session_logs(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    jobs = isolate_jira_agent_artifacts["job_store"]
    # Session log created while agent runs (no link on job yet)
    log = sessions / "KAN-1_20260801_184430.log"
    log.write_text("partial output\n")
    (sessions / "KAN-1_20260801_184430.prompt.txt").write_text(
        "# Direct\n\n## Task\nhello\n\n# X\n"
    )
    # Retry log also present — must not become a separate job
    retry_log = sessions / "KAN-1_20260801_184500_retry1.log"
    retry_log.write_text("retry output\n")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "test", "hello")
    sm.update_state("KAN-1", status=TaskStatus.EXECUTING)
    job = jobs.create_job(
        issue_key="KAN-1",
        summary="test",
        description="hello",
        workflow_type="execution",
        agent="sisyphus",
        task_id="task_x",
        status="executing",
    )
    jobs.update_job(job["job_id"], started_at="2026-08-01T18:44:23")

    class _P:
        def list_live_processing_keys(self):
            return ["KAN-1"]

        _active_jobs = {"KAN-1": job["job_id"]}

    resp = build_jobs(
        issue_key="KAN-1",
        processor=_P(),  # type: ignore[arg-type]
        store=jobs,
        state_manager=sm,
    )
    ids = [j.job_id for j in resp.jobs]
    assert job["job_id"] in ids
    assert not any(i.startswith("legacy_") for i in ids), ids


def test_unlinked_session_logs_not_shown_as_legacy_jobs(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Product rule: never synthesize legacy_* jobs from session files."""
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    jobs = isolate_jira_agent_artifacts["job_store"]
    old = sessions / "KAN-1_20260701_100000.log"
    old.write_text("old run\n")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "test", "hello")
    j = jobs.create_job(
        issue_key="KAN-1",
        summary="test",
        description="hello",
        status="completed",
        task_id="t2",
    )
    jobs.update_job(
        j["job_id"],
        session_log_path=str(sessions / "other.log"),
        started_at="2026-08-01T12:00:00",
    )

    resp = build_jobs(issue_key="KAN-1", store=jobs, state_manager=sm)
    ids = [x.job_id for x in resp.jobs]
    assert j["job_id"] in ids
    assert not any(i.startswith("legacy_") for i in ids), ids
    # Helper is a permanent no-op
    assert (
        _legacy_jobs_from_sessions(
            summaries={},
        )
        == []
    )


def test_retry_paths_and_attempts_nested_on_job(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    jobs = isolate_jira_agent_artifacts["job_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-2", "retry nest", "d")

    initial = str(sessions / "KAN-2_20260805_120000.log")
    retry1 = str(sessions / "KAN-2_20260805_120100_retry1.log")
    Path(initial).write_text("first\n")
    Path(retry1).write_text("second\n")

    j = jobs.create_job(
        issue_key="KAN-2",
        summary="retry nest",
        description="d",
        status="executing",
        task_id="task_a",
    )
    jobs.update_job(j["job_id"], session_log_path=initial)
    jobs.update_job(
        j["job_id"],
        retry_attempt={
            "attempt_number": 1,
            "label": "retry1",
            "reason": "timeout",
            "delay_seconds": 5.0,
            "failed_session_log_path": initial,
            "error_message": "\n[TIMEOUT] Task exceeded 30 seconds",
            "return_code": -1,
            "opencode_session_id": "ses_fail",
            "task_id": "task_b",
            "timestamp": "2026-08-05T12:01:00",
        },
        task_id="task_b",
        session_log_path=retry1,
    )

    resp = build_jobs(issue_key="KAN-2", store=jobs, state_manager=sm)
    assert len(resp.jobs) == 1
    job = resp.jobs[0]
    assert job.job_id == j["job_id"]
    assert not job.job_id.startswith("legacy_")
    assert initial in job.session_log_paths
    assert retry1 in job.session_log_paths
    assert job.session_log_path == retry1
    assert len(job.retry_attempts) == 1
    assert job.retry_attempts[0].label == "retry1"
    assert job.retry_attempts[0].reason == "timeout"
    assert "TIMEOUT" in (job.retry_attempts[0].error_message or "")
