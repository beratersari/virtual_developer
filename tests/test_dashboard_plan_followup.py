"""Job page offers Implement / Revise only for the live plan."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import (
    build_jobs,
    build_one_job,
    plan_document_for_issue,
    plan_followup_for_job,
)
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def _stores(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    return sm, store


def _job(store: JobStore, key: str, *, status: str, started: str, workflow: str = "planning"):
    job = store.create_job(
        issue_key=key,
        summary=f"{status} {started}",
        workflow_type=workflow,
        status=status,
    )
    stored = store.update_job(job["job_id"], status=status, started_at=started)
    assert stored is not None
    return stored


def test_latest_plan_ready_job_offers_actions(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    latest = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T11:00:00")
    state = sm.get_state("KAN-1")

    old_view = plan_followup_for_job(older, state, store)
    new_view = plan_followup_for_job(latest, state, store)

    assert old_view is not None and new_view is not None
    assert old_view["actions"] is False
    assert old_view["kind"] == "newer"
    assert old_view["message"] == "A newer plan is the current one."
    assert old_view["job_id"] == latest["job_id"]
    assert new_view["actions"] is True
    assert new_view["kind"] == "current"


def test_old_plan_points_at_the_revision_while_it_runs(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLANNING)
    older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    running = _job(store, "KAN-1", status="planning", started="2026-09-22T12:00:00")
    sm.update_state("KAN-1", metadata={"current_job_id": running["job_id"]})

    view = plan_followup_for_job(older, sm.get_state("KAN-1"), store)
    assert view is not None
    assert view["actions"] is False
    assert view["kind"] == "revising"
    assert view["message"] == "This plan is being revised."
    assert view["job_id"] == running["job_id"]
    assert plan_followup_for_job(running, sm.get_state("KAN-1"), store) is None


def test_plan_job_hides_actions_after_implement_starts(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.EXECUTING)
    plan = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    _job(
        store,
        "KAN-1",
        status="executing",
        started="2026-09-22T13:00:00",
        workflow="execution",
    )

    view = plan_followup_for_job(plan, sm.get_state("KAN-1"), store)
    assert view is not None
    assert view["actions"] is False
    assert view["kind"] == "implemented"
    assert view["message"] == "This plan was implemented."
    assert view["job_id"] is None
    assert view["issue_key"] == "KAN-1"

    sm.update_state("KAN-1", status=TaskStatus.COMPLETED)
    done = plan_followup_for_job(plan, sm.get_state("KAN-1"), store)
    assert done is not None
    assert done["kind"] == "implemented"


def test_jobs_list_shows_plan_ready_only_on_the_latest_job(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    latest = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T11:00:00")
    other = _job(store, "KAN-9", status="plan_ready", started="2026-09-22T09:00:00")

    listed = {
        item.job_id: item.status
        for item in build_jobs(store=store, state_manager=sm, page_size=20).jobs
    }
    assert listed[older["job_id"]] == "superseded"
    assert listed[latest["job_id"]] == "plan_ready"
    assert listed[other["job_id"]] == "plan_ready"

    opened = build_one_job(older["job_id"], store=store, state_manager=sm)
    assert opened is not None
    assert opened.status == "superseded"
    current = build_one_job(latest["job_id"], store=store, state_manager=sm)
    assert current is not None
    assert current.status == "plan_ready"


def test_jobs_list_hides_plan_ready_when_a_later_run_exists(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLANNING)
    older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    running = _job(store, "KAN-1", status="planning", started="2026-09-22T12:00:00")

    listed = {
        item.job_id: item.status
        for item in build_jobs(store=store, state_manager=sm, page_size=20).jobs
    }
    assert listed[older["job_id"]] == "superseded"
    assert listed[running["job_id"]] == "planning"


def test_plan_ready_job_detail_includes_the_plan_file(tmp_path, monkeypatch):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    from src.paths import plans_dir

    plan_path = plans_dir() / "KAN-1.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Login\n\nUse Redis.\n", encoding="utf-8")
    sm.update_state("KAN-1", plan_path=str(plan_path))
    older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
    latest = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T11:00:00")

    body = plan_document_for_issue("KAN-1", sm.get_state("KAN-1"))
    assert body is not None
    assert body["missing"] is False
    assert "Use Redis." in body["text"]

    with patch("src.dashboard.api.job_store", store):
        app = create_dashboard_app(processor=None, state_manager=sm)
        client = TestClient(app)
        current = client.get(f"/api/jobs/{latest['job_id']}").json()
        previous = client.get(f"/api/jobs/{older['job_id']}").json()

    assert current["job"]["status"] == "plan_ready"
    assert "Use Redis." in current["plan"]["text"]
    assert previous["job"]["status"] == "superseded"
    assert previous["plan"] is None


def test_job_detail_includes_plan_followup(tmp_path):
    sm, store = _stores(tmp_path)
    sm.create_state("KAN-1", "plan login", "Mode: plan")
    sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
    job = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")

    with patch("src.dashboard.api.job_store", store):
        app = create_dashboard_app(processor=None, state_manager=sm)
        client = TestClient(app)
        body = client.get(f"/api/jobs/{job['job_id']}").json()

    assert body["plan_followup"]["actions"] is True
    assert body["plan_followup"]["kind"] == "current"
    assert body["issue"]["status"] == "plan_ready"
