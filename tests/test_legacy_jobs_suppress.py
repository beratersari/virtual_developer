"""Legacy session jobs must not duplicate open JobStore runs."""

from __future__ import annotations

from pathlib import Path

from src.dashboard.service import build_jobs
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_open_job_suppresses_matching_session_legacy(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    jobs = isolate_jira_agent_artifacts["job_store"]
    # Session log created while agent runs (no link on job yet)
    log = sessions / "KAN-1_20260801_184430_0.log"
    log.write_text("partial output\n")
    (sessions / "KAN-1_20260801_184430_0.prompt.txt").write_text(
        "# Direct\n\n## Task\nhello\n\n# X\n"
    )

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
    # Job started slightly before log filename timestamp
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


def test_legacy_still_shown_for_old_unlinked_logs(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    jobs = isolate_jira_agent_artifacts["job_store"]
    old = sessions / "KAN-1_20260701_100000_0.log"
    old.write_text("old run\n")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "test", "hello")
    # completed job with different log path (covered)
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
    assert any(i.startswith("legacy_KAN-1_20260701") for i in ids), ids