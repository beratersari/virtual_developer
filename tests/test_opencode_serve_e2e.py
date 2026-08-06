"""E2E tests: OpenCode serve orchestration with multi-compact continue.

Simulates a session that must compact **twice** (incomplete after each compact)
before a final successful turn — the failure mode Virtual Developer must handle
when ``opencode run`` would otherwise exit 0 mid-task.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.opencode_serve import (
    DEFAULT_CONTINUE_PROMPT,
    OpenCodeServeClient,
    ServeOrchestrator,
    assess_serve_turn,
    count_compaction_signals,
)
from src.opencode_sessions import assess_session_completeness
from src.orchestrator.agent_runner import AgentRunner, AgentTask


# ---------------------------------------------------------------------------
# Fake serve backend: forces two compact-incomplete turns, then success
# ---------------------------------------------------------------------------


class FakeServeBackend:
    """Stateful fake of OpenCode HTTP session APIs.

    Workflow (``required_compacts=2``):
      turn 0 (initial)  → compact incomplete (open todos, summary stop)
      turn 1 (continue) → compact incomplete again
      turn 2 (continue) → real complete (todos done, finish=stop)
    """

    def __init__(self, *, required_compacts: int = 2):
        self.required_compacts = required_compacts
        self.session_id = "ses_fake_double_compact"
        self.messages: List[Dict[str, Any]] = []
        self.todos: List[Dict[str, Any]] = [
            {"content": "Explore", "status": "completed"},
            {"content": "Implement", "status": "in_progress"},
            {"content": "Commit", "status": "pending"},
        ]
        self.message_calls = 0
        self.prompts: List[str] = []
        self.aborted = False

    async def health(self) -> Dict[str, Any]:
        return {"healthy": True, "version": "fake-1.18"}

    async def create_session(self, title: str, **kwargs) -> Dict[str, Any]:
        return {"id": self.session_id, "title": title}

    async def send_message(
        self,
        session_id: str,
        text: str,
        **kwargs,
    ) -> Dict[str, Any]:
        self.message_calls += 1
        self.prompts.append(text)
        # User message
        self.messages.append(
            {
                "info": {"role": "user", "finish": None, "summary": {"diffs": []}},
                "parts": [{"type": "text", "text": text}],
            }
        )

        compact_index = self.message_calls  # 1-based
        if compact_index <= self.required_compacts:
            # Simulate compact-then-stop (no real completion)
            self.messages.append(
                {
                    "info": {
                        "role": "user",
                        "finish": None,
                        "summary": {"diffs": []},
                    },
                    "parts": [{"type": "compaction", "auto": True}],
                }
            )
            self.messages.append(
                {
                    "info": {
                        "role": "assistant",
                        "finish": "stop",
                        "summary": True,
                    },
                    "parts": [
                        {"type": "step-start"},
                        {
                            "type": "text",
                            "text": f"## Compaction summary #{compact_index}\n"
                            "Work was in progress; context compacted.",
                        },
                        {"type": "step-finish"},
                    ],
                }
            )
            return {
                "info": {
                    "role": "assistant",
                    "finish": "stop",
                    "summary": True,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": f"Compacting… (cycle {compact_index})",
                    }
                ],
            }

        # Final successful turn
        self.todos = [
            {"content": "Explore", "status": "completed"},
            {"content": "Implement", "status": "completed"},
            {"content": "Commit", "status": "completed"},
        ]
        final = {
            "info": {
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [
                {"type": "step-start"},
                {
                    "type": "text",
                    "text": "All todos complete. Implemented and committed.",
                },
                {"type": "step-finish"},
            ],
        }
        self.messages.append(final)
        return final

    async def list_messages(
        self, session_id: str, *, limit: int = 50
    ) -> List[Dict[str, Any]]:
        return list(self.messages[-limit:])

    async def list_todos(self, session_id: str) -> List[Dict[str, Any]]:
        return list(self.todos)

    async def session_status(self) -> Dict[str, Any]:
        return {}

    async def abort(self, session_id: str) -> bool:
        self.aborted = True
        return True

    async def summarize(self, session_id: str, **kwargs) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class FakeServeClient(OpenCodeServeClient):
    """OpenCodeServeClient facade backed by FakeServeBackend (no real HTTP)."""

    def __init__(self, backend: FakeServeBackend):
        # Skip real AsyncClient construction
        self.base_url = "http://fake/"
        self.timeout_seconds = 30.0
        self.directory = None
        self._owned_client = False
        self._client = None  # type: ignore
        self._backend = backend

    async def health(self):
        return await self._backend.health()

    async def create_session(self, title: str, **kw):
        return await self._backend.create_session(title, **kw)

    async def send_message(self, session_id, text, **kw):
        return await self._backend.send_message(session_id, text, **kw)

    async def list_messages(self, session_id, *, limit=50):
        return await self._backend.list_messages(session_id, limit=limit)

    async def list_todos(self, session_id):
        return await self._backend.list_todos(session_id)

    async def session_status(self):
        return await self._backend.session_status()

    async def abort(self, session_id):
        return await self._backend.abort(session_id)

    async def summarize(self, session_id, **kw):
        return await self._backend.summarize(session_id, **kw)

    async def aclose(self):
        return await self._backend.aclose()


# ---------------------------------------------------------------------------
# Unit: assess + count
# ---------------------------------------------------------------------------


def test_count_compaction_signals():
    msgs = [
        {"parts": [{"type": "text", "text": "hi"}]},
        {"parts": [{"type": "compaction"}]},
        {"info": {"summary": True}, "parts": [{"type": "text", "text": "sum"}]},
    ]
    assert count_compaction_signals(msgs) == 2


def test_assess_serve_turn_open_todos_and_summary():
    messages = [
        {
            "info": {"role": "assistant", "finish": "stop", "summary": True},
            "parts": [{"type": "text", "text": "compacted"}],
        }
    ]
    todos = [
        {"status": "pending", "content": "Commit"},
        {"status": "in_progress", "content": "Build"},
    ]
    r = assess_serve_turn(
        "ses_x",
        messages=messages,
        todos=todos,
        compact_events_seen=1,
    )
    assert r["premature"] is True
    assert r["open_todos"] == 2


def test_assess_complete_after_final_stop():
    messages = [
        {
            "info": {"role": "assistant", "finish": "stop", "summary": None},
            "parts": [{"type": "text", "text": "done"}],
        }
    ]
    todos = [{"status": "completed", "content": "All"}]
    r = assess_session_completeness(
        "ses_y",
        messages=[
            {
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            }
        ],
        todos=todos,
    )
    assert r["complete"] is True
    assert r["premature"] is False


# ---------------------------------------------------------------------------
# E2E: orchestrator must continue through TWO compacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_double_compact_then_success():
    """Task needs 2 compact cycles; orchestrator continues twice then succeeds."""
    backend = FakeServeBackend(required_compacts=2)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(client=client, max_compact_continues=3)

    result = await orch.run(
        prompt="Implement feature X end-to-end and commit.",
        title="KAN-99: double compact e2e",
        agent="Sisyphus - ultraworker",
    )

    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert result.session_id == "ses_fake_double_compact"
    # initial + 2 continues
    assert backend.message_calls == 3
    assert result.continue_count == 2
    assert result.compact_events >= 2
    # Second and third prompts are continue prompts
    assert backend.prompts[0].startswith("Implement feature")
    assert "compaction" in backend.prompts[1].lower() or "Continue" in backend.prompts[1]
    assert "Continue" in backend.prompts[2] or "compaction" in backend.prompts[2].lower()
    # Turn log shows premature then success
    assert len(result.turns) == 3
    assert result.turns[0]["assessment"]["premature"] is True
    assert result.turns[1]["assessment"]["premature"] is True
    assert result.turns[2]["assessment"]["complete"] is True


@pytest.mark.asyncio
async def test_e2e_double_compact_fails_if_max_continues_too_low():
    """With max_compact_continues=1, two required compacts → incomplete failure."""
    backend = FakeServeBackend(required_compacts=2)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(client=client, max_compact_continues=1)

    result = await orch.run(
        prompt="Do the work",
        title="KAN-99: insufficient continues",
    )

    assert result.returncode == 2
    assert result.incomplete is True
    assert result.continue_count == 1
    # initial + 1 continue only
    assert backend.message_calls == 2
    assert "INCOMPLETE" in (result.stderr or "")


@pytest.mark.asyncio
async def test_e2e_agent_runner_serve_mode_double_compact(tmp_path, monkeypatch):
    """AgentRunner with OPENCODE_RUN_MODE=serve uses orchestrator (2 compacts)."""
    monkeypatch.chdir(tmp_path)
    backend = FakeServeBackend(required_compacts=2)
    client = FakeServeClient(backend)

    runner = AgentRunner(working_directory=tmp_path)

    async def fake_run_via_serve(task, **kwargs):
        # Mirror production path but inject fake client
        from src.opencode_serve import ServeOrchestrator

        session_file = kwargs["session_file"]
        orch = ServeOrchestrator(client=client, max_compact_continues=3)
        lines: List[str] = []
        turn = await orch.run(
            prompt=task.prompt or "",
            title=f"{task.issue_key}: {task.description}",
            agent=task.agent,
            log_lines=lines,
        )
        session_file.write_text(turn.stdout or "", encoding="utf-8")
        result = turn.to_agent_result(task.task_id, session_file=str(session_file))
        if kwargs.get("on_complete"):
            kwargs["on_complete"](result)
        return result

    with patch.object(runner, "_run_agent_via_serve", side_effect=fake_run_via_serve):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.opencode_run_mode = "serve"
            s.opencode_serve_url = "http://127.0.0.1:4096"
            s.opencode_serve_max_compact_continues = 3
            s.opencode_cli = "opencode"
            s.default_model = "opencode/deepseek-v4-flash-free"
            s.agent_task_timeout_seconds = 60
            task = AgentTask(
                description="double compact work",
                prompt="Implement and commit everything.",
                agent="sisyphus",
                issue_key="KAN-99",
            )
            # Force serve branch without real HTTP by patching mode check path
            # We call fake_run_via_serve directly through patch of the method
            # that run_agent invokes when mode==serve.
            result = await runner.run_agent(task)

    assert result["returncode"] == 0
    assert result.get("continue_count") == 2
    assert result.get("compact_events", 0) >= 2
    assert result.get("mode") == "serve"
    assert backend.message_calls == 3
    # Session log written
    assert result.get("session_file")
    assert Path(result["session_file"]).exists()


@pytest.mark.asyncio
async def test_e2e_three_compacts_with_budget_three():
    """Stress: exactly 3 compacts required and max_continues=3 → success."""
    backend = FakeServeBackend(required_compacts=3)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(client=client, max_compact_continues=3)

    result = await orch.run(prompt="long task", title="T")
    assert result.returncode == 0
    assert result.continue_count == 3
    assert backend.message_calls == 4  # 1 initial + 3 continues


@pytest.mark.asyncio
async def test_serve_health_failure():
    class DeadClient(FakeServeClient):
        async def health(self):
            raise ConnectionError("connection refused")

    backend = FakeServeBackend()
    client = DeadClient(backend)
    orch = ServeOrchestrator(client=client, max_compact_continues=2)
    result = await orch.run(prompt="x", title="t")
    assert result.returncode == 1
    assert "unreachable" in (result.stderr or "").lower() or "health" in (
        result.stderr or ""
    ).lower()


def test_default_continue_prompt_mentions_compaction():
    assert "compaction" in DEFAULT_CONTINUE_PROMPT.lower()
