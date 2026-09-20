"""GET /api/analytics — real HTTP, real job JSON files."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager


def _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch):
    jobs: JobStore = isolate_jira_agent_artifacts["job_store"]
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", jobs)
    monkeypatch.setattr("src.state.job_store.job_store", jobs)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(state_manager=sm)
    return TestClient(app), jobs


def _stamp(days_ago: int, hour: int = 10) -> str:
    dt = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    dt = dt - timedelta(days=days_ago)
    return dt.isoformat(timespec="seconds")


def test_analytics_empty_store(tmp_path, isolate_jira_agent_artifacts, monkeypatch):
    http, _jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    r = http.get("/api/analytics", params={"period": "7d"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["jobs"] == 0
    assert body["matched"] == 0
    assert isinstance(body["series"], list)


def test_analytics_counts_and_filters(tmp_path, isolate_jira_agent_artifacts, monkeypatch):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    a = jobs.create_job(
        issue_key="KAN-1",
        summary="plan login",
        workflow_type="planning",
        source="jira",
        model="gpt-4.1",
        backend="opencode",
        status="completed",
    )
    jobs.update_job(
        a["job_id"], started_at=_stamp(2), status="completed", completed_at=_stamp(2, 12)
    )
    b = jobs.create_job(
        issue_key="KAN-2",
        summary="build login",
        workflow_type="execution",
        source="jira",
        model="gpt-4.1",
        backend="opencode",
        status="error",
    )
    jobs.update_job(b["job_id"], started_at=_stamp(1), status="error")
    c = jobs.create_job(
        issue_key="KAN-3",
        summary="review mr",
        workflow_type="review",
        source="gitlab",
        model="sonnet",
        backend="opencode",
        status="completed",
        repository_url="https://gitlab.example.com/acme/app.git",
    )
    jobs.update_job(c["job_id"], started_at=_stamp(1, 14), status="completed")
    d = jobs.create_job(
        issue_key="KAN-4",
        summary="old",
        workflow_type="execution",
        source="jira",
        model="gpt-4.1",
        status="completed",
    )
    jobs.update_job(d["job_id"], started_at=_stamp(40), status="completed")

    all7 = http.get("/api/analytics", params={"period": "7d", "bucket": "day"})
    assert all7.status_code == 200, all7.text
    body = all7.json()
    assert body["totals"]["jobs"] == 3
    assert body["totals"]["completed"] == 2
    assert body["totals"]["error"] == 1
    cats = {row["id"]: row["jobs"] for row in body["categories"]}
    assert cats.get("plan") == 1
    assert cats.get("build") == 1
    assert cats.get("review") == 1
    models = {row["id"]: row["jobs"] for row in body["models"]}
    assert models.get("gpt-4.1") == 2
    assert models.get("sonnet") == 1
    assert sum(p["total"] for p in body["series"]) == 3
    plan_row = next(r for r in body["categories"] if r["id"] == "plan")
    assert plan_row["share"] == 33.3
    assert body["totals"]["share"] == 100.0

    only_err = http.get("/api/analytics", params={"period": "7d", "status": "error"})
    assert only_err.json()["totals"]["jobs"] == 1
    assert only_err.json()["totals"]["error"] == 1

    only_done = http.get(
        "/api/analytics", params={"period": "7d", "status": "completed"}
    )
    assert only_done.json()["totals"]["jobs"] == 2
    assert only_done.json()["totals"]["completed"] == 2

    only_plan = http.get("/api/analytics", params={"period": "7d", "category": "plan"})
    assert only_plan.json()["totals"]["jobs"] == 1
    assert only_plan.json()["totals"]["completed"] == 1

    gitlab = http.get("/api/analytics", params={"period": "7d", "source": "gitlab"})
    assert gitlab.json()["totals"]["jobs"] == 1

    model = http.get("/api/analytics", params={"period": "7d", "model": "sonnet"})
    assert model.json()["totals"]["jobs"] == 1

    repo = http.get(
        "/api/analytics",
        params={"period": "7d", "repository": "acme/app"},
    )
    assert repo.json()["totals"]["jobs"] == 1

    key = http.get("/api/analytics", params={"period": "7d", "issue_key": "KAN-2"})
    assert key.json()["totals"]["jobs"] == 1

    year = http.get("/api/analytics", params={"period": "90d"})
    assert year.json()["totals"]["jobs"] == 4

    facets = body["facets"]
    assert any(f["id"] == "gpt-4.1" for f in facets["model"])
    assert any(f["id"] == "plan" for f in facets["category"])


def test_analytics_omits_unset_model_series(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Jobs with no model still count in totals, not as a fake model row."""
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    named = jobs.create_job(
        issue_key="KAN-1",
        summary="named",
        workflow_type="execution",
        model="gpt-4.1",
        status="completed",
    )
    jobs.update_job(named["job_id"], started_at=_stamp(1), status="completed")
    blank = jobs.create_job(
        issue_key="KAN-2",
        summary="no model",
        workflow_type="execution",
        status="error",
    )
    jobs.update_job(blank["job_id"], started_at=_stamp(1), status="error", model=None)

    body = http.get("/api/analytics", params={"period": "7d"}).json()
    assert body["totals"]["jobs"] == 2
    ids = [row["id"] for row in body["models"]]
    assert ids == ["gpt-4.1"]
    assert "(unset)" not in ids
    assert "(unset)" not in body["model_keys"]
    assert all("(unset)" not in (p.get("counts") or {}) for p in body["model_series"])
    facet_ids = [f["id"] for f in body["facets"]["model"]]
    assert "(unset)" not in facet_ids
    gpt = next(r for r in body["models"] if r["id"] == "gpt-4.1")
    assert gpt["share"] == 50.0


def test_analytics_24h_month_bucket_stays_small(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """24 hours + Month must not explode into a huge series / hang the GET."""
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = jobs.create_job(
        issue_key="KAN-24",
        summary="today",
        workflow_type="execution",
        status="completed",
    )
    started = (datetime.now() - timedelta(hours=1)).replace(microsecond=0)
    jobs.update_job(
        rec["job_id"],
        started_at=started.isoformat(timespec="seconds"),
        status="completed",
    )
    r = http.get("/api/analytics", params={"period": "24h", "bucket": "month"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"]["bucket"] == "month"
    assert 1 <= len(body["series"]) <= 3
    assert body["totals"]["jobs"] == 1


def test_analytics_custom_from_to(tmp_path, isolate_jira_agent_artifacts, monkeypatch):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = jobs.create_job(
        issue_key="KAN-9",
        summary="mid",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(rec["job_id"], started_at=_stamp(3), status="completed")
    start = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%dT00:00:00")
    end = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT23:59:59")
    r = http.get("/api/analytics", params={"from": start, "to": end, "bucket": "day"})
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["jobs"] == 1
