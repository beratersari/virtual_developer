"""KAN-12371: Codex resume hits an active writer and must not hour-backoff.

Production log (retries 11–13 of a long Django Codex job)::

    thread-store conflict: thread 01a03397-… already has an active writer
    Error: thread/resume: thread/resume failed: … (code -32600)
    Incomplete_session on attempt 14/3 … retrying in 40960.0s

Root causes this file pins:

1. Any Codex non-zero exit was marked ``incomplete``, so the OpenCode
   256-resume budget + exponential backoff applied (5 * 2^n → hours).
2. Resume reused OpenCode finish-todos / "Continue the previous OpenCode
   session" prompts, which cannot release a Codex thread lock.
3. A second ``exec resume`` against a live writer is retried forever
   instead of starting a new thread from the current files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.backends.codex import (
    DEFAULT_CODEX_COLD_CONTINUE_PROMPT,
    DEFAULT_CODEX_RESUME_PROMPT,
    is_codex_thread_lock_error,
)
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.opencode_serve import DEFAULT_CONTINUE_PROMPT, DEFAULT_FINISH_TODOS_PROMPT
from tests.test_long_job_nudge_todos_jira_e2e import _seed_django_shaped_tree


THREAD_ID = "01a03397-15ff-7941-a5e7-17e23b3d7b82"
LOCK_LOG = (
    "ERROR codex_core::session::session: failed to initialize thread "
    f"persistence: thread-store conflict: thread {THREAD_ID} already "
    "has an active writer\n"
    f"Error: thread/resume: thread/resume failed: thread {THREAD_ID} "
    "already has an active writer (code -32600)"
)


def _lock_result(task_id: str, session_file: Path) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "returncode": 1,
        "stdout": LOCK_LOG,
        "stderr": (
            "[codex] thread-store conflict: already has an active writer"
        ),
        "session_file": str(session_file),
        "opencode_session_id": THREAD_ID,
        "incomplete": False,
        "incomplete_reasons": ["codex thread locked"],
        "thread_locked": True,
        "timed_out": False,
        "backend": "codex",
    }


@pytest.mark.asyncio
async def test_codex_writer_lock_recovers_on_django_tree(tmp_path, monkeypatch):
    """Long Django job: lock → short resume → new thread → success.

    Mirrors KAN-12371: first wall-clock timeout, then the same thread
    refuses resume because the writer is still held.
    """
    monkeypatch.chdir(tmp_path)
    _seed_django_shaped_tree(tmp_path)
    runner = AgentRunner(working_directory=tmp_path)
    seen: List[Dict[str, Optional[str]]] = []
    delays: List[float] = []
    session_file = tmp_path / "kan12371.log"
    session_file.write_text(LOCK_LOG, encoding="utf-8")

    async def fake_run(task, **kwargs):
        seen.append(
            {
                "sid": task.session_id,
                "prompt": task.prompt or "",
                "task_id": task.task_id,
            }
        )
        n = len(seen)
        if n == 1:
            return {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": f"[codex] thread {THREAD_ID}",
                "stderr": "[codex] timed out after 7200s",
                "session_file": str(session_file),
                "opencode_session_id": THREAD_ID,
                "incomplete": False,
                "timed_out": True,
                "backend": "codex",
            }
        if task.session_id == THREAD_ID:
            return _lock_result(task.task_id, session_file)
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "continued django billing from current files",
            "stderr": "",
            "session_file": str(session_file),
            "opencode_session_id": "01bbbbbb-cccc-dddd-eeee-ffffffffffff",
            "incomplete": False,
            "backend": "codex",
        }

    def on_retry(attempt, delay, reason, *rest):
        delays.append(float(delay))
        assert reason in {"timeout", "thread_locked"}
        assert delay < 60.0

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 5
            s.agent_task_retry_backoff_multiplier = 2.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(
                description="Implement multi-app Django billing",
                prompt="BUILD the Django billing + inventory apps",
                agent="build",
                issue_key="KAN-12371",
                backend="codex",
                model="GLM",
            )
            async def _instant(_delay):
                return None

            with patch(
                "src.orchestrator.agent_runner.asyncio.sleep",
                side_effect=_instant,
            ):
                result = await runner.run_agent_with_retry(
                    task,
                    max_retries=3,
                    max_incomplete_retries=256,
                    on_retry=on_retry,
                )

    assert result["returncode"] == 0
    assert result["retry_info"]["retried"] is True
    assert result["retry_info"]["lock_retries_used"] >= 1
    assert result["retry_info"]["incomplete_retries_used"] == 0
    assert all(d <= 15.0 for d in delays)
    assert 40960.0 not in delays
    prompts = [row["prompt"] for row in seen]
    assert DEFAULT_FINISH_TODOS_PROMPT not in prompts
    assert DEFAULT_CONTINUE_PROMPT not in prompts
    assert not any("OpenCode session" in (p or "") for p in prompts)
    # Timeout keeps the thread; first lock retries resume; next lock starts new.
    assert seen[1]["sid"] == THREAD_ID
    assert seen[1]["prompt"] == DEFAULT_CODEX_RESUME_PROMPT
    assert any(row["sid"] is None for row in seen[2:])
    assert any(
        row["prompt"] == DEFAULT_CODEX_COLD_CONTINUE_PROMPT for row in seen[2:]
    )


@pytest.mark.asyncio
async def test_codex_lock_does_not_use_incomplete_256_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(working_directory=tmp_path)
    calls = {"n": 0}
    delays: List[float] = []

    async def fake_run(task, **kwargs):
        calls["n"] += 1
        return _lock_result(task.task_id, tmp_path / "lock.log")

    def on_retry(attempt, delay, reason, *rest):
        delays.append(float(delay))
        assert reason == "thread_locked"

    async def _instant(_delay):
        return None

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 5
            s.agent_task_retry_backoff_multiplier = 2.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(
                description="d",
                prompt="BUILD",
                agent="build",
                backend="codex",
                session_id=THREAD_ID,
            )
            with patch(
                "src.orchestrator.agent_runner.asyncio.sleep",
                side_effect=_instant,
            ):
                result = await runner.run_agent_with_retry(
                    task,
                    max_retries=3,
                    max_incomplete_retries=256,
                    on_retry=on_retry,
                )

    # 1 initial + 3 lock retries, not 1 + 256.
    assert calls["n"] == 4
    assert result["returncode"] == 1
    assert result.get("thread_locked") is True
    assert result["retry_info"]["incomplete_retries_used"] == 0
    assert result["retry_info"]["lock_retries_used"] == 3
    assert delays == [5.0, 10.0, 15.0]
    assert task.session_id is None
    assert task.abandoned_session_id == THREAD_ID


def test_lock_blob_from_kan12371_is_detected():
    assert is_codex_thread_lock_error(LOCK_LOG) is True


def test_leftover_writer_keeps_live_thread(tmp_path):
    """KAN-12375: the live exec is the writer — keep that thread."""
    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="d",
        prompt="BUILD",
        agent="build",
        backend="codex",
        session_id=THREAD_ID,
    )
    runner._resume_codex_after_lock(
        task, THREAD_ID, lock_hits=1, leftover_writer=True
    )
    assert task.session_id == THREAD_ID
    assert task.abandoned_session_id is None
    assert task.prompt == DEFAULT_CODEX_RESUME_PROMPT


@pytest.mark.asyncio
async def test_stream_overflow_retry_keeps_live_thread(tmp_path, monkeypatch):
    """Huge-line crash must continue the thread that is still writing."""
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(working_directory=tmp_path)
    seen: List[Dict[str, Optional[str]]] = []
    delays: List[float] = []
    session_file = tmp_path / "kan12375.log"
    session_file.write_text(
        "[codex] Separator is not found, and chunk exceed the limit\n",
        encoding="utf-8",
    )
    overflow = {
        "task_id": "t0",
        "returncode": -1,
        "stdout": '{"id":"item_9","type":"command_execution"}\n',
        "stderr": (
            "[codex] LimitOverrunError: Separator is not found, "
            "and chunk exceed the limit"
        ),
        "session_file": str(session_file),
        "opencode_session_id": THREAD_ID,
        "incomplete": False,
        "timed_out": False,
        "backend": "codex",
        "stream_overflow": True,
        "leftover_writer": True,
        "thread_locked": True,
    }

    async def fake_run(task, **kwargs):
        seen.append({"sid": task.session_id, "prompt": task.prompt or ""})
        if len(seen) == 1:
            return overflow
        assert task.session_id == THREAD_ID
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "continued the live thread",
            "stderr": "",
            "session_file": str(session_file),
            "opencode_session_id": THREAD_ID,
            "incomplete": False,
            "backend": "codex",
        }

    def on_retry(attempt, delay, reason, *rest):
        delays.append(float(delay))
        assert reason == "thread_locked"

    async def _instant(_delay):
        return None

    with patch.object(runner, "run_agent", side_effect=fake_run):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_max_incomplete_retries = 256
            s.agent_task_retry_delay_seconds = 5
            s.agent_task_retry_backoff_multiplier = 2.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(
                description="KAN-12375 mock diff overflow",
                prompt="BUILD",
                agent="build",
                issue_key="KAN-12375",
                backend="codex",
                session_id=THREAD_ID,
            )
            with patch(
                "src.orchestrator.agent_runner.asyncio.sleep",
                side_effect=_instant,
            ):
                result = await runner.run_agent_with_retry(
                    task,
                    max_retries=3,
                    max_incomplete_retries=256,
                    on_retry=on_retry,
                )

    assert result["returncode"] == 0
    assert len(seen) == 2
    assert seen[0]["sid"] == THREAD_ID
    assert seen[1]["sid"] == THREAD_ID
    assert seen[1]["prompt"] == DEFAULT_CODEX_RESUME_PROMPT
    assert DEFAULT_CODEX_COLD_CONTINUE_PROMPT not in [row["prompt"] for row in seen]
    assert delays and delays[0] >= 10.0


def test_codex_resume_helper_skips_opencode_db(tmp_path):
    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="d",
        prompt="ORIGINAL BUILD",
        agent="build",
        backend="codex",
        issue_key="KAN-12371",
    )
    runner._resume_opencode_session_for_retry(
        task,
        THREAD_ID,
        why="timeout",
        session_file=str(tmp_path / "x.log"),
        timed_out=True,
        stdout="partial",
    )
    assert task.session_id == THREAD_ID
    assert task.prompt == DEFAULT_CODEX_RESUME_PROMPT
    assert "OpenCode" not in (task.prompt or "")
    assert "ORIGINAL BUILD" not in (task.prompt or "")
