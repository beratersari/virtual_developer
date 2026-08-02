"""Job deletion: store + dashboard API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import delete_job_record, delete_job_records
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_job_store_delete(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    j = store.create_job(issue_key="DEL-1", summary="s", description="d")
    jid = j["job_id"]
    assert store.get_job(jid) is not None
    assert store.delete_job(jid) is True
    assert store.get_job(jid) is None
    assert store.delete_job(jid) is False
    assert store.delete_job("legacy_x") is False


def test_delete_job_record_refuses_live(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    j = store.create_job(
        issue_key="DEL-2",
        summary="s",
        description="d",
        status="running",
    )
    out = delete_job_record(j["job_id"], store=store)
    assert out["ok"] is False
    assert "Cannot delete" in out["error"]
    assert store.get_job(j["job_id"]) is not None


def test_delete_job_record_with_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / ".jira-agent" / "sessions"
    agent.mkdir(parents=True)
    log = agent / "DEL-3_20260101_000000_0.log"
    prompt = agent / "DEL-3_20260101_000000_0.prompt.txt"
    log.write_text("log")
    prompt.write_text("prompt")
    (Path(str(log) + ".session_id")).write_text("ses1")

    store = JobStore(jobs_dir=tmp_path / ".jira-agent" / "jobs")
    j = store.create_job(
        issue_key="DEL-3",
        summary="s",
        description="d",
        status="completed",
    )
    store.update_job(
        j["job_id"],
        session_log_path=str(log),
        prompt_path=str(prompt),
        status="completed",
    )

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("DEL-3", "s", "d")
    sm.update_state(
        "DEL-3",
        status=TaskStatus.COMPLETED,
        metadata={"job_ids": [j["job_id"], "job_other"], "current_job_id": j["job_id"]},
    )

    out = delete_job_record(
        j["job_id"],
        store=store,
        state_manager=sm,
        delete_artifacts=True,
    )
    assert out["ok"] is True
    assert out["store_deleted"] is True
    assert store.get_job(j["job_id"]) is None
    assert not log.is_file()
    assert not prompt.is_file()
    st = sm.get_state("DEL-3")
    assert j["job_id"] not in (st.metadata or {}).get("job_ids", [])
    assert (st.metadata or {}).get("current_job_id") == "job_other"


def test_api_delete_job(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = JobStore(jobs_dir=tmp_path / "jobs")
    j = store.create_job(
        issue_key="API-DEL",
        summary="s",
        description="d",
        status="completed",
    )
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("API-DEL", "s", "d")

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.job_store", store)
        m.setattr("src.dashboard.service.default_job_store", store)
        app = create_dashboard_app(processor=None, state_manager=sm)
        client = TestClient(app)
        r = client.delete(f"/api/jobs/{j['job_id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert store.get_job(j["job_id"]) is None

        r404 = client.delete("/api/jobs/job_missing000")
        assert r404.status_code == 404

        live = store.create_job(
            issue_key="API-LIVE",
            summary="s",
            description="d",
            status="executing",
        )
        r409 = client.delete(f"/api/jobs/{live['job_id']}")
        assert r409.status_code == 409


def test_delete_job_records_bulk(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    a = store.create_job(
        issue_key="BULK-1", summary="s", description="d", status="completed"
    )
    b = store.create_job(
        issue_key="BULK-2", summary="s", description="d", status="error"
    )
    live = store.create_job(
        issue_key="BULK-3", summary="s", description="d", status="running"
    )

    out = delete_job_records(
        [a["job_id"], b["job_id"], live["job_id"], a["job_id"], ""],
        store=store,
    )
    assert out["deleted_count"] == 2
    assert out["failed_count"] == 1
    assert a["job_id"] in out["deleted"]
    assert b["job_id"] in out["deleted"]
    assert out["failed"][0]["job_id"] == live["job_id"]
    assert store.get_job(a["job_id"]) is None
    assert store.get_job(live["job_id"]) is not None


def test_api_bulk_delete_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = JobStore(jobs_dir=tmp_path / "jobs")
    done = store.create_job(
        issue_key="API-BULK",
        summary="s",
        description="d",
        status="completed",
    )
    live = store.create_job(
        issue_key="API-BULK-LIVE",
        summary="s",
        description="d",
        status="executing",
    )
    sm = JiraStateManager(state_dir=tmp_path / "state")

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.job_store", store)
        m.setattr("src.dashboard.service.default_job_store", store)
        app = create_dashboard_app(processor=None, state_manager=sm)
        client = TestClient(app)

        r_empty = client.post("/api/jobs/bulk-delete", json={"job_ids": []})
        assert r_empty.status_code == 400

        r = client.post(
            "/api/jobs/bulk-delete",
            json={
                "job_ids": [done["job_id"], live["job_id"]],
                "delete_artifacts": True,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["deleted_count"] == 1
        assert body["failed_count"] == 1
        assert done["job_id"] in body["deleted"]
        assert store.get_job(done["job_id"]) is None
        assert store.get_job(live["job_id"]) is not None
