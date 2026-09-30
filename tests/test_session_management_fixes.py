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
