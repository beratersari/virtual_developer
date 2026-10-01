"""One yaver.sqlite holds jobs, schedules, binds, issue state, and the queue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore
from src.state.record_db import database_path
from src.state.schedule_store import ScheduleStore
from src.state.session_bind_store import SessionBindStore


def test_standard_dirs_share_the_parent_database(tmp_path: Path):
    root = tmp_path / "yaver"
    for name in ("jobs", "schedules", "opencode-binds", "queue", "state"):
        assert database_path(root / name) == root / "yaver.sqlite"
    assert database_path(tmp_path / "binds") == tmp_path / "binds" / "yaver.sqlite"


def test_records_live_in_one_database_and_json_is_imported_once(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "runtime"
    jobs = root / "jobs"
    schedules = root / "schedules"
    binds = root / "opencode-binds"
    queue = root / "queue"
    state = root / "state"
    plans = root / "plans"
    for folder in (jobs, schedules, binds, queue, state, plans):
        folder.mkdir(parents=True)
    (plans / "KAN-1.md").write_text("# plan\n", encoding="utf-8")
    (jobs / "job_legacy01.json").write_text(
        json.dumps(
            {
                "job_id": "job_legacy01",
                "issue_key": "KAN-1",
                "summary": "eski iş",
                "status": "completed",
                "created_at": "2026-01-02T00:00:00",
                "deliveries": [
                    {"merge_request_url": "https://gitlab.example/mr/9"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (jobs / "job_bad.json").write_text("{", encoding="utf-8")
    (jobs / "job_skip.json").write_text(
        json.dumps({"job_id": "plain", "summary": "not a job"}),
        encoding="utf-8",
    )
    (schedules / "sched_legacy01.json").write_text(
        json.dumps(
            {
                "schedule_id": "sched_legacy01",
                "title": "later",
                "status": "scheduled",
                "issue_key": "KAN-1",
            }
        ),
        encoding="utf-8",
    )
    (binds / "osb_legacy01.json").write_text(
        json.dumps(
            {
                "bind_id": "osb_legacy01",
                "session_id": "ses_old",
                "repository_url": "https://gitlab.example/acme/app.git",
                "branch": "feature/login",
                "target_branch": "develop",
            }
        ),
        encoding="utf-8",
    )
    (queue / "q_legacy01.json").write_text(
        json.dumps(
            {
                "queue_id": "q_legacy01",
                "status": "completed",
                "issue_key": "KAN-1",
                "source": "jira",
                "created_at": "2026-01-01T00:00:00.000",
            }
        ),
        encoding="utf-8",
    )
    (state / "KAN_12.json").write_text(
        json.dumps(
            {
                "issue_key": "KAN-12",
                "issue_summary": "loser",
                "status": "pending",
                "metadata": {},
                "retry_history": [],
            }
        ),
        encoding="utf-8",
    )
    (state / "KAN-12.json").write_text(
        json.dumps(
            {
                "issue_key": "KAN-12",
                "issue_summary": "winner",
                "status": "completed",
                "metadata": {},
                "retry_history": [],
            }
        ),
        encoding="utf-8",
    )

    job_store = JobStore(jobs_dir=jobs)
    schedule_store = ScheduleStore(schedules_dir=schedules)
    bind_store = SessionBindStore(binds_dir=binds)
    queue_store = WorkQueueStore(queue_dir=queue)
    states = JiraStateManager(state_dir=state)

    assert job_store.ensure_index() == 1
    assert schedule_store.ensure_index() == 1
    assert bind_store.ensure_index() == 1
    assert queue_store.get("q_legacy01")["status"] == "completed"
    loaded = states.get_state("KAN-12")
    assert loaded is not None
    assert loaded.issue_key == "KAN-12"
    assert loaded.issue_summary == "winner"
    assert loaded.status == TaskStatus.COMPLETED

    assert job_store.ensure_index() == 0
    assert schedule_store.ensure_index() == 0
    assert bind_store.ensure_index() == 0
    assert not (jobs / "job_legacy01.json").is_file()
    assert (jobs / "job_bad.json").is_file()
    assert (jobs / "job_skip.json").is_file()
    assert not (schedules / "sched_legacy01.json").is_file()
    assert not (binds / "osb_legacy01.json").is_file()
    assert not (queue / "q_legacy01.json").is_file()
    assert not (state / "KAN-12.json").is_file()
    assert not (state / "KAN_12.json").is_file()
    assert (plans / "KAN-1.md").read_text(encoding="utf-8") == "# plan\n"

    job = job_store.get_job("job_legacy01")
    assert job is not None
    assert job["summary"] == "eski iş"
    assert job["deliveries"][0]["merge_request_url"] == "https://gitlab.example/mr/9"
    assert schedule_store.get("sched_legacy01")["title"] == "later"
    assert bind_store.get_by_id("osb_legacy01")["session_id"] == "ses_old"

    created = job_store.create_job(issue_key="KAN-2", summary="yeni", status="running")
    assert created is not None
    schedule_store.create(
        title="fresh",
        description="",
        repository_url="https://gitlab.example/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2099-01-01T00:00:00",
        issue_key="KAN-2",
        issue_description="",
    )
    bind_store.upsert(
        repository_url="https://gitlab.example/acme/app.git",
        branch="feature/login",
        target_branch="develop",
        session_id="ses_new",
        issue_key="KAN-2",
        kind="build",
    )
    queue_store.enqueue(source="jira", issue_key="KAN-2", summary="wait")
    states.create_state("KAN-3", "fresh")
    assert set(jobs.glob("job_*.json")) == {
        jobs / "job_bad.json",
        jobs / "job_skip.json",
    }
    assert list(schedules.glob("sched_*.json")) == []
    assert list(binds.glob("osb_*.json")) == []
    assert list(queue.glob("q_*.json")) == []
    assert list(state.glob("*.json")) == []

    db = root / "yaver.sqlite"
    assert db.is_file()
    for name in (
        "jobs.sqlite",
        "schedules.sqlite",
        "opencode-binds.sqlite",
    ):
        assert not (root / name).is_file()
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "jobs",
        "schedules",
        "session_binds",
        "issue_states",
        "queue_items",
    } <= tables
    assert "plans" not in tables

    (jobs / "job_legacy01.json").write_text(
        json.dumps({"job_id": "job_legacy01", "summary": "replaced"}),
        encoding="utf-8",
    )
    job_store._index_ready = False
    calls: list[str] = []
    real_glob = Path.glob

    def spy(self: Path, pattern: str):
        if pattern == "job_*.json":
            calls.append(str(self))
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", spy)
    assert job_store.ensure_index() == 0
    assert calls == []
    assert job_store.get_job("job_legacy01")["summary"] == "eski iş"
    assert (jobs / "job_legacy01.json").is_file()


def test_dashboard_service_import_captures_the_test_store(
    isolate_jira_agent_artifacts,
):
    import src.dashboard.service as dash_svc

    assert dash_svc.default_job_store is isolate_jira_agent_artifacts["job_store"]


def test_pytest_isolation_does_not_alias_the_operator_stores(
    isolate_jira_agent_artifacts,
):
    """Dashboard and processor imports must follow the per-test stores."""
    import src.dashboard.analytics as analytics
    import src.dashboard.api as api
    import src.dashboard.service as dash_svc
    import src.processor as processor_mod
    import src.scheduler.service as sched_svc
    from src.config import settings

    runtime = isolate_jira_agent_artifacts["runtime"].resolve()
    assert api.job_store is isolate_jira_agent_artifacts["job_store"]
    assert api.schedule_store is isolate_jira_agent_artifacts["schedule_store"]
    assert processor_mod.job_store is isolate_jira_agent_artifacts["job_store"]
    assert processor_mod.work_queue_store is isolate_jira_agent_artifacts["queue_store"]
    assert sched_svc.schedule_store is isolate_jira_agent_artifacts["schedule_store"]
    assert dash_svc.default_job_store is isolate_jira_agent_artifacts["job_store"]
    assert analytics.default_job_store is isolate_jira_agent_artifacts["job_store"]
    assert Path(settings.state_dir).resolve() == (runtime / "state").resolve()
    assert runtime in Path(api.job_store.jobs_dir).resolve().parents


def test_custom_folder_keeps_its_own_database(tmp_path: Path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    rec = store.upsert(
        repository_url="https://gitlab.example/acme/app.git",
        branch="feature/login",
        target_branch="develop",
        session_id="ses_custom",
        issue_key="KAN-1",
        kind="build",
    )
    assert rec is not None
    assert (tmp_path / "binds" / "yaver.sqlite").is_file()
    assert not (tmp_path / "yaver.sqlite").is_file()
    assert list((tmp_path / "binds").glob("osb_*.json")) == []
