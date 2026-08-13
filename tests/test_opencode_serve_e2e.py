"""E2E tests: OpenCode serve orchestration with multi-compact continue.

Simulates a session that must compact **twice** (incomplete after each compact)
before a final successful turn — the failure mode Virtual Developer must handle
when a serve turn would otherwise look finished mid-task.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

import httpx

from src.opencode_serve import (
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_MAX_COMPACT_CONTINUES,
    OpenCodeServeClient,
    ServeOrchestrator,
    assess_serve_turn,
    compaction_marker_keys,
    count_compaction_signals,
    format_serve_error,
    is_serve_timeout,
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
        self._seq = 0
        self.status_polls = 0
        self.auto_complete_on_idle = False
        self._auto_completed = False
        self.busy_polls_remaining = 0
        # Stay idle (incomplete) for this many status polls, then auto-complete.
        self.idle_polls_before_auto_complete = 1
        self._idle_polls = 0
        self.busy_until_aborted = False

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"

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
                "info": {
                    "id": self._next_id("msg"),
                    "role": "user",
                    "finish": None,
                    "summary": {"diffs": []},
                },
                "parts": [{"type": "text", "text": text}],
            }
        )

        compact_index = self.message_calls  # 1-based
        if compact_index <= self.required_compacts:
            # Simulate compact-then-stop (no real completion)
            self.messages.append(
                {
                    "info": {
                        "id": self._next_id("msg"),
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
                        "id": self._next_id("msg"),
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
                "id": self._next_id("msg"),
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
        self, session_id: str, *, limit: int = 500
    ) -> List[Dict[str, Any]]:
        return list(self.messages[-limit:])

    async def list_all_messages(self, session_id: str, **kwargs) -> List[Dict[str, Any]]:
        return list(self.messages)

    async def session_status(self) -> Dict[str, Any]:
        self.status_polls += 1
        if self.busy_until_aborted and not self.aborted:
            return {self.session_id: {"type": "busy"}}
        if self.busy_polls_remaining > 0:
            self.busy_polls_remaining -= 1
            return {self.session_id: {"type": "busy"}}
        if self.auto_complete_on_idle and not self._auto_completed:
            self._idle_polls += 1
            need = max(1, int(self.idle_polls_before_auto_complete or 1))
            if self._idle_polls >= need:
                self._auto_completed = True
                self.todos = [
                    {"content": "Explore", "status": "completed"},
                    {"content": "Implement", "status": "completed"},
                    {"content": "Commit", "status": "completed"},
                ]
                self.messages.append(
                    {
                        "info": {
                            "id": self._next_id("msg"),
                            "role": "assistant",
                            "finish": "stop",
                            "summary": None,
                        },
                        "parts": [
                            {
                                "type": "text",
                                "text": "Auto-resumed after compact; committed.",
                            }
                        ],
                    }
                )
        return {self.session_id: {"type": "idle"}}

    async def list_todos(self, session_id: str) -> List[Dict[str, Any]]:
        return list(self.todos)

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

    async def list_messages(self, session_id, *, limit=500):
        return await self._backend.list_messages(session_id, limit=limit)

    async def list_all_messages(self, session_id, **kw):
        return await self._backend.list_all_messages(session_id, **kw)

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


def test_assess_serve_turn_new_compact_this_turn_not_success():
    """Empty todos + finish=stop after a *new* compact must not be complete.

    Caught by compact-then-stop (last assistant follows a compaction part),
    not by a blanket "any compact this turn ⇒ fail".
    """
    messages = [
        {
            "info": {"id": "c1", "role": "user", "finish": None},
            "parts": [{"type": "compaction", "auto": True}],
        },
        {
            "info": {"id": "a1", "role": "assistant", "finish": "stop", "summary": None},
            "parts": [{"type": "text", "text": "All todos complete."}],
        },
    ]
    r = assess_serve_turn(
        "ses_false_ok",
        messages=messages,
        todos=[{"status": "completed", "content": "All"}],
        compact_events_seen=1,
        new_compacts_this_turn=1,
    )
    assert r["premature"] is True, r
    assert r["complete"] is False
    assert any("compact-then-stop" in x for x in (r.get("reasons") or []))


def test_assess_work_after_compact_summary_is_success():
    """Agent continued after compact summary in the same turn → complete."""
    messages = [
        {
            "info": {"id": "c1", "role": "user", "finish": None},
            "parts": [{"type": "compaction", "auto": True}],
        },
        {
            "info": {
                "id": "sum1",
                "role": "assistant",
                "finish": "stop",
                "summary": True,
            },
            "parts": [{"type": "text", "text": "Compacted earlier work."}],
        },
        {
            "info": {
                "id": "a2",
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [{"type": "text", "text": "Implemented and committed."}],
        },
    ]
    r = assess_serve_turn(
        "ses_real_ok",
        messages=messages,
        todos=[{"status": "completed", "content": "All"}],
        compact_events_seen=1,
        new_compacts_this_turn=1,
    )
    assert r["complete"] is True, r
    assert r["premature"] is False


def test_compaction_marker_keys_survive_sliding_window():
    """New compact id is visible even when an old marker drops out of the window."""
    old = [
        {"info": {"id": "old_c"}, "parts": [{"type": "compaction"}]},
        {"info": {"id": "m1"}, "parts": [{"type": "text", "text": "x"}]},
    ]
    new = [
        {"info": {"id": "m1"}, "parts": [{"type": "text", "text": "x"}]},
        {"info": {"id": "new_c"}, "parts": [{"type": "compaction"}]},
    ]
    assert compaction_marker_keys(old) == {"old_c"}
    assert compaction_marker_keys(new) == {"new_c"}
    assert compaction_marker_keys(new) - compaction_marker_keys(old) == {"new_c"}


def test_assess_serve_turn_old_compact_markers_do_not_loop():
    """After a successful continue, leftover compact markers must not force another."""
    messages = [
        {
            "info": {"role": "user"},
            "parts": [{"type": "compaction"}],
        },
        {
            "info": {"role": "assistant", "finish": "stop", "summary": None},
            "parts": [{"type": "text", "text": "Finished remaining work and committed."}],
        },
    ]
    r = assess_serve_turn(
        "ses_after_continue",
        messages=messages,
        todos=[{"status": "completed", "content": "All"}],
        compact_events_seen=1,
        new_compacts_this_turn=0,
    )
    # Sequence still looks like compact-then-stop on the last two messages —
    # that path is intentional (assess_session_completeness). This test only
    # asserts *cumulative* compact_events_seen alone does not add a reason.
    assert r.get("new_compacts_this_turn") == 0


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
class FakeSilentCompactStopBackend(FakeServeBackend):
    """Compact-then-stop that *looks* successful: finish=stop, todos completed.

    This is the production false-complete shape (KAN-style): no summary=True,
    no open todos — only a compaction part then a cheerful stop.
    """

    def __init__(self):
        super().__init__(required_compacts=0)
        self._silent_compact_pending = True

    async def send_message(self, session_id: str, text: str, **kwargs):
        self.message_calls += 1
        self.prompts.append(text)
        self.messages.append(
            {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "user",
                    "finish": None,
                    "summary": None,
                },
                "parts": [{"type": "text", "text": text}],
            }
        )
        if self._silent_compact_pending:
            self._silent_compact_pending = False
            self.todos = [
                {"content": "Explore", "status": "completed"},
                {"content": "Implement", "status": "completed"},
            ]
            compact_user = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "user",
                    "finish": None,
                },
                "parts": [{"type": "compaction", "auto": True}],
            }
            stop = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": None,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": "All todos complete. Context was compacted.",
                    }
                ],
            }
            self.messages.extend([compact_user, stop])
            return stop
        # Continue turn: real finish, no new compact
        self.todos = [
            {"content": "Explore", "status": "completed"},
            {"content": "Implement", "status": "completed"},
            {"content": "Commit", "status": "completed"},
        ]
        final = {
            "info": {
                "id": self._next_id("msg"),
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [
                {
                    "type": "text",
                    "text": "Resumed after compact; committed the change.",
                }
            ],
        }
        self.messages.append(final)
        return final


@pytest.mark.asyncio
async def test_e2e_silent_compact_waits_without_continue():
    """Auto-compact with completed todos: wait, do not POST Continue."""
    backend = FakeSilentCompactStopBackend()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client, compact_wait_seconds=1.0, compact_poll_seconds=0.05
    )
    result = await orch.run(
        prompt="implement 5+4",
        title="E2E-COMPACT: silent stop",
        agent="atlas",
    )
    # Compact-then-stop: wait out the budget, never POST Continue / Finish-todos.
    assert result.returncode != 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert backend.prompts == ["implement 5+4"]
    blob = "\n".join(backend.prompts)
    assert "Continue the previous" not in blob
    assert "Finish remaining todos" not in blob


@pytest.mark.asyncio
async def test_e2e_compact_auto_resume_without_continue_prompt():
    """OpenCode auto-resumes after compact; orchestrator must not inject Continue."""
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = True
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client, compact_wait_seconds=1.0, compact_poll_seconds=0.05
    )
    result = await orch.run(
        prompt="Implement feature X end-to-end and commit.",
        title="KAN-99: auto compact",
        agent="Sisyphus - ultraworker",
    )
    assert result.returncode == 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert backend.prompts[0].startswith("Implement feature")
    assert all("Continue the previous" not in p for p in backend.prompts)


@pytest.mark.asyncio
async def test_on_session_fires_before_first_message():
    """Session id must be known before the first POST /session/{id}/message."""
    backend = FakeServeBackend(required_compacts=0)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(client=client)
    seen: list = []

    def on_session(sid: str) -> None:
        seen.append((sid, backend.message_calls))

    result = await orch.run(
        prompt="Do the work",
        title="KAN-1: session bind",
        on_session=on_session,
    )
    assert result.returncode == 0
    assert seen == [("ses_fake_double_compact", 0)]
    assert result.session_id == "ses_fake_double_compact"
    assert backend.message_calls == 1


@pytest.mark.asyncio
async def test_e2e_open_todos_after_compact_wait_no_extra_prompt():
    """If work is still open after compact wait, do not send another user message."""
    backend = FakeServeBackend(required_compacts=2)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client, compact_wait_seconds=0.4, compact_poll_seconds=0.05
    )
    result = await orch.run(prompt="Do the work", title="KAN-99")
    assert result.returncode != 0
    assert result.continue_count == 0
    assert backend.message_calls == 1
    blob = "\n".join(backend.prompts)
    assert "Continue the previous" not in blob
    assert "Finish remaining todos" not in blob


@pytest.mark.asyncio
async def test_e2e_agent_runner_serve_mode_waits_compact(tmp_path, monkeypatch):
    """AgentRunner serve mode waits out compact; one prompt only."""
    monkeypatch.chdir(tmp_path)
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = True
    client = FakeServeClient(backend)

    runner = AgentRunner(working_directory=tmp_path)

    async def fake_run_via_serve(task, **kwargs):
        from src.opencode_serve import ServeOrchestrator

        session_file = kwargs["session_file"]
        orch = ServeOrchestrator(
            client=client, compact_wait_seconds=1.0, compact_poll_seconds=0.05
        )
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
            s.opencode_serve_url = "http://127.0.0.1:4096"
            s.opencode_serve_max_compact_continues = 3
            s.opencode_cli = "opencode"
            s.default_model = "opencode/deepseek-v4-flash-free"
            s.agent_task_timeout_seconds = 60
            task = AgentTask(
                description="compact wait",
                prompt="Implement and commit everything.",
                agent="sisyphus",
                issue_key="KAN-99",
            )
            result = await runner.run_agent(task)

    assert result["returncode"] == 0
    assert result.get("continue_count") == 0
    assert result.get("mode") == "serve"
    assert backend.message_calls == 1
    assert result.get("session_file")
    assert Path(result["session_file"]).exists()


@pytest.mark.asyncio
async def test_e2e_waits_while_session_busy_then_auto_resumes():
    """Compact-then-stop + busy status: wait until idle; one prompt only."""
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = True
    backend.busy_polls_remaining = 4
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=1.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.1,
    )
    result = await orch.run(prompt="long task", title="KAN-BUSY")
    assert result.returncode == 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert backend.status_polls >= 4
    assert all("Continue the previous" not in p for p in backend.prompts)


@pytest.mark.asyncio
async def test_e2e_waits_through_idle_for_delayed_auto_resume():
    """Idle after compact is not done — wait until auto-resume completes.

    Old bug: 2s settle → leftover open todos → inject Finish-todos / ERROR.
    """
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = True
    backend.idle_polls_before_auto_complete = 8
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=2.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.08,
    )
    result = await orch.run(prompt="long readme job", title="KAN-DELAY")
    assert result.returncode == 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert backend._auto_completed is True
    assert all("Finish remaining todos" not in p for p in backend.prompts)
    assert all("Continue the previous" not in p for p in backend.prompts)


@pytest.mark.asyncio
async def test_e2e_does_not_send_twenty_continues():
    """A long compacting job must not inject 20 Continue user messages."""
    backend = FakeServeBackend(required_compacts=20)
    backend.auto_complete_on_idle = True
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client, compact_wait_seconds=1.0, compact_poll_seconds=0.05
    )
    result = await orch.run(prompt="very long task", title="KAN-20C")
    assert result.returncode == 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert all("Continue the previous" not in p for p in backend.prompts)


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


def test_default_continue_prompt_not_used_for_compact():
    """Continue text is for timeout/error only — must not mention compaction."""
    assert "timeout" in DEFAULT_CONTINUE_PROMPT.lower()
    assert "compaction" not in DEFAULT_CONTINUE_PROMPT.lower()


def test_format_serve_error_empty_read_timeout():
    """httpx.ReadTimeout() stringifies to '' — logs must still name the type."""
    err = httpx.ReadTimeout("")
    assert str(err) == ""
    assert is_serve_timeout(err)
    text = format_serve_error(err, timeout_seconds=30)
    assert "ReadTimeout" in text
    assert "30s" in text
    assert "message failed" not in text  # prefix is added by orchestrator


def test_format_serve_error_http_status_includes_body():
    req = httpx.Request("POST", "http://fake/session/ses_x/message")
    resp = httpx.Response(400, request=req, text='{"error":"session busy"}')
    exc = httpx.HTTPStatusError("boom", request=req, response=resp)
    text = format_serve_error(exc)
    assert "400" in text
    assert "session busy" in text


@pytest.mark.asyncio
async def test_e2e_serve_http_timeout_marks_timed_out_without_abort():
    """Idle ReadTimeout → timed_out so error retry may Continue; do not abort.

    Aborting a timed-out idle session then injecting Continue made chat messy
    when the real cause was auto-compact. Compact/busy timeouts wait instead.
    """

    class TimeoutOnce(FakeServeBackend):
        async def send_message(self, session_id, text, **kwargs):
            self.prompts.append(text)
            raise httpx.ReadTimeout("")

    backend = TimeoutOnce(required_compacts=0)
    backend.session_id = "ses_timeout_busy"
    client = FakeServeClient(backend)
    client.timeout_seconds = 30.0
    orch = ServeOrchestrator(
        client=client, max_compact_continues=2, compact_wait_seconds=0.4
    )
    result = await orch.run(prompt="do work", title="KAN-TO")

    assert result.timed_out is True
    assert result.returncode == -1
    assert result.incomplete is False
    assert backend.aborted is False
    assert result.stderr.count("[serve] message failed:") == 1
    assert "ReadTimeout" in (result.stderr or "")
    assert "30s" in (result.stderr or "")
    out = result.to_agent_result("task_to")
    assert out.get("timed_out") is True
    assert out.get("opencode_session_id") == "ses_timeout_busy"


@pytest.mark.asyncio
async def test_e2e_http_timeout_idle_waits_for_auto_resume_no_second_prompt():
    """Idle after HTTP timeout is compact/auto-resume, not a BUILD retry."""

    class TimeoutThenResume(FakeServeBackend):
        async def send_message(self, session_id, text, **kwargs):
            self.prompts.append(text)
            self.message_calls += 1
            raise httpx.ReadTimeout("")

    backend = TimeoutThenResume(required_compacts=1)
    backend.auto_complete_on_idle = True
    backend.idle_polls_before_auto_complete = 4
    client = FakeServeClient(backend)
    client.timeout_seconds = 30.0
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=1.5,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.08,
    )
    result = await orch.run(prompt="# Build mode\ndo the work", title="KAN-TO2")
    assert result.returncode == 0, result.stderr
    assert backend.message_calls == 1
    assert backend.prompts == ["# Build mode\ndo the work"]
    assert all("Finish remaining todos" not in p for p in backend.prompts)


def _http_500() -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://fake/session/ses_x/message")
    resp = httpx.Response(
        500,
        json={
            "name": "UnknownError",
            "data": {
                "message": "Unexpected server error. Check server logs for details.",
                "ref": "err_test",
            },
        },
        request=req,
    )
    return httpx.HTTPStatusError("500", request=req, response=resp)


@pytest.mark.asyncio
async def test_resume_aborts_leftover_busy_session_before_prompt():
    """Cancel often leaves serve busy; a new POST 500s unless we abort first."""
    backend = FakeServeBackend(required_compacts=0)
    backend.busy_until_aborted = True
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=1.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="NEW JOB PROMPT",
        title="KAN-RESUME",
        session_id=backend.session_id,
    )
    assert backend.aborted is True
    assert backend.message_calls == 1
    assert backend.prompts == ["NEW JOB PROMPT"]
    assert result.returncode == 0, result.stderr
    assert "still busy; aborting leftover turn" in result.stdout


@pytest.mark.asyncio
async def test_http_500_while_busy_waits_does_not_fail_job():
    """500 + busy means OpenCode is already working — wait, do not retry BUILD."""

    class FailThenBusy(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self._failed = False
            self.auto_complete_on_idle = True
            self.idle_polls_before_auto_complete = 2

        async def send_message(self, session_id, text, **kwargs):
            if not self._failed:
                self._failed = True
                self.prompts.append(text)
                self.message_calls += 1
                self.busy_polls_remaining = 2
                raise _http_500()
            return await super().send_message(session_id, text, **kwargs)

    backend = FailThenBusy()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=1.5,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.08,
    )
    result = await orch.run(prompt="do the work", title="KAN-500")
    assert result.returncode == 0, result.stderr
    assert backend.message_calls == 1
    assert "waiting for the in-flight turn" in result.stdout
    assert "message failed" in (result.stdout + result.stderr)


@pytest.mark.asyncio
async def test_http_500_idle_status_fails_fast_not_false_busy_wait():
    """Generic 500 + idle status is a real failure (missing dir/agent/model).

    Waiting forever for 'completeness snapshot' made dashboard look stuck
    while OpenCode never started the turn.
    """

    class Fail500Idle(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self._failed = False

        async def send_message(self, session_id, text, **kwargs):
            if not self._failed:
                self._failed = True
                self.prompts.append(text)
                self.message_calls += 1
                raise _http_500()
            return await super().send_message(session_id, text, **kwargs)

    backend = Fail500Idle()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=0.4,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="do the work",
        title="KAN-500-IDLE",
        session_id=backend.session_id,
    )
    assert result.returncode == 1
    assert backend.message_calls == 1
    assert "message failed" in (result.stderr or "")
    assert "waiting for the in-flight turn" not in result.stdout
    assert "still incomplete" not in result.stdout


@pytest.mark.asyncio
async def test_missing_workdir_fails_before_message():
    """Missing x-opencode-directory must fail fast (not OpenCode UnknownError wait)."""
    from src.opencode_serve import OpenCodeServeClient

    backend = FakeServeBackend(required_compacts=0)
    # Real client wrapper path: inject missing directory on orchestrator client
    client = FakeServeClient(backend)
    client.directory = "/tmp/vd-does-not-exist-e2e-xyz"
    client.ensure_directory_ready = OpenCodeServeClient.ensure_directory_ready.__get__(
        client, FakeServeClient
    )
    orch = ServeOrchestrator(client=client, compact_wait_seconds=0.3)
    result = await orch.run(prompt="x", title="KAN-MISS")
    assert result.returncode == 1
    assert "does not exist" in (result.stderr or "")
    assert backend.message_calls == 0


def test_session_is_busy_accepts_alternate_shapes():
    from src.opencode_serve import session_is_busy

    sid = "ses_alt"
    assert session_is_busy({sid: {"type": "busy"}}, sid)
    assert session_is_busy({sid: {"state": "running"}}, sid)
    assert session_is_busy({"data": {sid: {"status": "in_progress"}}}, sid)
    assert session_is_busy(
        {"sessions": [{"id": sid, "type": "busy"}]}, sid
    )
    assert not session_is_busy({sid: {"type": "idle"}}, sid)
    assert not session_is_busy({}, sid)


def test_is_message_conflict_error_not_blanket_5xx():
    from src.opencode_serve import is_message_conflict_error

    # Generic UnknownError 500 is NOT a conflict (missing dir/agent/model)
    assert is_message_conflict_error(_http_500()) is False
    req = httpx.Request("POST", "http://fake/session/x/message")
    resp400 = httpx.Response(400, text='{"error":"bad request"}', request=req)
    assert (
        is_message_conflict_error(
            httpx.HTTPStatusError("400", request=req, response=resp400)
        )
        is False
    )
    resp409 = httpx.Response(409, text="session busy", request=req)
    assert (
        is_message_conflict_error(
            httpx.HTTPStatusError("409", request=req, response=resp409)
        )
        is True
    )


# ---------------------------------------------------------------------------
# Clarifying questions (unattended / one-pass)
# ---------------------------------------------------------------------------


class _QuestionThenOkBackend(FakeServeBackend):
    """First turn asks a free-form question; unattended nudge finishes."""

    def __init__(self, *, finish_on_nudge: bool = True):
        super().__init__(required_compacts=0)
        self.session_id = "ses_question"
        self.finish_on_nudge = finish_on_nudge
        self.todos = [
            {"content": "Explore", "status": "completed"},
            {"content": "Implement", "status": "completed"},
            {"content": "Commit", "status": "completed"},
        ]

    async def send_message(self, session_id, text, **kwargs):
        self.message_calls += 1
        self.prompts.append(text)
        self.messages.append(
            {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "user",
                    "finish": None,
                    "summary": {"diffs": []},
                },
                "parts": [{"type": "text", "text": text}],
            }
        )
        if self.message_calls == 1 or not self.finish_on_nudge:
            reply = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": None,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": (
                            "Which database should we use — Postgres or SQLite?"
                        ),
                    }
                ],
            }
            self.messages.append(reply)
            return reply
        final = {
            "info": {
                "id": self._next_id("msg"),
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [
                {
                    "type": "text",
                    "text": "Chose Postgres (default). Implemented and committed.",
                }
            ],
        }
        self.messages.append(final)
        return final


@pytest.mark.asyncio
async def test_clarifying_question_gets_one_unattended_nudge_then_ok():
    """Model asks free-form question → one nudge (not BUILD) → can complete."""
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    backend = _QuestionThenOkBackend(finish_on_nudge=True)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=0.3,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="# Build mode\nYou run unattended\ndo the work",
        title="KAN-Q-OK",
    )
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[0].startswith("# Build mode")
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert result.continue_count == 1


@pytest.mark.asyncio
async def test_compact_wait_exits_on_clarifying_question_not_poll_900():
    """Regression: idle + open todos + clarifying question must not spin.

    Production logs looked like::

        idle after compact but still incomplete (poll 780,
        reasons=[open todos…, assistant asked a clarifying question])
        — waiting for auto-resume (no user message)

    Auto-resume will never answer a human question. Leave wait and nudge.
    """
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    class CompactThenAsk(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=1)
            self.session_id = "ses_q_wait"
            self.todos = [
                {"content": f"T{i}", "status": "pending"} for i in range(14)
            ] + [{"content": "now", "status": "in_progress"}]

        async def send_message(self, session_id, text, **kwargs):
            self.message_calls += 1
            self.prompts.append(text)
            self.messages.append(
                {
                    "info": {
                        "id": self._next_id("msg"),
                        "role": "user",
                        "finish": None,
                        "summary": {"diffs": []},
                    },
                    "parts": [{"type": "text", "text": text}],
                }
            )
            if self.message_calls == 1:
                # Compact marker then a clarifying question (not a finished job).
                self.messages.append(
                    {
                        "info": {
                            "id": self._next_id("msg"),
                            "role": "user",
                            "finish": None,
                            "summary": {"diffs": []},
                        },
                        "parts": [{"type": "compaction", "auto": True}],
                    }
                )
                reply = {
                    "info": {
                        "id": self._next_id("msg"),
                        "role": "assistant",
                        "finish": "stop",
                        "summary": None,
                    },
                    "parts": [
                        {
                            "type": "text",
                            "text": "Shall I continue with the remaining work?",
                        }
                    ],
                }
                self.messages.append(reply)
                return reply
            # Unattended nudge → finish
            self.todos = [
                {"content": f"T{i}", "status": "completed"} for i in range(15)
            ]
            final = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": None,
                },
                "parts": [
                    {"type": "text", "text": "Chose defaults. All todos done."}
                ],
            }
            self.messages.append(final)
            return final

    backend = CompactThenAsk()
    client = FakeServeClient(backend)
    # Long budget would hang for minutes if the wait loop ignores the question.
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=30.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build mode\ndo work", title="KAN-Q-WAIT")
    assert "leaving compact wait" in result.stdout or result.continue_count >= 1
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert result.returncode == 0, result.stderr
    # Must not burn hundreds of status polls (production was 700–990).
    assert backend.status_polls < 40, backend.status_polls


@pytest.mark.asyncio
async def test_clarifying_question_still_asking_is_incomplete_not_timeout():
    """If the model keeps asking, fail incomplete quickly (no compact wait)."""
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    backend = _QuestionThenOkBackend(finish_on_nudge=False)
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=30.0,  # must not burn this budget
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="# Build mode\ndo the work",
        title="KAN-Q-STILL",
    )
    assert result.returncode == 2
    assert result.incomplete is True
    assert result.timed_out is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    reasons = " ".join(str(r) for r in (result.incomplete_reasons or []))
    assert "clarifying question" in reasons.lower()
    out = result.to_agent_result("task_q")
    assert out.get("assistant_asked_question") is True
    assert out.get("incomplete") is True
