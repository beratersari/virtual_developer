"""Regression tests for critical security and lifecycle fixes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app, _safe_under_static
from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_spa_path_traversal_blocked(tmp_path, monkeypatch):
    """SPA fallback must not serve files outside web/dist."""
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html>ok</html>")
    secret = tmp_path / "secret.env"
    secret.write_text("JIRA_API_TOKEN=super-secret")

    # Point static dir at our temp dist
    monkeypatch.setattr(
        "src.dashboard.api._static_dir",
        lambda: dist,
    )
    app = create_dashboard_app()
    client = TestClient(app)

    # Encoded traversal — should get index.html SPA shell, not secret
    r = client.get("/%2e%2e/%2e%2e/secret.env")
    assert r.status_code == 200
    assert "super-secret" not in r.text
    assert "JIRA_API_TOKEN" not in r.text


def test_safe_under_static_rejects_parent(tmp_path):
    static = tmp_path / "dist"
    static.mkdir()
    (static / "ok.txt").write_text("yes")
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    assert _safe_under_static(static, "ok.txt") is not None
    assert _safe_under_static(static, "../outside.txt") is None
    assert _safe_under_static(static, "..%2foutside.txt") is None


def test_cors_not_wildcard(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.api._static_dir",
        lambda: None,
    )
    app = create_dashboard_app()
    client = TestClient(app)
    r = client.get("/api/meta", headers={"Origin": "https://evil.example"})
    # Must not reflect arbitrary evil origins
    acao = r.headers.get("access-control-allow-origin", "")
    assert acao != "https://evil.example"
    assert acao != "*"


def test_poller_skips_terminal_on_cold_start(tmp_path, monkeypatch):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("COLD-1", "done already", "d")
    sm.update_state("COLD-1", status=TaskStatus.COMPLETED)

    poller = JiraPoller(board_id="1")
    poller.state_manager = sm
    poller.client = MagicMock()
    poller.interval = 30

    issue = {
        "key": "COLD-1",
        "fields": {
            "summary": "done already",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["ai-assist"],
            "assignee": None,
        },
    }
    poller.client.get_active_sprint.return_value = None
    poller.client.get_board_issues.return_value = [issue]

    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    result = poller.poll_board()

    keys = [i["key"] for i in result]
    assert "COLD-1" not in keys
    assert "COLD-1" in poller._seen_issues


def test_fail_issue_finishes_job_and_sets_requeue(tmp_path, monkeypatch):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    monkeypatch.setattr("src.processor.job_store", __import__("src.state.job_store", fromlist=["JobStore"]).JobStore(jobs_dir=tmp_path / "jobs"))
    from src.state.job_store import JobStore

    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = JobStore(jobs_dir=tmp_path / "jobs")
    proc.reporter = MagicMock()
    proc.jira_client = MagicMock()

    state = sm.create_state("FAIL-1", "s", "desc")
    job = proc.job_store.create_job(
        issue_key="FAIL-1",
        summary="s",
        description="desc",
        workflow_type="direct",
        agent="a",
        task_id="task_x",
        status="executing",
    )
    proc._active_jobs["FAIL-1"] = job["job_id"]
    sm.update_state("FAIL-1", status=TaskStatus.EXECUTING, current_task_id="task_x")

    proc._fail_issue("FAIL-1", "boom")

    st = sm.get_state("FAIL-1")
    assert st is not None
    assert st.status == TaskStatus.ERROR
    assert st.metadata.get("requeue_eligible") is True
    assert "FAIL-1" not in proc._active_jobs
    finished = proc.job_store.get_job(job["job_id"])
    assert finished is not None
    assert finished["status"] == "error"


def test_ensure_agent_runner_not_project_root(tmp_path, monkeypatch):
    from src.config import settings

    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()

    def boom(*a, **k):
        raise RuntimeError("no git")

    monkeypatch.setattr(proc, "_init_git_manager", boom)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "temp_dir_base", Path(".temp"))
    monkeypatch.setattr(settings, "project_root", tmp_path / "project_root_real")
    (tmp_path / "project_root_real").mkdir()

    sm.create_state("SBX-1", "s", "d")
    runner = proc._ensure_agent_runner("SBX-1")
    wd = Path(runner.working_directory).resolve()
    assert "sandbox_" in wd.name
    assert wd != Path(settings.project_root).resolve()
    assert "project_root_real" not in str(wd)


def test_state_manager_atomic_concurrent_writes(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path)
    sm.create_state("RACE-1", "s", "d")

    errors = []

    def writer(n):
        try:
            for i in range(20):
                sm.update_state("RACE-1", progress_percentage=i, error_message=f"w{n}-{i}")
        except Exception as e:
            errors.append(e)

    import threading

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    st = sm.get_state("RACE-1")
    assert st is not None
    # File must remain valid JSON
    assert st.issue_key == "RACE-1"
