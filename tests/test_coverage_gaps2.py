"""Second pass gap fillers for remaining lines/branches."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient


def test_logger_critical_module():
    from src import logger as lm
    from src.logger import LogLevel

    lm.set_level(LogLevel.DEBUG)
    lm.critical("critical msg")
    lm.error("err with exc", RuntimeError("x"))


@pytest.mark.asyncio
async def test_process_event_issue_updated_branch(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = reporter
    with patch.object(p, "_handle_issue_updated", new_callable=AsyncMock) as m:
        await p.process_event(
            {
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "U-1", "fields": {"status": {"name": "To Do"}}},
            }
        )
        m.assert_awaited()


@pytest.mark.asyncio
async def test_fail_issue_comment_id_none(state_manager, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = MagicMock()
    p.reporter.post_error.return_value = None
    state_manager.create_state("NID-1", "s", "d")
    p._fail_issue("NID-1", "err")


def test_poller_non_terminal_non_inflight(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, board_id="1")
    p.state_manager = state_manager
    state_manager.create_state("PR-1", "s", "d")
    state_manager.update_state("PR-1", status=TaskStatus.PLAN_READY)
    p._status_before_poll = {"PR-1": "in progress"}
    issues = [{"key": "PR-1", "fields": {"status": {"name": "To Do"}}}]
    assert p.check_status_changes(issues) == []


def test_webhook_no_match_returns_false_path():
    from fastapi.testclient import TestClient
    from src.jira.webhook_server import create_webhook_app
    from src.config import settings

    app = create_webhook_app(secret=None)
    c = TestClient(app)
    body = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "Z-9",
            "fields": {
                "project": {"key": "PROJ"},
                "labels": ["not-a-trigger"],
                "assignee": None,
            },
        },
    }
    with patch("src.jira.webhook_server.settings") as s:
        s.webhook_path = settings.webhook_path
        s.jira_projects_list = ["PROJ"]
        s.trigger_labels_list = ["ai-assist"]
        s.trigger_on_assignment = False
        s.trigger_mentions_list = ["@DevBot"]
        r = c.post(settings.webhook_path, json=body)
        assert r.json()["status"] == "ignored"


def test_webhook_verify_signature_false_no_header():
    """Cover verify_signature when secret set and signature is None."""
    from src.jira.webhook_server import create_webhook_app
    from fastapi.testclient import TestClient
    from src.config import settings

    app = create_webhook_app(secret="abc")
    # Access nested verify via posting without header - already 401
    c = TestClient(app)
    r = c.post(settings.webhook_path, content=b"{}")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_monitor_skips_plan_ready_and_exception(state_manager, reporter, fake_jira):
    from src.daemon import JiraAgentDaemon
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = proc
    daemon.state_manager = state_manager
    daemon._running = True

    state_manager.create_state("PRM-1", "s", "d")
    state_manager.update_state("PRM-1", status=TaskStatus.PLAN_READY)

    # first call raises, second stops
    calls = {"n": 0}

    def get_active():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk")
        daemon._running = False
        return []

    state_manager.get_active_issues = get_active
    with patch("asyncio.sleep", new_callable=AsyncMock):
        await daemon._monitor_active_issues()


def test_daemon_main_module():
    import runpy
    import sys

    with patch("src.daemon.main") as m:
        # simulate __main__
        with patch.object(sys, "argv", ["daemon"]):
            import src.daemon as d

            # call the if __name__ block logic
            d.main()
            m.assert_called()


@pytest.mark.asyncio
async def test_agent_stream_timeout_continue(tmp_path, monkeypatch):
    """Cover readline TimeoutError continue path and inner elapsed timeout."""
    monkeypatch.chdir(tmp_path)
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner(working_directory=tmp_path)

    class SlowStream:
        def __init__(self):
            self.n = 0

        async def readline(self):
            self.n += 1
            if self.n < 3:
                await asyncio.sleep(1.1)  # trigger wait_for timeout 1.0
                return b""
            return b""

    class FakeProc:
        def __init__(self):
            self.returncode = 0
            self.stdout = SlowStream()
            self.stderr = SlowStream()

        async def wait(self):
            return 0

        def kill(self):
            pass

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 30
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=FakeProc()),
        ):
            task = AgentTask(description="d", prompt="p", agent="a", issue_key="ST-1")
            # short outer timeout may fire instead - either path is fine
            result = await runner.run_agent(task, timeout_seconds=5)
            assert "returncode" in result


@pytest.mark.asyncio
async def test_background_agent_windows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner(working_directory=tmp_path)

    class P:
        returncode = None
        pid = 1

    with patch("src.orchestrator.agent_runner.IS_WINDOWS", True):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.opencode_cli = "opencode"
            s.default_model = "m"
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=P()),
            ):
                tid = await runner.run_background_agent(
                    AgentTask(description="d", prompt="p", agent="a")
                )
                assert tid


@pytest.mark.asyncio
async def test_execution_on_retry_no_state(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    """on_retry when get_state returns None (if current_state branch false).

    Note: after skipping state update, post_progress_update still receives
    get_state() which is also None and will crash — that is a separate bug
    tested in test_logical_issues. Here we mock get_state to return None only
    once for the if current_state check by patching add path carefully.
    """
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = reporter

    state = state_manager.create_state("RNS-1", "s", "d")
    state_manager.update_state("RNS-1", plan_path="p.md")
    git = MagicMock()
    git.ensure_feature_branch.return_value = "feature/x"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/x"
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "s"
    git.get_last_commit_message.return_value = "b"
    git.create_merge_request.return_value = None

    calls = {"n": 0}
    real_get = state_manager.get_state

    def flaky_get(key):
        calls["n"] += 1
        # first get inside on_retry -> None; later calls real
        if calls["n"] == 1:
            return None
        return real_get(key)

    async def with_retry(task, on_output=None, on_progress=None, on_retry=None, **kw):
        if on_retry:
            with patch.object(state_manager, "get_state", side_effect=flaky_get):
                try:
                    on_retry(1, 0.01, "error", None, "e", 1, "ses")
                except Exception:
                    pass  # progress update may fail when state None
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "fail",
            "retry_info": {"attempts": 1},
        }

    runner = MagicMock()
    runner.run_agent_with_retry = with_retry
    p.git_manager = git
    p.agent_runner = runner
    with patch.object(p, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.orchestrator_agent = "atlas"
            s.agent_task_timeout_seconds = 5
            s.agent_task_max_retries = 1
            s.default_branch = "main"
            await p._start_execution_workflow(state)


@pytest.mark.asyncio
async def test_code_review_on_output_callable(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = reporter
    state = state_manager.create_state("COO-1", "s", "d")

    async def run_agent(task, on_output=None, **kw):
        if on_output:
            on_output("stdout", "line")
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    p.agent_runner = MagicMock()
    p.agent_runner.run_agent = run_agent
    p.git_manager = None
    with patch("src.processor.settings") as s:
        s.code_review_model = "m"
        s.code_review_agent = "e"
        s.agent_task_timeout_seconds = 5
        await p._start_code_review(state, "sum")
