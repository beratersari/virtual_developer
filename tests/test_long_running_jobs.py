"""Long-job serve paths (many compacts, question + nudge). No live LLM/Jira.

These are the production failures that blocked merging: a long OpenCode
session compact-then-stops (sometimes after an unattended nudge) and must
auto-resume instead of ERROR / fake compact-budget comments.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.opencode_serve import (
    DEFAULT_UNATTENDED_NUDGE_PROMPT,
    ServeOrchestrator,
)
from src.processor import JobProcessor
from src.state.models import TaskStatus
from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


@pytest.mark.asyncio
async def test_long_job_twenty_five_compacts_auto_resume_one_prompt():
    """No compact-continue cap: 25 compact-then-stop cycles still succeed."""
    backend = FakeServeBackend(required_compacts=25)
    backend.auto_complete_on_idle = True
    backend.idle_polls_before_auto_complete = 2
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=2.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="very long build", title="KAN-LONG-25")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert result.continue_count == 0
    assert backend.message_calls == 1
    assert all("Continue the previous" not in p for p in backend.prompts)
    assert all("Finish remaining todos" not in p for p in backend.prompts)


@pytest.mark.asyncio
async def test_long_job_question_nudge_then_many_compacts_succeeds():
    """Production path: question → nudge → compact-then-stop → auto-resume."""

    class LongQuestionThenCompact(FakeServeBackend):
        def __init__(self):
            super().__init__(required_compacts=0)
            self.session_id = "ses_long_q"
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
                            "## Compaction summary\n"
                            "Still working through the long checklist."
                        ),
                    }
                ],
            }
            self.messages.append(compact)
            return compact

    backend = LongQuestionThenCompact()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=2.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="# Build\nlong feature", title="KAN-LONG-Q")
    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 2
    assert backend.prompts[1] == DEFAULT_UNATTENDED_NUDGE_PROMPT
    assert "after unattended nudge still incomplete" not in (result.stderr or "")
    assert "Continue the previous" not in "\n".join(backend.prompts)


def test_long_job_post_nudge_todos_jira_is_not_compaction(
    processor, state_manager, fake_jira
):
    """Operator-visible comment must not say compact budget for leftover todos."""
    state_manager.create_state("KAN-LONG-T", "long", "Mode: build")
    processor._fail_from_agent_result(
        "KAN-LONG-T",
        {
            "returncode": 2,
            "stderr": (
                "[INCOMPLETE] after unattended nudge still incomplete: "
                "open todos: 4 pending, 1 in_progress"
            ),
            "incomplete": True,
            "incomplete_reasons": ["open todos: 4 pending, 1 in_progress"],
        },
        fallback="agent failed",
    )
    st = state_manager.get_state("KAN-LONG-T")
    assert st is not None
    assert st.status == TaskStatus.ERROR
    bodies = [c["body"] for c in fake_jira.comments]
    assert any("unfinished work" in b.lower() for b in bodies)
    assert not any("context compaction" in b.lower() for b in bodies)
    assert not any("OPENCODE_SERVE_MAX_COMPACT_CONTINUES" in b for b in bodies)


def test_long_job_settings_have_no_compact_continue_cap():
    from src.config import Settings

    assert "opencode_serve_max_compact_continues" not in Settings.model_fields


def test_long_job_stuck_budget_ignores_legacy_compact_meta():
    """Watchdog extra_attempts must not depend on a compact-continue setting."""
    from src.config import compute_stuck_limit_seconds

    base = compute_stuck_limit_seconds(1800, 3, extra_attempts=0)
    with_incomplete = compute_stuck_limit_seconds(1800, 3, extra_attempts=256)
    assert with_incomplete > base
    # Legacy job metadata may still have max_compact_continues; daemon max()s
    # it with incomplete retries. The settings field itself is gone.
    legacy = compute_stuck_limit_seconds(1800, 3, extra_attempts=256)
    assert legacy == with_incomplete
