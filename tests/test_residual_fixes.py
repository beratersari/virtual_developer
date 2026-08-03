"""Regression tests for residual security/lifecycle/FE-backend fixes."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.daemon import JiraAgentDaemon
from src.dashboard.api import create_dashboard_app
from src.jira.poller import JiraPoller
from src.orchestrator.agent_runner import AgentRunner
from src.processor import JobProcessor, _JobSlotLimiter
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_cancel_while_still_todo_does_not_requeue(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    proc.reporter = MagicMock()
    poller = JiraPoller(board_id="1")
    poller.state_manager = sm
    proc._poller = poller

    sm.create_state("R-1", "s", "d")
    sm.update_state("R-1", status=TaskStatus.EXECUTING, current_task_id="t1")
    job = store.create_job(issue_key="R-1", status="executing", task_id="t1")
    proc._active_jobs["R-1"] = job["job_id"]
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    proc._contexts["R-1"] = {"git": None, "runner": runner}
    poller._last_jira_status["R-1"] = "to do"

    asyncio.run(proc.cancel_job("R-1", reason="test"))
    assert poller._last_jira_status["R-1"] == "to do"
    poller._status_before_poll = {"R-1": "to do"}
    issue = {
        "key": "R-1",
        "fields": {
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": [],
            "assignee": None,
        },
    }
    assert poller.check_status_changes([issue]) == []


def test_cancel_from_in_progress_requeues_on_todo(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    proc.reporter = MagicMock()
    poller = JiraPoller(board_id="1")
    poller.state_manager = sm
    proc._poller = poller

    sm.create_state("R-2", "s", "d")
    sm.update_state("R-2", status=TaskStatus.EXECUTING, current_task_id="t1")
    job = store.create_job(issue_key="R-2", status="executing", task_id="t1")
    proc._active_jobs["R-2"] = job["job_id"]
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    proc._contexts["R-2"] = {"git": None, "runner": runner}
    poller._last_jira_status["R-2"] = "in progress"

    asyncio.run(proc.cancel_job("R-2", reason="test"))
    assert poller._last_jira_status["R-2"] == "in progress"
    poller._status_before_poll = {"R-2": "in progress"}
    issue = {
        "key": "R-2",
        "fields": {
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": [],
            "assignee": None,
        },
    }
    out = poller.check_status_changes([issue])
    assert [i["key"] for i in out] == ["R-2"]


def test_process_signalled_false_when_nothing_killed(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    proc.reporter = MagicMock()
    sm.create_state("R-3", "s", "d")
    sm.update_state("R-3", status=TaskStatus.EXECUTING, current_task_id="t1")
    runner = MagicMock()
    runner.cancel_task.return_value = False
    runner.cancel_all_tasks.return_value = 0
    proc._contexts["R-3"] = {"git": None, "runner": runner}
    res = asyncio.run(proc.cancel_job("R-3"))
    assert res["ok"] is True
    assert res["process_signalled"] is False


def test_watchdog_releases_context(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = JobStore(jobs_dir=tmp_path / "jobs")
    d = JiraAgentDaemon()
    d.processor.state_manager = sm
    d.processor.job_store = store
    d.processor.reporter = MagicMock()
    sm.create_state("R-4", "s", "d")
    sm.update_state(
        "R-4",
        status=TaskStatus.EXECUTING,
        current_task_id="t1",
        started_at=datetime(2020, 1, 1),
    )
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    d.processor._contexts["R-4"] = {"git": MagicMock(), "runner": runner}
    job = store.create_job(issue_key="R-4", status="executing", task_id="t1")
    d.processor._active_jobs["R-4"] = job["job_id"]
    d._abort_stuck_issue(sm.get_state("R-4"), "stuck")
    assert "R-4" not in d.processor._contexts
    assert sm.get_state("R-4").status == TaskStatus.ERROR


def test_plan_ready_not_overwritten(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    proc.reporter = MagicMock()
    sm.create_state("R-5", "s", "d")
    job = store.create_job(issue_key="R-5", status="planning", task_id="t1")
    store.update_job(job["job_id"], status="plan_ready")
    sm.update_state(
        "R-5",
        status=TaskStatus.PLAN_READY,
        metadata={"current_job_id": job["job_id"]},
    )
    proc._finish_job_record("R-5", status="error", error_message="later")
    assert store.get_job(job["job_id"])["status"] == "plan_ready"


def test_supersede_keeps_old_task_id(tmp_path):
    store = JobStore(jobs_dir=tmp_path / "jobs")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = store
    proc.reporter = MagicMock()
    sm.create_state("R-6", "s", "d")
    job1 = store.create_job(issue_key="R-6", task_id="task_OLD", status="executing")
    proc._active_jobs["R-6"] = job1["job_id"]
    sm.update_state("R-6", status=TaskStatus.EXECUTING, current_task_id="task_NEW")
    proc._start_job_record(
        sm.get_state("R-6"),
        workflow_type="execution",
        agent="a",
        task_id="task_NEW",
        status="executing",
    )
    old = store.get_job(job1["job_id"])
    assert old["status"] == "superseded"
    assert old["task_id"] == "task_OLD"


@pytest.mark.asyncio
async def test_job_slot_limiter_no_over_admit():
    lim = _JobSlotLimiter(2)
    await lim.acquire()
    await lim.acquire()
    lim.resize(1)
    import asyncio

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lim.acquire(), timeout=0.05)


def test_session_parse_last_labeled():
    r = AgentRunner()
    sid = r._parse_session_id(
        ["noise ses_oldparent xyz", "Session: ses_realnew123"]
    )
    assert sid == "ses_realnew123"


def test_docs_not_spa_fallback(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>spa</html>")
    monkeypatch.setattr("src.dashboard.api._static_dir", lambda: dist)
    client = TestClient(create_dashboard_app())
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/").status_code == 200


def test_board_issues_pagination():
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            client = JiraClient()
            client.client = http

            def resp(*_a, **k):
                start = k.get("params", {}).get("startAt", 0)
                m = MagicMock()
                m.status_code = 200
                m.raise_for_status = MagicMock()
                if start == 0:
                    m.json.return_value = {
                        "total": 150,
                        "issues": [{"key": f"I-{i}"} for i in range(100)],
                    }
                else:
                    m.json.return_value = {
                        "total": 150,
                        "issues": [{"key": f"I-{i}"} for i in range(100, 150)],
                    }
                return m

            http.get.side_effect = resp
            issues = client.get_board_issues("1", max_results=100)
            assert len(issues) == 150
            assert http.get.call_count == 2
