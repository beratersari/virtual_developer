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


def test_job_artifacts_api_blocks_path_traversal(tmp_path, monkeypatch):
    """Poisoned job session_log_path / prompt_path must not leak arbitrary files.

    ``GET /api/jobs/{id}/artifacts`` reads paths stored on the job record.
    Even if those fields point outside ``.jira-agent``, content must stay empty
    and the secret file body must not appear in the JSON response.
    """
    from src.state.job_store import JobStore

    monkeypatch.chdir(tmp_path)
    agent_root = tmp_path / ".jira-agent"
    agent_root.mkdir()
    (agent_root / "sessions").mkdir()

    secret = tmp_path / "secret.env"
    secret.write_text("JIRA_API_TOKEN=super-secret-leak\n", encoding="utf-8")

    # Absolute + relative traversal styles an attacker might plant on a job row
    evil_abs = str(secret.resolve())
    evil_rel = str(Path("..") / "secret.env")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(
        issue_key="SEC-TRAVERSE-1",
        summary="s",
        description="d",
        workflow_type="execution",
        agent="a",
    )
    store.update_job(
        job["job_id"],
        session_log_path=evil_abs,
        prompt_path=evil_rel,
        session_log_paths=[evil_abs, str(Path("../../../etc/passwd"))],
        prompt_paths=[evil_rel],
    )
    sm.create_state("SEC-TRAVERSE-1", "s", "d")

    with patch("src.dashboard.api.job_store", store):
        with patch("src.dashboard.service.default_job_store", store):
            with patch(
                "src.dashboard.service._artifacts_root",
                lambda: agent_root.resolve(),
            ):
                app = create_dashboard_app(processor=None, state_manager=sm)
                client = TestClient(app)
                r = client.get(f"/api/jobs/{job['job_id']}/artifacts")

    assert r.status_code == 200
    body = r.json()
    blob = r.text
    assert "super-secret-leak" not in blob
    assert "JIRA_API_TOKEN" not in blob
    for row in (body.get("session_logs") or []) + (body.get("prompts") or []):
        assert not (row.get("content") or "").strip()
        err = (row.get("error") or "").lower()
        assert "outside" in err or "symlink" in err or err


def test_job_artifacts_api_allows_under_jira_agent_only(tmp_path, monkeypatch):
    """Legitimate session log under .jira-agent is readable; sibling outside is not."""
    from src.state.job_store import JobStore

    monkeypatch.chdir(tmp_path)
    agent_root = tmp_path / ".jira-agent"
    sessions = agent_root / "sessions"
    sessions.mkdir(parents=True)
    good = sessions / "SEC-OK-1_run.log"
    good.write_text("agent session output ok\n", encoding="utf-8")
    outside = tmp_path / "not-agent.log"
    outside.write_text("should-not-leak\n", encoding="utf-8")

    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    job = store.create_job(
        issue_key="SEC-OK-1",
        summary="s",
        description="d",
        workflow_type="execution",
        agent="a",
    )
    store.update_job(
        job["job_id"],
        session_log_path=str(good),
        session_log_paths=[str(good), str(outside)],
    )
    sm.create_state("SEC-OK-1", "s", "d")

    with patch("src.dashboard.api.job_store", store):
        with patch("src.dashboard.service.default_job_store", store):
            with patch(
                "src.dashboard.service._artifacts_root",
                lambda: agent_root.resolve(),
            ):
                app = create_dashboard_app(processor=None, state_manager=sm)
                client = TestClient(app)
                r = client.get(f"/api/jobs/{job['job_id']}/artifacts")

    assert r.status_code == 200
    body = r.json()
    assert "should-not-leak" not in r.text
    logs = body.get("session_logs") or []
    assert any("agent session output ok" in (row.get("content") or "") for row in logs)
    outside_rows = [row for row in logs if "not-agent" in (row.get("path") or "")]
    for row in outside_rows:
        assert "should-not-leak" not in (row.get("content") or "")
        assert row.get("error")


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


def test_poller_intakes_terminal_on_todo_with_trigger(tmp_path, monkeypatch):
    """To Do + trigger is rework: completed local state is still re-queued.

    Intentional (AGENTS.md / README). plan_ready is the exception, not
    completed/error/cancelled.
    """
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("COLD-1", "done already", "d")
    sm.update_state("COLD-1", status=TaskStatus.COMPLETED)

    poller = JiraPoller(board_id="1")
    poller.state_manager = sm
    poller.client = MagicMock()
    poller.interval = 30
    poller._seen_issues.add("COLD-1")

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
    assert "COLD-1" in keys


def test_poller_skips_in_flight_on_todo_with_trigger(tmp_path, monkeypatch):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("LIVE-1", "running", "d")
    sm.update_state("LIVE-1", status=TaskStatus.EXECUTING)

    poller = JiraPoller(board_id="1")
    poller.state_manager = sm
    poller.client = MagicMock()
    poller.interval = 30

    issue = {
        "key": "LIVE-1",
        "fields": {
            "summary": "running",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
            "assignee": None,
        },
    }
    poller.client.get_active_sprint.return_value = None
    poller.client.get_board_issues.return_value = [issue]

    from src.config import settings

    monkeypatch.setattr(settings, "trigger_labels", "bot")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    result = poller.poll_board()
    assert [i["key"] for i in result] == []


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
        workflow_type="execution",
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
