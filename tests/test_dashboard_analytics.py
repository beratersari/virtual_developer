"""GET /api/analytics — real HTTP, job JSON plus local jobs.sqlite index."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytest
import uvicorn
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

    sqlite = isolate_jira_agent_artifacts["jobs_dir"].parent / "jobs.sqlite"
    assert sqlite.is_file()
    assert jobs.count_jobs() == 4

    all7 = http.get("/api/analytics", params={"period": "7d", "bucket": "day"})
    assert all7.status_code == 200, all7.text
    body = all7.json()
    assert body["in_range"] == 3
    assert body["scanned"] == 3
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
    assert year.json()["scanned"] == 4


def test_analytics_sql_does_not_walk_iter_jobs(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = jobs.create_job(
        issue_key="KAN-sql",
        summary="indexed",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(rec["job_id"], started_at=_stamp(1), status="completed")

    def _boom():
        raise AssertionError("iter_jobs should not run when SQLite WHERE is used")

    jobs.iter_jobs = _boom  # type: ignore[method-assign]
    r = http.get("/api/analytics", params={"period": "7d", "status": "completed"})
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["jobs"] == 1
    assert r.json()["scanned"] == 1


def test_analytics_unset_model_is_in_table_not_series(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Jobs with no model appear in the table/facet so shares sum to 100%.

    They still must not appear on model_series / model_keys (no fake line).
    """
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
    assert ids == ["gpt-4.1", "(unset)"]
    assert "(unset)" not in body["model_keys"]
    assert all("(unset)" not in (p.get("counts") or {}) for p in body["model_series"])
    facet_ids = [f["id"] for f in body["facets"]["model"]]
    assert "(unset)" in facet_ids
    gpt = next(r for r in body["models"] if r["id"] == "gpt-4.1")
    unset = next(r for r in body["models"] if r["id"] == "(unset)")
    assert gpt["share"] == 50.0
    assert unset["share"] == 50.0
    assert gpt["share"] + unset["share"] == 100.0


def test_analytics_plan_ready_is_its_own_outcome(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    ready = jobs.create_job(
        issue_key="KAN-10",
        summary="plan wait",
        workflow_type="planning",
        model="gpt-4.1",
        status="plan_ready",
        agent="derman-plan",
    )
    jobs.update_job(ready["job_id"], started_at=_stamp(1), status="plan_ready")
    done = jobs.create_job(
        issue_key="KAN-11",
        summary="built",
        workflow_type="execution",
        model="gpt-4.1",
        status="completed",
        agent="derman-build",
    )
    jobs.update_job(done["job_id"], started_at=_stamp(1), status="completed")

    body = http.get("/api/analytics", params={"period": "7d"}).json()
    totals = body["totals"]
    assert totals["jobs"] == 2
    assert totals["completed"] == 1
    assert totals["plan_ready"] == 1
    assert totals["error"] == 0
    assert (
        totals["completed"]
        + totals["error"]
        + totals["cancelled"]
        + totals["plan_ready"]
        + totals["in_flight"]
        == totals["jobs"]
    )
    assert sum(p["plan_ready"] for p in body["series"]) == 1
    agents = {row["id"]: row["jobs"] for row in body["agents"]}
    assert agents.get("derman-plan") == 1
    assert agents.get("derman-build") == 1
    only_ready = http.get(
        "/api/analytics", params={"period": "7d", "status": "plan_ready"}
    ).json()
    assert only_ready["totals"]["jobs"] == 1
    assert only_ready["totals"]["plan_ready"] == 1


def test_analytics_merges_repo_urls_with_and_without_git_suffix(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    a = jobs.create_job(
        issue_key="KAN-20",
        summary="with git",
        workflow_type="execution",
        status="completed",
        repository_url="https://gitlab.com/acme/app.git",
    )
    jobs.update_job(a["job_id"], started_at=_stamp(1), status="completed")
    b = jobs.create_job(
        issue_key="KAN-21",
        summary="no git",
        workflow_type="execution",
        status="error",
        repository_url="https://gitlab.com/acme/app",
    )
    jobs.update_job(b["job_id"], started_at=_stamp(1), status="error")

    body = http.get("/api/analytics", params={"period": "7d"}).json()
    repos = body["facets"]["repository"]
    assert len(repos) == 1
    assert repos[0]["jobs"] == 2
    assert repos[0]["id"] == "https://gitlab.com/acme/app"
    filtered = http.get(
        "/api/analytics",
        params={"period": "7d", "repository": "https://gitlab.com/acme/app.git"},
    ).json()
    assert filtered["totals"]["jobs"] == 2


def test_analytics_bucket_follows_the_range(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """The chart step is chosen from the time range. A bucket query is ignored."""
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
    day = http.get("/api/analytics", params={"period": "24h", "bucket": "month"})
    assert day.status_code == 200, day.text
    body = day.json()
    assert body["range"]["bucket"] == "hour"
    assert 1 <= len(body["series"]) <= 48
    assert body["totals"]["jobs"] == 1
    month = http.get("/api/analytics", params={"period": "30d", "bucket": "hour"})
    assert month.status_code == 200, month.text
    assert month.json()["range"]["bucket"] == "day"
    assert len(month.json()["series"]) <= 40


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


def test_analytics_cancel_stops_before_reading_jobs(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """A disconnected / superseded GET must not keep walking the store."""
    from src.dashboard.analytics import AnalyticsCancelled, build_analytics

    _http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = jobs.create_job(
        issue_key="KAN-cancel",
        summary="walk me",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(rec["job_id"], started_at=_stamp(1), status="completed")
    ev = threading.Event()
    ev.set()
    with pytest.raises(AnalyticsCancelled):
        build_analytics(period="7d", store=jobs, cancel=ev)


def test_analytics_cancel_after_iter_jobs_raises(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    from src.dashboard.analytics import AnalyticsCancelled, build_analytics

    _http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    rec = jobs.create_job(
        issue_key="KAN-cancel-2",
        summary="walk me",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(rec["job_id"], started_at=_stamp(1), status="completed")
    indexed = jobs.iter_jobs()
    assert indexed and indexed[0]["issue_key"] == "KAN-cancel-2"
    ev = threading.Event()
    real_query = jobs.query_jobs

    def _query_then_cancel(**kwargs):
        rows = real_query(**kwargs)
        ev.set()
        return rows

    jobs.query_jobs = _query_then_cancel  # type: ignore[method-assign]
    with pytest.raises(AnalyticsCancelled):
        build_analytics(period="7d", store=jobs, cancel=ev)


def test_analytics_issue_key_is_exact_not_substring(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """KAN-24 must not pull KAN-240. Search (q) still contains."""
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    a = jobs.create_job(
        issue_key="KAN-24",
        summary="short key",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(a["job_id"], started_at=_stamp(1), status="completed")
    b = jobs.create_job(
        issue_key="KAN-240",
        summary="longer key",
        workflow_type="execution",
        status="error",
    )
    jobs.update_job(b["job_id"], started_at=_stamp(1), status="error")
    exact = http.get("/api/analytics", params={"period": "7d", "issue_key": "KAN-24"})
    assert exact.status_code == 200, exact.text
    assert exact.json()["totals"]["jobs"] == 1
    assert exact.json()["totals"]["completed"] == 1
    both = http.get(
        "/api/analytics", params={"period": "7d", "issue_key": "KAN-24,KAN-240"}
    )
    assert both.json()["totals"]["jobs"] == 2
    contains = http.get("/api/analytics", params={"period": "7d", "q": "KAN-24"})
    assert contains.json()["totals"]["jobs"] == 2


def test_analytics_reviews_count_unique_mrs(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Open / merged / closed are unique GitLab MRs and Azure PRs, not jobs."""
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    empty = http.get("/api/analytics", params={"period": "7d"}).json()["reviews"]
    zeros = {"opened": 0, "merged": 0, "closed": 0, "total": 0}
    assert empty == {"ours": zeros, "contributed": zeros}

    open_a = jobs.create_job(
        issue_key="KAN-1",
        summary="first push",
        workflow_type="execution",
        source="jira",
        status="completed",
        merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/4",
        gitlab_project="acme/app",
        gitlab_mr_iid=4,
    )
    jobs.update_job(
        open_a["job_id"],
        started_at=_stamp(1),
        status="completed",
        merge_request_state="opened",
    )
    open_follow = jobs.create_job(
        issue_key="KAN-1",
        summary="follow-up on same MR",
        workflow_type="execution",
        source="gitlab",
        status="completed",
        merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/4",
        gitlab_project="acme/app",
        gitlab_mr_iid=4,
    )
    jobs.update_job(
        open_follow["job_id"],
        started_at=_stamp(1, 11),
        status="completed",
        merge_request_state="opened",
    )
    merged = jobs.create_job(
        issue_key="KAN-2",
        summary="shipped",
        workflow_type="execution",
        source="jira",
        status="completed",
        merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/9",
        gitlab_project="acme/app",
        gitlab_mr_iid=9,
    )
    jobs.update_job(
        merged["job_id"],
        started_at=_stamp(1, 12),
        status="completed",
        merge_request_state="merged",
    )
    closed = jobs.create_job(
        issue_key="KAN-3",
        summary="dropped",
        workflow_type="execution",
        source="jira",
        status="cancelled",
        merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/11",
        gitlab_project="acme/app",
        gitlab_mr_iid=11,
    )
    jobs.update_job(
        closed["job_id"],
        started_at=_stamp(1, 13),
        status="cancelled",
        merge_request_state="closed",
    )
    azure = jobs.create_job(
        issue_key="KAN-4",
        summary="tfs pr",
        workflow_type="execution",
        source="azure",
        status="completed",
        merge_request_url="https://tfs.example.com/tfs/DefaultCollection/App/_git/app/pullrequest/7",
        azure_project="App",
        azure_pr_id=7,
    )
    jobs.update_job(
        azure["job_id"],
        started_at=_stamp(1, 14),
        status="completed",
        merge_request_state="completed",
    )
    no_mr = jobs.create_job(
        issue_key="KAN-5",
        summary="plan only",
        workflow_type="planning",
        source="jira",
        status="plan_ready",
    )
    jobs.update_job(no_mr["job_id"], started_at=_stamp(1, 15), status="plan_ready")

    body = http.get("/api/analytics", params={"period": "7d"}).json()
    reviews = body["reviews"]
    # Jira jobs opened !4, !9, !11. GitLab follow-up on !4 does not duplicate.
    # Azure PR comment on PR 7 is contributed (already open).
    assert reviews["ours"] == {
        "opened": 1,
        "merged": 1,
        "closed": 1,
        "total": 3,
    }, reviews
    assert reviews["contributed"] == {
        "opened": 0,
        "merged": 1,
        "closed": 0,
        "total": 1,
    }, reviews
    assert body["totals"]["jobs"] == 6

    later = jobs.update_job(
        open_follow["job_id"],
        merge_request_state="merged",
    )
    assert later is not None
    after = http.get("/api/analytics", params={"period": "7d"}).json()["reviews"]
    assert after["ours"]["opened"] == 0, after
    assert after["ours"]["merged"] == 2, after
    assert after["ours"]["closed"] == 1, after
    assert after["ours"]["total"] == 3, after
    assert after["contributed"]["merged"] == 1, after

    listed = http.get(
        "/api/analytics/reviews",
        params={"period": "7d", "state": "opened", "origin": "ours"},
    )
    assert listed.status_code == 200, listed.text
    open_body = listed.json()
    assert open_body["state"] == "opened"
    assert open_body["origin"] == "ours"
    assert open_body["total"] == 0

    ours_merged = http.get(
        "/api/analytics/reviews",
        params={"period": "7d", "state": "merged", "origin": "ours"},
    ).json()
    assert ours_merged["total"] == 2, ours_merged
    urls = [row["url"] for row in ours_merged["items"]]
    assert urls.count(
        "https://gitlab.example.com/acme/app/-/merge_requests/4"
    ) == 1, urls
    same = [
        row
        for row in ours_merged["items"]
        if row["url"].endswith("/merge_requests/4")
    ]
    assert same and same[0]["jobs"] == 2
    assert same[0]["origin"] == "ours"
    assert {row["state"] for row in ours_merged["items"]} == {"merged"}

    contrib = http.get(
        "/api/analytics/reviews",
        params={"period": "7d", "origin": "contributed"},
    ).json()
    assert contrib["total"] == 1
    assert contrib["items"][0]["url"].endswith("pullrequest/7")
    assert contrib["items"][0]["origin"] == "contributed"

    all_list = http.get("/api/analytics/reviews", params={"period": "7d"}).json()
    assert all_list["total"] == 4
    assert all_list["state"] == "all"
    bad = http.get("/api/analytics/reviews", params={"state": "nope"})
    assert bad.status_code == 400


def test_analytics_in_flight_and_combined_filters(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    running = jobs.create_job(
        issue_key="KAN-1",
        summary="live build",
        workflow_type="execution",
        source="jira",
        status="running",
        model="gpt-4.1",
        backend="opencode",
        agent="derman-build",
    )
    jobs.update_job(running["job_id"], started_at=_stamp(1), status="running")
    planning = jobs.create_job(
        issue_key="KAN-2",
        summary="live plan",
        workflow_type="planning",
        source="jira",
        status="planning",
        model="gpt-4.1",
        agent="derman-plan",
    )
    jobs.update_job(planning["job_id"], started_at=_stamp(1), status="planning")
    azure = jobs.create_job(
        issue_key="AZ-9",
        summary="azure pr",
        workflow_type="azure-pr",
        source="azure_pr",
        status="completed",
        model="gpt-4.1",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/App/_git/app",
    )
    jobs.update_job(azure["job_id"], started_at=_stamp(1), status="completed")
    gl = jobs.create_job(
        issue_key="GL-1",
        summary="gitlab mr",
        workflow_type="gitlab_mr",
        source="gitlab_mr",
        status="error",
        model="sonnet",
    )
    jobs.update_job(gl["job_id"], started_at=_stamp(1), status="error")

    body = http.get("/api/analytics", params={"period": "7d"}).json()
    assert body["totals"]["jobs"] == 4
    assert body["totals"]["in_flight"] == 2
    assert body["totals"]["completed"] == 1
    assert body["totals"]["error"] == 1
    assert (
        body["totals"]["completed"]
        + body["totals"]["error"]
        + body["totals"]["cancelled"]
        + body["totals"]["plan_ready"]
        + body["totals"]["in_flight"]
        == body["totals"]["jobs"]
    )
    inflight = http.get(
        "/api/analytics", params={"period": "7d", "status": "in_flight"}
    ).json()
    assert inflight["totals"]["jobs"] == 2
    assert inflight["totals"]["in_flight"] == 2
    azure_only = http.get(
        "/api/analytics", params={"period": "7d", "source": "azure"}
    ).json()
    assert azure_only["totals"]["jobs"] == 1
    assert azure_only["totals"]["completed"] == 1
    build = http.get(
        "/api/analytics", params={"period": "7d", "category": "build"}
    ).json()
    assert build["totals"]["jobs"] == 3
    gl_build = http.get(
        "/api/analytics",
        params={"period": "7d", "category": "build", "source": "gitlab"},
    ).json()
    assert gl_build["totals"]["jobs"] == 1
    assert gl_build["totals"]["error"] == 1
    and_zero = http.get(
        "/api/analytics",
        params={"period": "7d", "status": "error", "category": "plan"},
    ).json()
    assert and_zero["totals"]["jobs"] == 0


def test_analytics_excludes_jobs_without_timestamps(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    import json

    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    dated = jobs.create_job(
        issue_key="KAN-1",
        summary="dated",
        workflow_type="execution",
        status="completed",
    )
    jobs.update_job(dated["job_id"], started_at=_stamp(1), status="completed")
    # Store writes always stamp updated_at; a timestamp-less file is the
    # only way a job can vanish from Analytics.
    (jobs.jobs_dir / "job_notime0001.json").write_text(
        json.dumps(
            {
                "job_id": "job_notime0001",
                "issue_key": "KAN-2",
                "summary": "no time",
                "workflow_type": "execution",
                "status": "error",
                "started_at": None,
                "updated_at": None,
            }
        ),
        encoding="utf-8",
    )
    body = http.get("/api/analytics", params={"period": "7d"}).json()
    assert body["totals"]["jobs"] == 1
    assert body["scanned"] == 1
    assert body["totals"]["completed"] == 1


def test_analytics_all_hour_coarsens_long_span(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    http, jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    old = jobs.create_job(
        issue_key="KAN-old",
        summary="ancient",
        workflow_type="execution",
        status="completed",
    )
    started = (datetime.now() - timedelta(days=800)).replace(microsecond=0)
    jobs.update_job(
        old["job_id"],
        started_at=started.isoformat(timespec="seconds"),
        status="completed",
    )
    r = http.get("/api/analytics", params={"period": "all"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["range"]["bucket"] == "month"
    assert len(body["series"]) <= 400
    assert body["totals"]["jobs"] == 1


def test_analytics_http_timeout_returns_504(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """A stuck aggregation must fail closed instead of hanging the SPA."""
    http, _jobs = _client(tmp_path, isolate_jira_agent_artifacts, monkeypatch)

    def _hang(**kwargs):
        cancel = kwargs.get("cancel")
        if cancel is not None:
            cancel.wait(timeout=2)
        else:
            time.sleep(2)
        raise AssertionError("analytics hang should have been cut off")

    monkeypatch.setattr("src.dashboard.analytics.build_analytics", _hang)
    monkeypatch.setattr("src.dashboard.analytics.ANALYTICS_TIMEOUT_SECONDS", 0.05)
    r = http.get("/api/analytics", params={"period": "7d"})
    assert r.status_code == 504, r.text
    assert "timed out" in str(r.json().get("detail", "")).lower()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=0.5, verify=False)
            if resp.status_code < 500:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"{url} not ready: {last}")


def test_uvicorn_analytics_and_jobs_after_json_backfill(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Old job JSON only → new store backfills sqlite → live Analytics + Jobs."""
    jobs_dir = isolate_jira_agent_artifacts["jobs_dir"]
    now = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    records = [
        {
            "job_id": "job_e2ea0001",
            "issue_key": "KAN-101",
            "summary": "plan login",
            "description": "write the plan",
            "status": "completed",
            "workflow_type": "planning",
            "source": "jira",
            "model": "gpt-4.1",
            "backend": "opencode",
            "started_at": now,
            "updated_at": now,
            "completed_at": now,
        },
        {
            "job_id": "job_e2ea0002",
            "issue_key": "KAN-102",
            "summary": "build login",
            "status": "error",
            "workflow_type": "execution",
            "source": "jira",
            "model": "gpt-4.1",
            "started_at": now,
            "updated_at": now,
        },
    ]
    for rec in records:
        (jobs_dir / f"{rec['job_id']}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )

    store = JobStore(jobs_dir=jobs_dir)
    assert store.ensure_index() == 2
    sqlite = jobs_dir.parent / "jobs.sqlite"
    assert sqlite.is_file()
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", store)
    monkeypatch.setattr("src.state.job_store.job_store", store)
    monkeypatch.setattr("src.dashboard.api.job_store", store)
    monkeypatch.setattr("src.dashboard.service.default_job_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(state_manager=sm)
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_http(f"http://127.0.0.1:{port}/api/health")
        analytics = httpx.get(
            f"http://127.0.0.1:{port}/api/analytics",
            params={"period": "7d"},
            timeout=15.0,
            verify=False,
        )
        assert analytics.status_code == 200, analytics.text
        body = analytics.json()
        assert body["totals"]["jobs"] == 2
        assert body["matched"] == 2
        listed = httpx.get(
            f"http://127.0.0.1:{port}/api/jobs",
            params={"page": 1, "page_size": 25},
            timeout=15.0,
            verify=False,
        )
        assert listed.status_code == 200, listed.text
        jobs_body = listed.json()
        keys = {j["issue_key"] for j in jobs_body.get("jobs") or []}
        assert keys == {"KAN-101", "KAN-102"}
        detail = httpx.get(
            f"http://127.0.0.1:{port}/api/jobs/job_e2ea0001",
            timeout=15.0,
            verify=False,
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["job"]["description"] == "write the plan"
        assert (jobs_dir / "job_e2ea0001.json").is_file()
    finally:
        server.should_exit = True
