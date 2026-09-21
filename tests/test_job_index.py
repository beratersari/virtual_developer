"""SQLite job index (local file next to jobs/)."""

from __future__ import annotations

import json
from pathlib import Path

from src.state.job_index import JobIndex, default_index_path, row_params


def test_default_index_path_sits_beside_jobs_dir(tmp_path: Path):
    jobs = tmp_path / "yaver" / "jobs"
    assert default_index_path(jobs) == tmp_path / "yaver" / "jobs.sqlite"


def test_upsert_list_count_delete(tmp_path: Path):
    idx = JobIndex(tmp_path / "jobs.sqlite")
    idx.upsert(
        {
            "job_id": "job_aaa",
            "issue_key": "KAN-1",
            "summary": "first",
            "status": "completed",
            "workflow_type": "execution",
            "source": "jira",
            "model": "gpt-4.1",
            "started_at": "2026-01-02T10:00:00",
            "gitlab_mr_iid": 12,
        }
    )
    idx.upsert(
        {
            "job_id": "job_bbb",
            "issue_key": "KAN-2",
            "summary": "later",
            "status": "error",
            "started_at": "2026-01-03T10:00:00",
        }
    )
    assert idx.count() == 2
    assert idx.count(issue_key="kan-1") == 1
    assert idx.list_ids() == ["job_bbb", "job_aaa"]
    assert idx.list_ids(issue_key="KAN-1") == ["job_aaa"]
    rows = {r["job_id"]: r for r in idx.iter_jobs()}
    assert rows["job_aaa"]["model"] == "gpt-4.1"
    assert rows["job_aaa"]["gitlab_mr_iid"] == 12
    idx.delete("job_aaa")
    assert idx.count() == 1
    assert idx.list_ids() == ["job_bbb"]
    idx.close()


def test_upsert_skips_blank_job_id(tmp_path: Path):
    idx = JobIndex(tmp_path / "jobs.sqlite")
    idx.upsert({"issue_key": "KAN-1", "summary": "no id"})
    assert idx.count() == 0
    idx.close()


def test_reconcile_fills_missing_job_id_and_refreshes_stale_rows(tmp_path: Path):
    """Old files name the id. A newer JSON must replace a stale index row."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "job_oldname.json").write_text(
        json.dumps(
            {
                "issue_key": "KAN-3",
                "summary": "first write",
                "status": "completed",
                "created_at": "2025-11-01T09:00:00",
            }
        ),
        encoding="utf-8",
    )
    idx = JobIndex(tmp_path / "jobs.sqlite")
    assert idx.reconcile(jobs_dir) == 1
    assert idx.all_ids() == {"job_oldname"}
    row = idx.iter_jobs()[0]
    assert row["started_at"] == "2025-11-01T09:00:00"
    (jobs_dir / "job_oldname.json").write_text(
        json.dumps(
            {
                "job_id": "job_oldname",
                "issue_key": "KAN-3",
                "summary": "edited on disk",
                "status": "error",
                "error_message": "push failed",
                "started_at": "2025-11-01T09:00:00",
            }
        ),
        encoding="utf-8",
    )
    assert idx.reconcile(jobs_dir) == 1
    rows = idx.iter_jobs()
    assert rows[0]["summary"] == "edited on disk"
    assert rows[0]["status"] == "error"
    idx.close()


def test_reconcile_inserts_json_and_drops_orphan_rows(tmp_path: Path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    raw = {
        "job_id": "job_legacy01",
        "issue_key": "KAN-9",
        "summary": "from disk",
        "status": "completed",
        "workflow_type": "planning",
        "started_at": "2026-02-01T08:00:00",
    }
    (jobs_dir / "job_legacy01.json").write_text(json.dumps(raw), encoding="utf-8")
    idx = JobIndex(tmp_path / "jobs.sqlite")
    idx.upsert({"job_id": "job_gone", "issue_key": "KAN-0", "status": "error"})
    added = idx.reconcile(jobs_dir)
    assert added == 1
    assert idx.count() == 1
    assert idx.all_ids() == {"job_legacy01"}
    rows = idx.iter_jobs()
    assert rows[0]["summary"] == "from disk"
    idx.close()


def test_query_jobs_applies_period_and_filters(tmp_path: Path):
    idx = JobIndex(tmp_path / "jobs.sqlite")
    idx.upsert(
        {
            "job_id": "job_old",
            "issue_key": "KAN-1",
            "status": "completed",
            "workflow_type": "execution",
            "source": "jira",
            "model": "gpt-4.1",
            "started_at": "2026-01-01T10:00:00",
        }
    )
    idx.upsert(
        {
            "job_id": "job_new",
            "issue_key": "KAN-2",
            "status": "error",
            "workflow_type": "planning",
            "source": "gitlab",
            "model": "sonnet",
            "repository_url": "https://gitlab.example.com/acme/app.git",
            "started_at": "2026-09-20T10:00:00",
        }
    )
    week = idx.query_jobs(start="2026-09-14T00:00:00", end="2026-09-21T23:59:59")
    assert [r["job_id"] for r in week] == ["job_new"]
    err = idx.query_jobs(
        start="2026-01-01T00:00:00",
        end="2026-12-31T23:59:59",
        status="error",
    )
    assert [r["job_id"] for r in err] == ["job_new"]
    plan = idx.query_jobs(
        start="2026-01-01T00:00:00",
        end="2026-12-31T23:59:59",
        category="plan",
    )
    assert [r["job_id"] for r in plan] == ["job_new"]
    repo = idx.query_jobs(
        start="2026-01-01T00:00:00",
        end="2026-12-31T23:59:59",
        repository="acme/app",
    )
    assert [r["job_id"] for r in repo] == ["job_new"]
    key = idx.query_jobs(
        start="2026-01-01T00:00:00",
        end="2026-12-31T23:59:59",
        issue_key="KAN-2",
    )
    assert [r["job_id"] for r in key] == ["job_new"]
    assert idx.min_when() == "2026-01-01T10:00:00"
    idx.close()


def test_row_params_uses_created_at_when_started_missing():
    p = row_params(
        {
            "job_id": "job_x",
            "created_at": "2026-03-01T00:00:00",
            "azure_pr_id": "7",
        }
    )
    assert p["started_at"] == "2026-03-01T00:00:00"
    assert p["azure_pr_id"] == 7
