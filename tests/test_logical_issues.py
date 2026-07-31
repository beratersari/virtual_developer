"""
Tests that encode expected correct behavior.

If these FAIL, the production code has a logical bug (or incomplete design).
They are intentionally strict.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


def test_logic_max_retries_zero_should_mean_zero_retries():
    """
    WHY: `max_retries or settings...` treats 0 as falsy, so callers cannot
    disable retries by passing max_retries=0. Zero should mean zero extra attempts.
    """
    runner = AgentRunner()
    calls = {"n": 0}

    async def fail(*a, **k):
        calls["n"] += 1
        return {
            "task_id": "t",
            "returncode": 1,
            "stdout": "",
            "stderr": "err",
            "session_file": None,
            "opencode_session_id": None,
            "progress": 0,
        }

    import asyncio

    async def run():
        with patch.object(runner, "run_agent", side_effect=fail):
            with patch("src.orchestrator.agent_runner.settings") as s:
                s.agent_task_max_retries = 5
                s.agent_task_retry_delay_seconds = 0.01
                s.agent_task_retry_backoff_multiplier = 1.0
                s.agent_task_retry_on_timeout = True
                s.agent_task_retry_on_error = True
                from src.orchestrator.agent_runner import AgentTask

                task = AgentTask(description="d", prompt="p", agent="a")
                await runner.run_agent_with_retry(task, max_retries=0)

    asyncio.get_event_loop().run_until_complete(run()) if False else None
    # use pytest-asyncio style via sync helper
    asyncio.run(run())
    # Expected correct behavior: only 1 attempt when max_retries=0
    assert calls["n"] == 1, (
        f"max_retries=0 should run once, but ran {calls['n']} times "
        f"(0 is treated as unset via `or`)"
    )


def test_logic_progress_percent_should_be_clamped_0_100():
    """
    WHY: Any `(\\d+)%` in agent logs becomes progress, including values >100
    or noise like '150% CPU'. Progress stored in state should be clamped.
    """
    runner = AgentRunner()
    assert runner._parse_progress("Progress: 150%") <= 100
    assert runner._parse_progress("Progress: 150%") >= 0


def test_logic_update_issue_labels_should_merge_not_replace():
    """
    Labels added by the bot must merge with existing issue labels (e.g. ai-assist).
    """
    from src.reporter.jira_reporter import JiraReporter
    from src.state.models import JiraAgentState

    client = MagicMock()
    client.add_comment.return_value = {"id": "1"}
    client.get_issue.return_value = {
        "fields": {"labels": ["ai-assist", "bot"]},
    }
    captured = {}

    def add_labels(issue_key, labels):
        existing = client.get_issue(issue_key)["fields"]["labels"]
        captured["labels"] = list(dict.fromkeys([*existing, *labels]))
        return True

    # Prefer add_labels path used by reporter
    client.add_labels.side_effect = add_labels
    r = JiraReporter(client=client)
    state = JiraAgentState(
        issue_key="L-1",
        issue_summary="s",
        metadata={"workflow_type": "planning"},
        plan_path="/p.md",
    )
    r.post_plan_summary(state, "# plan\nstep")

    labels = captured.get("labels")
    assert labels is not None
    assert "ai-assist" in labels
    assert "ai-plan-ready" in labels


def test_logic_create_issue_payload_should_wrap_fields():
    """
    WHY: Real Jira REST API v2 expects {"fields": {...}} for POST /issue.
    Current client posts top-level project/summary keys. That only works if a
    proxy rewrites the body; stock on-prem Jira returns 400.
    """
    from src.jira.client import JiraClient
    import httpx

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"key": "P-1"}
            resp.text = ""
            resp.raise_for_status = MagicMock()
            http.post.return_value = resp
            c.create_issue("PROJ", "sum", "desc")
            kwargs = http.post.call_args.kwargs
            payload = kwargs.get("json") or {}
            assert "fields" in payload, (
                "create_issue payload missing 'fields' wrapper required by Jira REST API v2"
            )


def test_logic_read_session_output_should_use_written_path():
    """
    WHY: run_agent writes ISSUE_timestamp_attempt.log but read_session_output
    rebuilds path as {task_id}.log without issue_key/timestamp, so monitor
    always reads empty content for real tasks.
    """
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        runner = AgentRunner(working_directory=Path(td))
        # simulate what run_agent would write
        real = runner._get_session_file("task_deadbeef", issue_key="PROJ-9", attempt_number=0)
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("REAL SESSION OUTPUT")
        content = runner.read_session_output("task_deadbeef")
        assert "REAL SESSION OUTPUT" in content, (
            "read_session_output cannot find the file run_agent wrote "
            f"(looked up via task_id only; real path was {real.name})"
        )


def test_logic_on_retry_post_progress_crashes_if_state_missing():
    """
    WHY: on_retry does:
      current_state = get_state(...)
      if current_state: add_retry...
      self.reporter.post_progress_update(self.state_manager.get_state(...), ...)
    If state was deleted/missing, the second get_state is also None and
    post_progress_update crashes on state.status — should guard or skip Jira post.
    """
    from src.reporter.jira_reporter import JiraReporter
    from src.state.models import JiraAgentState

    r = JiraReporter(client=MagicMock())
    # Expected correct behavior: None state should not raise
    try:
        r.post_progress_update(None, "retrying")  # type: ignore[arg-type]
        ok = True
    except Exception:
        ok = False
    assert ok, "post_progress_update(None) should not raise"


def test_logic_assign_issue_on_prem_should_use_name_not_account_id():
    """
    WHY: assign_issue uses fields.assignee.accountId (Jira Cloud). On-prem
    Server/DC typically expects {'name': username}. User said on-prem API style.
    """
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.onprem.local"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            resp = MagicMock()
            resp.status_code = 204
            resp.raise_for_status = MagicMock()
            http.put.return_value = resp
            c.assign_issue("P-1", "jdoe")
            payload = http.put.call_args.kwargs.get("json") or {}
            fields = payload.get("fields", payload)
            assignee = fields.get("assignee", {})
            assert "name" in assignee or "accountId" not in assignee, (
                "assign_issue sends accountId; on-prem Jira expects name"
            )
