"""Upgrade path: months of job JSON plus a real OpenCode session DB, over TCP.

The new exe creates jobs.sqlite on first Analytics/Jobs request. These tests
do not call ensure_index themselves. uvicorn + httpx is the same request a
browser sends. The OpenCode session table is the real lookup schema, not a
stand-in for find_sessions_for_issue.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
import uvicorn

from src.dashboard.api import create_dashboard_app
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.session_bind_store import workspace_id_for


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


def _stamp(days_ago: int) -> str:
    dt = datetime.now().replace(microsecond=0) - timedelta(days=days_ago)
    return dt.isoformat(timespec="seconds")


def _write_opencode_db(path, directory: str) -> None:
    """Same session columns OpenCode's local DB exposes to Yaver."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            agent TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER
        )
        """
    )
    con.execute(
        """
        INSERT INTO session (
            id, title, directory, agent,
            time_created, time_updated, cost, tokens_input, tokens_output
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses_legacy77",
            "KAN-77: months of history",
            directory,
            "derman-build",
            1_700_000_000_000,
            1_700_000_100_000,
            0.02,
            1200,
            800,
        ),
    )
    con.commit()
    con.close()


def test_http_backfill_old_jobs_and_real_opencode_session(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """First HTTP read of an old data dir builds the index and keeps sessions."""
    jobs_dir = isolate_jira_agent_artifacts["jobs_dir"]
    runtime = isolate_jira_agent_artifacts["runtime"]
    clone = tmp_path / "repo_KAN-77_20260101"
    clone.mkdir()
    (clone / "README.md").write_text("old clone\n", encoding="utf-8")
    _write_opencode_db(runtime / "opencode.db", str(clone))

    recent = _stamp(1)
    inside = _stamp(12)
    ancient = _stamp(200)
    # Filename is the id. Months-old files sometimes omitted job_id and
    # stored the clock in created_at only.
    (jobs_dir / "job_createdonly.json").write_text(
        json.dumps(
            {
                "issue_key": "KAN-12",
                "summary": "created_at only",
                "status": "completed",
                "workflow_type": "planning",
                "source": "jira",
                "model": "gpt-4.1",
                "created_at": inside,
                "description": "plan from last month",
            }
        ),
        encoding="utf-8",
    )
    (jobs_dir / "job_ancient.json").write_text(
        json.dumps(
            {
                "job_id": "job_ancient",
                "issue_key": "KAN-1",
                "summary": "last spring",
                "status": "completed",
                "workflow_type": "execution",
                "source": "jira",
                "started_at": ancient,
                "updated_at": ancient,
            }
        ),
        encoding="utf-8",
    )
    (jobs_dir / "job_legacy77.json").write_text(
        json.dumps(
            {
                "job_id": "job_legacy77",
                "issue_key": "KAN-77",
                "summary": "feat(KAN-77): keep the session",
                "description": "operator-visible body",
                "status": "error",
                "workflow_type": "execution",
                "source": "jira",
                "agent": "derman-build",
                "model": "gpt-4.1",
                "backend": "opencode",
                "error_message": "push failed",
                "delivery_status": "push_failed",
                "opencode_session_id": "ses_legacy77",
                "opencode_session_ids": ["ses_legacy77"],
                "working_directory": str(clone),
                "repository_url": "https://gitlab.example.com/acme/app.git",
                "merge_request_url": "https://gitlab.example.com/acme/app/-/merge_requests/77",
                "merge_request_state": "opened",
                "started_at": recent,
                "updated_at": recent,
                "session_log_path": str(runtime / "sessions" / "KAN-77.log"),
            }
        ),
        encoding="utf-8",
    )
    (jobs_dir / "job_corrupt.json").write_text("{not json", encoding="utf-8")

    store: JobStore = isolate_jira_agent_artifacts["job_store"]
    # JobStore opens an empty jobs.sqlite in __init__. Rows appear on the
    # first Analytics request, which is what a new exe does at startup.
    monkeypatch.setattr("src.dashboard.analytics.default_job_store", store)
    monkeypatch.setattr("src.dashboard.service.default_job_store", store)
    monkeypatch.setattr("src.dashboard.api.job_store", store)
    monkeypatch.setattr("src.state.job_store.job_store", store)

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-77", "feat(KAN-77): keep the session")
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    assert binds.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-77",
        target_branch="develop",
        session_id="ses_legacy77",
        issue_key="KAN-77",
        job_id="job_legacy77",
        working_directory=str(clone),
        kind="build",
    )
    wid = workspace_id_for(
        "https://gitlab.example.com/acme/app.git",
        "feature/KAN-77",
        "develop",
    )

    app = create_dashboard_app(state_manager=sm)
    app.state.job_store = store
    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_http(f"{base}/api/health")
        started = time.monotonic()
        analytics = httpx.get(
            f"{base}/api/analytics",
            params={"period": "30d"},
            timeout=15.0,
            verify=False,
        )
        elapsed = time.monotonic() - started
        assert analytics.status_code == 200, analytics.text
        assert elapsed < 2.0, f"30d analytics took {elapsed:.2f}s"
        body = analytics.json()
        assert body["range"]["bucket"] == "day"
        assert len(body["series"]) <= 400
        # created_at-only (12d) + the recent error. The 200-day job is outside.
        assert body["totals"]["jobs"] == 2
        assert body["totals"]["error"] == 1
        assert body["totals"]["completed"] == 1
        assert (jobs_dir.parent / "jobs.sqlite").is_file()

        everything = httpx.get(
            f"{base}/api/analytics",
            params={"period": "all"},
            timeout=15.0,
            verify=False,
        )
        assert everything.status_code == 200, everything.text
        assert everything.json()["totals"]["jobs"] == 3
        assert len(everything.json()["series"]) <= 400

        listed = httpx.get(
            f"{base}/api/jobs",
            params={"page": 1, "page_size": 25},
            timeout=15.0,
            verify=False,
        )
        assert listed.status_code == 200, listed.text
        jobs = listed.json()["jobs"]
        assert listed.json()["total"] == 3
        legacy = next(j for j in jobs if j["job_id"] == "job_legacy77")
        assert legacy["error_message"] == "push failed"
        assert legacy["delivery_status"] == "push_failed"
        assert legacy["opencode_session_id"] == "ses_legacy77"
        assert "merge_requests/77" in (legacy["merge_request_url"] or "")
        created = next(j for j in jobs if j["job_id"] == "job_createdonly")
        assert created["issue_key"] == "KAN-12"
        assert created["summary"] == "created_at only"

        detail = httpx.get(
            f"{base}/api/jobs/job_legacy77",
            timeout=15.0,
            verify=False,
        )
        assert detail.status_code == 200, detail.text
        job_doc = detail.json()["job"]
        assert job_doc["working_directory"] == str(clone)
        assert job_doc["session_log_path"]

        task = httpx.get(
            f"{base}/api/tasks/KAN-77",
            params={"artifacts": "true", "live": "false"},
            timeout=15.0,
            verify=False,
        )
        assert task.status_code == 200, task.text
        sessions = task.json().get("opencode_sessions") or []
        assert any(s.get("id") == "ses_legacy77" for s in sessions), sessions
        assert any(
            s.get("title") == "KAN-77: months of history" for s in sessions
        )

        workspace = httpx.get(
            f"{base}/api/opencode-workspaces/{wid}",
            timeout=15.0,
            verify=False,
        )
        assert workspace.status_code == 200, workspace.text
        ws_sessions = workspace.json().get("sessions") or []
        assert any(
            str(s.get("session_id") or "") == "ses_legacy77" for s in ws_sessions
        ), ws_sessions
    finally:
        server.should_exit = True
        thread.join(timeout=5)
