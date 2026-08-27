"""Jobs list is newest-created first, not grouped by issue key."""

from __future__ import annotations

import pytest

from src.dashboard.service import build_jobs, job_created_stamp
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


def test_build_jobs_sorts_by_created_date_not_issue_key(tmp_path):
    jobs = JobStore(jobs_dir=tmp_path / "jobs")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    old = jobs.create_job(issue_key="KAN-1", summary="old same issue")
    newer_other = jobs.create_job(issue_key="KAN-9", summary="newer other issue")
    mid = jobs.create_job(issue_key="KAN-1", summary="mid same issue")
    jobs.update_job(old["job_id"], started_at="2026-08-01T10:00:00")
    jobs.update_job(newer_other["job_id"], started_at="2026-08-20T12:00:00")
    jobs.update_job(mid["job_id"], started_at="2026-08-10T09:00:00")

    listed = build_jobs(page=1, page_size=10, store=jobs, state_manager=sm)
    ids = [j.job_id for j in listed.jobs]
    assert ids == [newer_other["job_id"], mid["job_id"], old["job_id"]]
    assert job_created_stamp(listed.jobs[0]) >= job_created_stamp(listed.jobs[1])


def test_list_jobs_uses_created_at_fallback(tmp_path):
    jobs = JobStore(jobs_dir=tmp_path / "jobs")
    older = jobs.create_job(issue_key="AAA-1", summary="aaa")
    newer = jobs.create_job(issue_key="ZZZ-1", summary="zzz")
    jobs.update_job(older["job_id"], started_at=None, created_at="2026-07-01T00:00:00")
    jobs.update_job(newer["job_id"], started_at=None, created_at="2026-07-15T00:00:00")
    listed = jobs.list_jobs()
    assert listed[0]["job_id"] == newer["job_id"]
    assert listed[1]["job_id"] == older["job_id"]
