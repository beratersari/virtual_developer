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
    DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT,
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_UNATTENDED_NUDGE_PROMPT,
    IDLE_STUCK_LOG_THRESHOLD,
    OpenCodeServeClient,
    ServeOrchestrator,
    assess_serve_turn,
    compaction_marker_keys,
    count_compaction_signals,
    explain_message_http_error,
    format_serve_error,
    is_serve_timeout,
    known_model_ids_from_payload,
    last_turn_is_live_question,
    model_is_known,
    next_compact_loop_streak,
    turn_should_wait_for_compact,
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

    async def list_known_models(self):
        return list(getattr(self, "known_models", []) or [])

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


def test_turn_should_wait_for_compact_ignores_lifetime_history():
    """KAN-95: old compact markers + finish=unknown must not start compact wait."""
    reasons = [
        "open todos: 2 pending, 1 in_progress",
        "last assistant finish is unfinished ('unknown')",
    ]
    assert (
        turn_should_wait_for_compact(reasons=reasons, new_compacts_this_turn=0)
        is False
    )
    assert (
        turn_should_wait_for_compact(reasons=reasons, new_compacts_this_turn=1)
        is True
    )
    assert (
        turn_should_wait_for_compact(
            reasons=["last assistant followed a compaction message"],
            new_compacts_this_turn=0,
        )
        is True
    )
    assert (
        turn_should_wait_for_compact(
            reasons=["session ended on compaction user part (no follow-up)"],
            new_compacts_this_turn=0,
        )
        is True
    )


def test_last_turn_is_live_question_requires_stop_not_summary():
    assert (
        last_turn_is_live_question(
            {
                "assistant_asked_question": True,
                "last_finish": "stop",
                "last_is_summary": False,
            }
        )
        is True
    )
    assert (
        last_turn_is_live_question(
            {
                "assistant_asked_question": True,
                "last_finish": "unknown",
                "last_is_summary": False,
            }
        )
        is False
    )
    assert (
        last_turn_is_live_question(
            {
                "assistant_asked_question": True,
                "last_finish": "stop",
                "last_is_summary": True,
            }
        )
        is False
    )
    assert last_turn_is_live_question({"last_finish": "stop"}) is False


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
    orch = ServeOrchestrator(client=client)
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
        client=client, compact_wait_seconds=0.4
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
async def test_incomplete_resume_waits_does_not_abort_busy_session():
    """finish=unknown retries must not kill the leftover turn."""
    backend = FakeServeBackend(required_compacts=0)
    backend.busy_polls_remaining = 3
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=1.5,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="Finish remaining todos",
        title="KAN-94-RESUME",
        session_id=backend.session_id,
        abort_busy_session=False,
    )
    assert backend.aborted is False
    assert backend.message_calls == 1
    assert result.returncode == 0, result.stderr
    assert "no abort on incomplete resume" in result.stdout
    assert "aborting leftover turn" not in result.stdout


@pytest.mark.asyncio
async def test_three_idle_after_compact_logs_sends_continue_not_error():
    """Three consecutive idle-after-compact logs → Continue, not ERROR."""
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = False
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=8.0,
        compact_poll_seconds=0.02,
        compact_settle_seconds=0.02,
    )
    result = await orch.run(prompt="do the work", title="KAN-IDLE-STUCK")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert result.timed_out is False
    assert backend.message_calls == 2
    assert any(
        p == DEFAULT_CONTINUE_PROMPT or (p or "").startswith("Continue")
        for p in backend.prompts
    )
    out = result.stdout or ""
    assert "idle-after-compact log seen" in out
    assert out.count("idle after compact but still incomplete") >= IDLE_STUCK_LOG_THRESHOLD
    assert "[INCOMPLETE]" not in (result.stderr or "")
    assert "[TIMEOUT]" not in (result.stderr or "")


@pytest.mark.asyncio
async def test_resume_with_old_compact_markers_does_not_enter_compact_wait():
    """Reused session history must not start a 6h auto-compact busy poll.

    KAN-95: compact_total>0 from the previous run + finish=unknown + open
    todos printed "compact detected" and then thousands of busy polls.
    """

    class HistoricalCompactResume(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_resume_old_compact"
            self.messages = [
                {
                    "info": {
                        "id": "old_compact",
                        "role": "user",
                        "finish": None,
                    },
                    "parts": [{"type": "compaction", "auto": True}],
                },
                {
                    "info": {
                        "id": "old_summary",
                        "role": "assistant",
                        "finish": "stop",
                        "summary": True,
                    },
                    "parts": [
                        {
                            "type": "text",
                            "text": "## Compaction summary\nOld run work.",
                        }
                    ],
                },
            ]
            self.todos = [
                {"content": "Explore Django", "status": "in_progress"},
                {"content": "Implement", "status": "pending"},
                {"content": "Review", "status": "pending"},
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
                    },
                    "parts": [{"type": "text", "text": text}],
                }
            )
            reply = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "unknown",
                    "summary": None,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": "Still exploring the repo; todos remain open.",
                    }
                ],
            }
            self.messages.append(reply)
            return reply

    backend = HistoricalCompactResume()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=8.0,
        compact_poll_seconds=0.02,
        compact_settle_seconds=0.02,
    )
    result = await orch.run(
        prompt="BUILD the Django audit-log package",
        title="KAN-95-RESUME",
        session_id=backend.session_id,
    )
    out = result.stdout or ""
    assert "compact detected" not in out
    assert out.count("waiting for auto-compact (poll") <= 1
    assert backend.message_calls == 1
    assert result.incomplete is True
    assert result.returncode == 2
    assert "[INCOMPLETE]" in (result.stderr or "")
    assert "open todos" in (result.stderr or "").lower()


@pytest.mark.asyncio
async def test_busy_compact_wait_leaves_on_live_stop_question():
    """Status stays busy after a finish=stop question — leave wait and nudge.

    KAN-95: last assistant asked the operator after OMO restore, but
    GET /session/status stayed busy so we never re-assessed.
    """

    class CompactThenBusyQuestion(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=1)
            self.busy_polls_remaining = 80
            self._injected_question = False

        async def session_status(self):
            self.status_polls += 1
            if (
                self.message_calls >= 1
                and not self._injected_question
                and self.status_polls >= 1
            ):
                self._injected_question = True
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
                                "text": (
                                    "Please clarify which approach to use "
                                    "before I proceed.\n"
                                    "1. Fresh start from the ticket\n"
                                    "2. Restore a different plan"
                                ),
                            }
                        ],
                    }
                )
            if self.busy_polls_remaining > 0:
                self.busy_polls_remaining -= 1
                return {self.session_id: {"type": "busy"}}
            return {self.session_id: {"type": "idle"}}

    backend = CompactThenBusyQuestion()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=8.0,
        compact_poll_seconds=0.02,
        compact_settle_seconds=0.02,
    )
    result = await orch.run(prompt="do the work", title="KAN-95-BUSY-Q")
    out = result.stdout or ""
    assert "leaving compact wait for unattended nudge" in out
    assert "busy after compact: assistant asked a clarifying question" in out
    assert backend.message_calls >= 2
    assert any(
        p == DEFAULT_UNATTENDED_NUDGE_PROMPT or "unattended" in (p or "").lower()
        or "no human" in (p or "").lower()
        for p in backend.prompts
    )
    # Must not sit out the compact-wait budget as a timeout.
    assert "[TIMEOUT]" not in (result.stderr or "")
    assert result.timed_out is False


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


def test_known_model_ids_from_config_providers_payload():
    ids = known_model_ids_from_payload(
        {
            "providers": [
                {
                    "id": "opencode",
                    "models": {
                        "big-pickle": {"name": "Big Pickle"},
                        "hy3-free": {},
                    },
                }
            ],
            "default": {"opencode": "big-pickle"},
        }
    )
    assert "opencode/big-pickle" in ids
    assert "opencode/hy3-free" in ids


def test_model_is_known_matches_provider_and_bare_id():
    known = ["opencode/big-pickle", "opencode/hy3-free"]
    assert model_is_known("opencode/big-pickle", known) is True
    assert model_is_known("hy3-free", known) is True
    assert model_is_known("opencode/deepseek-v4-flash-free", known) is False
    assert model_is_known("opencode/deepseek-v4-flash-free", []) is True


def test_explain_message_http_error_does_not_blame_missing_atlas():
    text = explain_message_http_error(
        _http_500(),
        directory=None,
        agent="Atlas - Plan Executor",
        model="opencode/deepseek-v4-flash-free",
    )
    assert "not registered" not in text
    assert "model=" in text
    assert "ProviderModelNotFoundError" in text


@pytest.mark.asyncio
async def test_unknown_model_fails_before_message():
    """Rotated Zen ids must fail closed with the inventory, not UnknownError 500."""
    backend = FakeServeBackend(required_compacts=0)
    client = FakeServeClient(backend)
    client.known_models = ["opencode/big-pickle", "opencode/hy3-free"]
    orch = ServeOrchestrator(client=client, compact_wait_seconds=0.3)
    result = await orch.run(
        prompt="do the work",
        title="KAN-MODEL",
        model="opencode/deepseek-v4-flash-free",
    )
    assert result.returncode == 1
    assert backend.message_calls == 0
    assert "unknown model" in " ".join(result.incomplete_reasons)
    assert "opencode/deepseek-v4-flash-free" in (result.stderr or "")
    assert "opencode/big-pickle" in (result.stderr or "")


@pytest.mark.asyncio
async def test_known_model_still_sends_prompt():
    backend = FakeServeBackend(required_compacts=0)
    client = FakeServeClient(backend)
    client.known_models = ["opencode/big-pickle"]
    orch = ServeOrchestrator(client=client, compact_wait_seconds=0.3)
    result = await orch.run(
        prompt="do the work",
        title="KAN-MODEL-OK",
        model="opencode/big-pickle",
    )
    assert result.returncode == 0, result.stderr
    assert backend.message_calls == 1


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


@pytest.mark.asyncio
async def test_nudge_then_done_with_stale_todos_is_success_not_error():
    """Regression (VOLKAN-style): finished after nudge must not ERROR.

    Sequence that failed in production:
    1. Model asks clarifying question (open todos still pending)
    2. Unattended nudge
    3. Model marks work done (finish=stop) but todo API still shows pending
    4. Old code: incomplete with clarifying question + open todos → dashboard ERROR
    """
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    class QuestionThenDoneStaleTodos(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_stale_todos"
            self.todos = [
                {"content": f"T{i}", "status": "pending"} for i in range(15)
            ] + [{"content": "now", "status": "in_progress"}]
            self._nudge_seen = False

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
                                "Shall I restart the deep exploration and "
                                "begin rewriting the documentation?"
                            ),
                        }
                    ],
                }
                self.messages.append(reply)
                return reply
            # Nudge turn: work done, but leave todos stale (API lag).
            self._nudge_seen = True
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
                        "text": (
                            "All 16 tasks are marked complete. The work was "
                            "finished. There is nothing remaining."
                        ),
                    }
                ],
            }
            self.messages.append(final)
            return final

        async def list_todos(self, session_id):
            # Stay stale for a couple of post-nudge re-fetches, then clear —
            # or stay stale forever to hit clean-stop acceptance.
            if self._nudge_seen and self.message_calls >= 2:
                # Keep stale on purpose: force clean-stop acceptance path.
                return list(self.todos)
            return list(self.todos)

    backend = QuestionThenDoneStaleTodos()
    client = FakeServeClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=0.3,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="# Build mode\nwrite docs",
        title="KAN-STALE",
    )
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "After one nudge still incomplete" not in (result.stderr or "")
    assert "git push" in DEFAULT_UNATTENDED_NUDGE_PROMPT.lower() or (
        "do **not** git push" in DEFAULT_UNATTENDED_NUDGE_PROMPT.lower()
        or "Do **not** git push" in DEFAULT_UNATTENDED_NUDGE_PROMPT
    )


@pytest.mark.asyncio
async def test_nudge_then_compact_then_auto_resume_is_success():
    """Long jobs often compact on the nudge turn; wait, do not fail immediately."""
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    class QuestionThenCompactNudge(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_nudge_compact"
            self.todos = [
                {"content": f"T{i}", "status": "pending"} for i in range(14)
            ] + [{"content": "now", "status": "in_progress"}]
            self.auto_complete_on_idle = True
            self.idle_polls_before_auto_complete = 2

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
                            "text": "Shall I use Postgres or SQLite?",
                        }
                    ],
                }
                self.messages.append(reply)
                return reply
            # Nudge turn compact-then-stop (the long-job production case).
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
            compact = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": True,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": "## Compaction summary\nWork still in progress.",
                    }
                ],
            }
            self.messages.append(compact)
            return compact

    backend = QuestionThenCompactNudge()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=1.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-LONG")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "after unattended nudge still incomplete" not in (result.stderr or "")


@pytest.mark.asyncio
async def test_nudge_then_compact_recap_of_shall_i_is_success_not_error():
    """Production: compact summary quotes 'Shall I…?' + 14 stale todos.

    That used to look like 'still asking after one nudge' and mark ERROR.
    """
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    class QuestionThenRecapCompact(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_nudge_recap"
            self.todos = [
                {"content": f"T{i}", "status": "pending"} for i in range(14)
            ] + [{"content": "now", "status": "in_progress"}]
            self.auto_complete_on_idle = True
            self.idle_polls_before_auto_complete = 2

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
                                "Shall I restart the deep exploration and "
                                "begin rewriting the documentation?"
                            ),
                        }
                    ],
                }
                self.messages.append(reply)
                return reply
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
            compact = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "stop",
                    "summary": True,
                },
                "parts": [
                    {
                        "type": "text",
                        "text": (
                            "## Compaction\nPreviously: Shall I restart the "
                            "deep exploration and begin rewriting the "
                            "documentation? Work still in progress."
                        ),
                    }
                ],
            }
            self.messages.append(compact)
            return compact

    backend = QuestionThenRecapCompact()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=1.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-RECAP")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "After one nudge still incomplete" not in (result.stderr or "")
    assert "assistant asked a clarifying question" not in (result.stderr or "")


@pytest.mark.asyncio
async def test_nudge_then_done_polite_closer_with_stale_todos_is_success():
    """'Let me know if you need anything' after finished work is not a question."""
    from src.opencode_serve import DEFAULT_UNATTENDED_NUDGE_PROMPT

    class QuestionThenCloser(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_closer"
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
                            "text": "Shall I restart the deep exploration?",
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
                        "text": (
                            "All 15 tasks are marked complete. The work was "
                            "finished. Let me know if you need anything else."
                        ),
                    }
                ],
            }
            self.messages.append(final)
            return final

        async def list_todos(self, session_id):
            return list(self.todos)

    backend = QuestionThenCloser()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=0.3,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build\nwrite docs", title="KAN-CLOSER")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "After one nudge still incomplete" not in (result.stderr or "")


@pytest.mark.asyncio
async def test_nudge_then_tool_calls_then_auto_resume_is_success():
    """Nudge HTTP can return finish=tool-calls while the session is still working."""
    class QuestionThenToolsNudge(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_nudge_tools"
            self.todos = [
                {"content": f"T{i}", "status": "pending"} for i in range(4)
            ] + [{"content": "now", "status": "in_progress"}]
            self.auto_complete_on_idle = True
            self.idle_polls_before_auto_complete = 2

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
                reply = {
                    "info": {
                        "id": self._next_id("msg"),
                        "role": "assistant",
                        "finish": "stop",
                        "summary": None,
                    },
                    "parts": [
                        {"type": "text", "text": "Which DB should we use?"}
                    ],
                }
                self.messages.append(reply)
                return reply
            mid = {
                "info": {
                    "id": self._next_id("msg"),
                    "role": "assistant",
                    "finish": "tool-calls",
                    "summary": None,
                },
                "parts": [
                    {"type": "text", "text": "Continuing with defaults…"}
                ],
            }
            self.messages.append(mid)
            return mid

    backend = QuestionThenToolsNudge()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=1.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-TOOLS")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert "after unattended nudge still incomplete" not in (result.stderr or "")


@pytest.mark.asyncio
async def test_nudge_then_stop_with_leftover_todos_waits_then_succeeds():
    """Production 45-min failure: question → nudge → finish=stop + leftover todos.

    The orchestrator used to emit
    ``[INCOMPLETE] after unattended nudge still incomplete: open todos:
    4 pending, 1 in_progress`` and kill the job. After the nudge the model
    is mid-work; we must wait for auto-resume, not terminate.
    """

    class QuestionThenWorkingTodos(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_long_todos"
            self.todos = [
                {"content": f"step-{i}", "status": "pending"} for i in range(10)
            ] + [{"content": "now", "status": "in_progress"}]
            self.auto_complete_on_idle = True
            self.idle_polls_before_auto_complete = 3

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
                            "text": "Shall I continue with the remaining 11 steps?",
                        }
                    ],
                }
                self.messages.append(reply)
                return reply
            self.todos = [
                {"content": f"step-{i}", "status": "pending"} for i in range(4)
            ] + [{"content": "now", "status": "in_progress"}]
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
                            "Continuing with defaults. Implementing the "
                            "remaining modules now."
                        ),
                    }
                ],
            }
            self.messages.append(reply)
            return reply

    backend = QuestionThenWorkingTodos()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=1.5,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(
        prompt="# Build\nimplement the django-sized feature",
        title="KAN-45MIN",
    )
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert "after unattended nudge still incomplete" not in (result.stderr or "")
    assert "Shall I continue" in "\n".join(
        str(p.get("text") or "")
        for m in backend.messages
        for p in (m.get("parts") or [])
        if isinstance(p, dict)
    )


def test_should_wait_after_nudge_helpers():
    from src.opencode_serve import should_wait_after_nudge

    assert should_wait_after_nudge(
        {
            "premature": True,
            "reasons": ["open todos: 4 pending, 1 in_progress"],
            "last_finish": "tool-calls",
        }
    )
    assert should_wait_after_nudge(
        {
            "premature": True,
            "reasons": [
                "open todos: 14 pending, 1 in_progress",
                "session ended on compaction summary (finish=stop, summary=true)",
                "last assistant followed a compaction message (compact-then-stop)",
            ],
            "last_finish": "stop",
            "last_is_summary": True,
        }
    )
    assert should_wait_after_nudge(
        {
            "premature": True,
            "assistant_asked_question": True,
            "last_is_summary": True,
            "reasons": [
                "open todos: 14 pending, 1 in_progress",
                "assistant asked a clarifying question",
                "session ended on compaction summary (finish=stop, summary=true)",
            ],
            "last_finish": "stop",
        }
    )
    assert should_wait_after_nudge(
        {
            "premature": True,
            "reasons": ["open todos: 4 pending, 1 in_progress"],
            "last_finish": "stop",
            "last_is_summary": False,
            "assistant_asked_question": False,
        }
    )
    assert not should_wait_after_nudge(
        {
            "premature": True,
            "assistant_asked_question": True,
            "reasons": ["assistant asked a clarifying question"],
            "last_finish": "stop",
        }
    )
    assert not should_wait_after_nudge(
        {"premature": False, "reasons": [], "last_finish": "stop"}
    )


def test_next_compact_loop_streak_increments_and_resets():
    summary = {
        "premature": True,
        "last_is_summary": True,
        "last_finish": "stop",
        "reasons": ["session ended on compaction summary (finish=stop, summary=true)"],
    }
    work = {
        "premature": False,
        "last_is_summary": False,
        "last_finish": "stop",
        "reasons": [],
    }
    assert (
        next_compact_loop_streak(
            prev_keys=set(),
            next_keys={"c1"},
            assessment=summary,
            consecutive=0,
        )
        == 1
    )
    assert (
        next_compact_loop_streak(
            prev_keys={"c1"},
            next_keys={"c1", "c2"},
            assessment=summary,
            consecutive=1,
        )
        == 2
    )
    # Same keys: do not increment (same cycle)
    assert (
        next_compact_loop_streak(
            prev_keys={"c1", "c2"},
            next_keys={"c1", "c2"},
            assessment=summary,
            consecutive=2,
        )
        == 2
    )
    assert (
        next_compact_loop_streak(
            prev_keys={"c1", "c2"},
            next_keys={"c1", "c2", "c3"},
            assessment=work,
            consecutive=2,
        )
        == 0
    )


class _CompactLoopBackend(FakeServeBackend):
    """Each status poll appends another compact-only cycle (no work turn)."""

    def __init__(self, *, keep_looping_after_abort: bool = False):
        super().__init__(required_compacts=1)
        self.auto_complete_on_idle = False
        self._looping = True
        self.keep_looping_after_abort = keep_looping_after_abort
        self.create_calls = 0

    async def create_session(self, title: str, **kwargs) -> Dict[str, Any]:
        self.create_calls += 1
        return {"id": self.session_id, "title": title}

    async def abort(self, session_id: str) -> bool:
        self.aborted = True
        if not self.keep_looping_after_abort:
            self._looping = False
        else:
            self.required_compacts = 99
        return True

    async def session_status(self) -> Dict[str, Any]:
        self.status_polls += 1
        if self._looping and self.message_calls >= 1:
            self.messages.append(
                {
                    "info": {
                        "id": self._next_id("cmp"),
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
                        "id": self._next_id("sum"),
                        "role": "assistant",
                        "finish": "stop",
                        "summary": True,
                    },
                    "parts": [
                        {
                            "type": "text",
                            "text": f"## Compaction recap #{self.status_polls}",
                        }
                    ],
                }
            )
        return {self.session_id: {"type": "idle"}}


@pytest.mark.asyncio
async def test_compact_loop_aborts_then_continues_same_session():
    """Loop → abort in-flight turn → Continue same session. Job can finish."""
    backend = _CompactLoopBackend()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=5.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
        compact_loop_cycles=3,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-LOOP")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.create_calls == 1
    assert result.session_id == backend.session_id
    assert DEFAULT_CONTINUE_PROMPT not in backend.prompts
    assert backend.prompts[1] == DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT
    assert backend.aborted is True


@pytest.mark.asyncio
async def test_compact_loop_again_after_continue_is_incomplete():
    """One same-session Continue only. A second loop fails (last resort)."""
    backend = _CompactLoopBackend(keep_looping_after_abort=True)
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=5.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
        compact_loop_cycles=3,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-LOOP2")
    assert result.returncode == 2, result.stderr
    assert result.incomplete is True
    assert backend.message_calls == 2
    assert DEFAULT_CONTINUE_PROMPT not in backend.prompts
    assert "auto-compact loop" in (result.stderr or "").lower()


@pytest.mark.asyncio
async def test_compact_then_work_then_compact_is_not_a_loop():
    """A real work turn between compacts must reset the streak and still succeed."""
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = True
    backend.idle_polls_before_auto_complete = 4
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=2.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
        compact_loop_cycles=3,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-OK")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 1
    assert DEFAULT_CONTINUE_PROMPT not in backend.prompts
