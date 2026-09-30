"""Review and test follow-ups stay on their own instruction."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, AgentTask

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

REVIEW_MARKER = "REVIEW-MARKER finish the code review"
TEST_MARKER = "TEST-MARKER write the unit tests"
BUILD_MARKER = "BUILD-MARKER implement the feature"
CLAUDE_ID = "22222222-2222-2222-2222-222222222222"
CODEX_ID = "33333333-3333-3333-3333-333333333333"
UNTAGGED_ID = "44444444-4444-4444-4444-444444444444"
STARTED_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
QUOTED_ID = "11111111-2222-3333-4444-555555555555"
LATER_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
TOP_LEVEL_ID = "cccccccc-dddd-eeee-ffff-000000000001"


def _task(agent: str, backend: str, prompt: str) -> AgentTask:
    return AgentTask(
        description="session fix",
        prompt=prompt,
        agent=agent,
        backend=backend,
        issue_key="KAN-1",
    )


def test_review_and_test_prompts_do_not_resume_implementation():
    from src.opencode_serve import (
        DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT,
        DEFAULT_CONTINUE_PROMPT,
        DEFAULT_REVIEW_UNATTENDED_NUDGE_PROMPT,
        DEFAULT_TEST_UNATTENDED_NUDGE_PROMPT,
        DEFAULT_UNATTENDED_NUDGE_PROMPT,
        compact_loop_continue_prompt,
        idle_continue_prompt,
        is_review_agent,
        is_test_agent,
        unattended_nudge_prompt,
    )

    review = unattended_nudge_prompt("derman-reviewer")
    test = unattended_nudge_prompt("derman-test")
    assert review == DEFAULT_REVIEW_UNATTENDED_NUDGE_PROMPT
    assert test == DEFAULT_TEST_UNATTENDED_NUDGE_PROMPT
    assert idle_continue_prompt("derman-reviewer") == review
    assert idle_continue_prompt("derman-test") == test
    for text in (review, test):
        lowered = text.lower()
        assert "resume implementation" not in lowered
        assert "finish remaining implementation" not in lowered
        assert "commit steps" not in lowered
    assert "finish the review only" in review.lower()
    assert "do **not** implement product code" in review.lower()
    assert "or commit" in review.lower()
    assert "finish the unit tests only" in test.lower()
    assert "do **not** implement product features" in test.lower()
    assert "commit the test files locally" in test.lower()
    assert unattended_nudge_prompt("derman-build") == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert idle_continue_prompt("derman-build") == DEFAULT_CONTINUE_PROMPT
    assert "resume implementation" in DEFAULT_CONTINUE_PROMPT
    assert compact_loop_continue_prompt("derman-reviewer") == (
        DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT
    )
    assert is_review_agent("derman-reviewer")
    assert not is_review_agent("preview")
    assert not is_review_agent("derman-plan")
    assert is_test_agent("derman-test")
    assert is_test_agent("widget-test")
    assert not is_test_agent("latest")
    assert not is_test_agent("derman-reviewer")


def test_review_and_test_retries_keep_the_original_instruction(tmp_path):
    from src.backends.claude import DEFAULT_CLAUDE_RESUME_PROMPT
    from src.backends.codex import (
        DEFAULT_CODEX_COLD_CONTINUE_PROMPT,
        DEFAULT_CODEX_RESUME_PROMPT,
    )
    from src.opencode_serve import DEFAULT_CONTINUE_PROMPT, DEFAULT_FINISH_TODOS_PROMPT

    runner = AgentRunner(working_directory=tmp_path)
    cases = (
        ("derman-reviewer", "opencode", "ses_review", REVIEW_MARKER),
        ("derman-test", "opencode", "ses_test", TEST_MARKER),
        ("derman-reviewer", "codex", CODEX_ID, REVIEW_MARKER),
        ("derman-test", "claude", CLAUDE_ID, TEST_MARKER),
    )
    for agent, backend, sid, marker in cases:
        task = _task(agent, backend, marker)
        runner._resume_opencode_session_for_retry(task, sid, why="timeout")
        assert task.session_id == sid
        assert marker in task.prompt
        assert task.prompt != DEFAULT_CONTINUE_PROMPT
        assert task.prompt != DEFAULT_CODEX_RESUME_PROMPT
        assert task.prompt != DEFAULT_CLAUDE_RESUME_PROMPT
        assert "resume implementation" not in task.prompt.lower()

    incomplete = _task("derman-reviewer", "opencode", REVIEW_MARKER)
    runner._resume_opencode_session_for_retry(
        incomplete, "ses_review", why="incomplete_session"
    )
    assert REVIEW_MARKER in incomplete.prompt
    assert incomplete.prompt != DEFAULT_FINISH_TODOS_PROMPT

    locked = _task("derman-test", "codex", TEST_MARKER)
    runner._resume_codex_after_lock(locked, CODEX_ID, lock_hits=1)
    assert locked.session_id == CODEX_ID
    assert TEST_MARKER in locked.prompt

    leftover = _task("derman-reviewer", "codex", REVIEW_MARKER)
    runner._resume_codex_after_lock(
        leftover, CODEX_ID, lock_hits=5, leftover_writer=True
    )
    assert leftover.session_id == CODEX_ID
    assert REVIEW_MARKER in leftover.prompt

    cold = _task("derman-test", "codex", TEST_MARKER)
    runner._resume_codex_after_lock(cold, CODEX_ID, lock_hits=2)
    assert cold.session_id is None
    assert TEST_MARKER in cold.prompt
    assert cold.prompt != DEFAULT_CODEX_COLD_CONTINUE_PROMPT

    build = _task("derman-build", "opencode", BUILD_MARKER)
    runner._resume_opencode_session_for_retry(build, "ses_build", why="timeout")
    assert build.prompt == DEFAULT_CONTINUE_PROMPT

    codex_build = _task("derman-build", "codex", BUILD_MARKER)
    runner._resume_opencode_session_for_retry(codex_build, CODEX_ID, why="error")
    assert codex_build.prompt == DEFAULT_CODEX_RESUME_PROMPT

    claude_build = _task("derman-build", "claude", BUILD_MARKER)
    runner._resume_opencode_session_for_retry(
        claude_build, CLAUDE_ID, why="timeout"
    )
    assert claude_build.prompt == DEFAULT_CLAUDE_RESUME_PROMPT

def _codex_lines(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


class _CodexProc:
    pid = 4242

    def __init__(self, payload: bytes, returncode: int = 0):
        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(payload)
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode


async def _run_codex_capture(tmp_path, monkeypatch, payload: bytes):
    from src.backends.base import AgentRunRequest
    from src.backends import codex as codex_mod
    from src.backends.codex import CodexBackend

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jira-agent").mkdir(exist_ok=True)
    codex_mod._LIVE_CODEX.clear()
    seen: list[str] = []

    async def _fake_exec(*_a, **_k):
        return _CodexProc(payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    try:
        result = await CodexBackend().run(
            AgentRunRequest(
                prompt="build",
                working_directory=tmp_path,
                timeout_seconds=5,
                on_session=seen.append,
            )
        )
    finally:
        codex_mod._LIVE_CODEX.clear()
    return result, seen


@pytest.mark.asyncio
async def test_codex_live_capture_keeps_thread_started_id(tmp_path, monkeypatch):
    quoted = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": f"app log session_id: {QUOTED_ID}",
            },
        }
    )
    started = json.dumps({"type": "thread.started", "thread_id": STARTED_ID})
    result, seen = await _run_codex_capture(
        tmp_path, monkeypatch, _codex_lines(started, quoted)
    )
    assert result.session_id == STARTED_ID
    assert seen == [STARTED_ID]

    later = json.dumps({"type": "thread.started", "thread_id": LATER_ID})
    replaced, replaced_seen = await _run_codex_capture(
        tmp_path, monkeypatch, _codex_lines(started, quoted, later)
    )
    assert replaced.session_id == LATER_ID
    assert replaced_seen == [STARTED_ID, LATER_ID]

    only = json.dumps({"session_id": TOP_LEVEL_ID})
    top, top_seen = await _run_codex_capture(
        tmp_path, monkeypatch, _codex_lines(only)
    )
    assert top.session_id == TOP_LEVEL_ID
    assert top_seen == [TOP_LEVEL_ID]

    chatter_first, chatter_seen = await _run_codex_capture(
        tmp_path, monkeypatch, _codex_lines(quoted, started)
    )
    assert chatter_first.session_id == STARTED_ID
    assert chatter_seen == [STARTED_ID]

@pytest.mark.asyncio
async def test_cancel_serve_aborts_when_settings_backend_is_codex(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "agent_backend", "codex")
    aborted: list[str] = []

    class Client:
        async def abort(self, sid: str) -> None:
            aborted.append(sid)

    runner = AgentRunner()
    runner._running_tasks["task_serve"] = {
        "mode": "serve",
        "client": Client(),
        "session_id": "ses_live",
        "cancel": False,
    }
    assert runner.cancel_task("task_serve") is True
    await asyncio.sleep(0)
    assert aborted == ["ses_live"]


def _processor(tmp_path):
    from src.jira.simulated_client import SimulatedJiraClient
    from src.processor import JobProcessor
    from src.state.manager import JiraStateManager

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sim = SimulatedJiraClient(base_url="http://127.0.0.1:1")
    with patch("src.processor.create_jira_client", return_value=sim):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = sim
    proc.reporter = MagicMock()
    return proc, sm


@pytest.mark.asyncio
async def test_comment_cancel_awaits_serve_abort(tmp_path, monkeypatch):
    from src.config import settings
    from src.state.models import TaskStatus

    monkeypatch.setattr(settings, "agent_backend", "codex")
    proc, sm = _processor(tmp_path)
    aborted: list[str] = []

    class Client:
        async def abort(self, sid: str) -> None:
            await asyncio.sleep(0)
            aborted.append(sid)

    runner = AgentRunner()
    runner._running_tasks["task_9"] = {
        "mode": "serve",
        "client": Client(),
        "session_id": "ses_cancelme",
    }
    proc.agent_runner = runner
    sm.create_state("KAN-7", "cancel me", "d")
    sm.update_state(
        "KAN-7",
        status=TaskStatus.EXECUTING,
        current_task_id="task_9",
        current_opencode_session_id="ses_cancelme",
    )
    await proc._handle_bot_command("KAN-7", "/cancel")
    assert "ses_cancelme" in aborted
    assert sm.get_state("KAN-7").status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_daemon_stop_awaits_serve_abort_before_cancelling_tasks(
    tmp_path, monkeypatch
):
    from src.config import settings
    from src.daemon import JiraAgentDaemon
    from src.state.models import TaskStatus

    monkeypatch.setattr(settings, "agent_backend", "codex")
    proc, sm = _processor(tmp_path)
    aborted: list[str] = []

    class Client:
        async def abort(self, sid: str) -> None:
            await asyncio.sleep(0)
            aborted.append(sid)

    runner = AgentRunner()
    runner._running_tasks["task_stop"] = {
        "mode": "serve",
        "client": Client(),
        "session_id": "ses_stop",
    }
    proc._contexts["KAN-8"] = {"runner": runner}
    sm.create_state("KAN-8", "stop me", "d")
    sm.update_state(
        "KAN-8",
        status=TaskStatus.EXECUTING,
        current_task_id="task_stop",
        current_opencode_session_id="ses_stop",
    )
    order: list[str] = []

    real_shutdown = proc.shutdown_processing

    def shutdown(*, reason: str = "") -> int:
        order.append("shutdown")
        assert aborted == ["ses_stop"]
        return real_shutdown(reason=reason)

    proc.shutdown_processing = shutdown
    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon._stopping = False
    daemon._poller = None
    daemon._dashboard_server = None
    daemon.processor = proc
    fake_task = MagicMock()
    fake_task.cancel.side_effect = lambda: order.append("cancel")
    with patch("sys.exit"):
        with patch("asyncio.all_tasks", return_value=[fake_task]):
            with patch("asyncio.current_task", return_value=MagicMock()):
                with patch("asyncio.gather", new_callable=AsyncMock):
                    await daemon.stop()
    assert order == ["shutdown", "cancel"]
    assert "ses_stop" in aborted
