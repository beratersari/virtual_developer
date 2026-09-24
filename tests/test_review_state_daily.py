"""Safe-outcome proofs for daily state-store bugs.

Each test asserts the durable result an operator can rely on. A failure
means current code loses or lies about that transition.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from src.state.job_index import JobIndex
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore
from src.state.schedule_store import ScheduleStore
from src.state.session_bind_store import SessionBindStore


def test_analytics_search_treats_percent_and_underscore_as_literal(tmp_path: Path):
    """Analytics q= must not treat LIKE wildcards as matching every job.

    Jobs-page search is a literal substring. A search for ``%`` or ``_``
    must not count jobs that do not contain those characters.
    """
    idx = JobIndex(tmp_path / "jobs.sqlite")
    idx.upsert(
        {
            "job_id": "job_plain",
            "issue_key": "KAN-1",
            "summary": "add login form",
            "description": "plain words only",
            "status": "completed",
            "workflow_type": "execution",
            "source": "jira",
            "agent": "derman-build",
            "model": "gpt-4.1",
            "repository_url": "https://gitlab.example.com/acme/app.git",
            "started_at": "2026-09-20T10:00:00",
        }
    )
    idx.upsert(
        {
            "job_id": "job_wild",
            "issue_key": "KAN-2",
            "summary": "100% done_now",
            "description": "literal percent and underscore",
            "status": "completed",
            "workflow_type": "execution",
            "source": "jira",
            "started_at": "2026-09-20T11:00:00",
        }
    )
    percent = idx.query_jobs(q="%")
    under = idx.query_jobs(q="_")
    assert [r["job_id"] for r in percent] == ["job_wild"]
    assert [r["job_id"] for r in under] == ["job_wild"]
    repo = idx.query_jobs(repository="100%_done")
    assert repo == []
    idx.close()


def test_create_state_does_not_return_pending_when_first_write_fails(
    tmp_path: Path, monkeypatch
):
    """A failed first create must not look owned. get_state stays empty,
    so a later poll would start a second run if the caller kept the return.
    """
    sm = JiraStateManager(state_dir=tmp_path / "state")

    def boom_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom_replace)
    monkeypatch.setattr("src.state.manager.os.replace", boom_replace)
    returned = sm.create_state("NEW-1", "first accept")
    monkeypatch.undo()
    assert sm.get_state("NEW-1") is None
    assert returned is None


def test_create_job_does_not_look_saved_when_disk_write_fails(
    tmp_path: Path, monkeypatch
):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    real = Path.replace

    def boom(self: Path, target):
        if self.name.endswith(".tmp") and "job_" in self.name:
            raise OSError("disk full")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", boom)
    job = store.create_job(issue_key="KAN-1", summary="run", status="running")
    assert job is None or store.get_job(job["job_id"]) is not None


def test_update_job_does_not_report_completed_when_disk_write_fails(
    tmp_path: Path, monkeypatch
):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(issue_key="KAN-1", summary="run", status="running")
    real = Path.replace

    def boom(self: Path, target):
        if self.name.endswith(".tmp") and job["job_id"] in self.name:
            raise OSError("disk full")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", boom)
    returned = store.update_job(job["job_id"], status="completed")
    disk = store.get_job(job["job_id"])
    assert disk is not None
    assert disk["status"] == "running"
    assert returned is None or returned["status"] == disk["status"]


def test_queue_finish_does_not_overwrite_cancelled(tmp_path: Path):
    """Stop writes cancelled. A late worker finish must not mark it completed."""
    qs = WorkQueueStore(queue_dir=tmp_path / "queue")
    rec = qs.enqueue(source="jira", issue_key="KAN-9", summary="run")
    claimed = qs.claim_next(max_running=4)
    assert claimed is not None
    qs.finish(claimed["queue_id"], status="cancelled", error_message="stop")
    late = qs.finish(claimed["queue_id"], status="completed")
    disk = qs.get(rec["queue_id"])
    assert disk is not None
    assert disk["status"] == "cancelled"
    assert late is None or late["status"] == "cancelled"


def test_job_index_status_matches_json_after_upsert_error(tmp_path: Path):
    """Daemon already reconciled. A later index upsert failure must not
    leave Analytics/Jobs on the old status while JSON says completed.
    """
    store = JobStore(jobs_dir=tmp_path / "jobs")
    store.ensure_index()
    job = store.create_job(issue_key="KAN-4", summary="run", status="running")

    def boom(_job):
        raise sqlite3.OperationalError("database is locked")

    assert store._index is not None
    store._index.upsert = boom  # type: ignore[method-assign]
    store.update_job(job["job_id"], status="completed")
    disk = store.get_job(job["job_id"])
    assert disk is not None and disk["status"] == "completed"
    rows = store.query_jobs(status="completed")
    assert any(r["job_id"] == job["job_id"] for r in rows)


def test_schedule_open_flag_matches_json_after_upsert_error(tmp_path: Path):
    """Poller skips intake while has_open_for_issue is true. After dispatch
    the JSON is terminal, so the flag must be false even if the index write
    failed (daemon already ran ensure_index).
    """
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    store.ensure_index()
    rec = store.create(
        title="later",
        description="do it",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2020-01-01T00:00:00",
        issue_key="KAN-7",
        issue_description="body",
    )
    claimed = store.claim_due(rec["schedule_id"])
    assert claimed is not None and claimed["status"] == "dispatching"

    def boom(_rec):
        raise sqlite3.OperationalError("database is locked")

    assert store._index is not None
    store._index.upsert = boom  # type: ignore[method-assign]
    updated = store.update(
        rec["schedule_id"],
        expected_status="dispatching",
        status="dispatched",
    )
    assert updated is not None
    disk = store.get(rec["schedule_id"])
    assert disk is not None and disk["status"] == "dispatched"
    assert store.has_open_for_issue("KAN-7") is False
    assert store.count_schedules(status="dispatched") == 1
    assert store.count_schedules(status="scheduled") == 0


def test_session_list_matches_json_after_upsert_error(tmp_path: Path):
    """Sessions page reads the index. After a failed upsert the listed
    session id must still be the one just written to JSON.
    """
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.ensure_index()
    first = store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/login",
        target_branch="develop",
        session_id="ses_old",
        issue_key="KAN-3",
        kind="build",
    )
    assert first is not None

    def boom(_rec):
        raise sqlite3.OperationalError("database is locked")

    assert store._index is not None
    store._index.upsert = boom  # type: ignore[method-assign]
    second = store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/login",
        target_branch="develop",
        session_id="ses_new",
        issue_key="KAN-3",
        kind="build",
    )
    assert second is not None and second["session_id"] == "ses_new"
    listed = store.list_binds(limit=None)
    ids = {r["bind_id"]: r["session_id"] for r in listed}
    assert ids.get(second["bind_id"]) == "ses_new"
