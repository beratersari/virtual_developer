"""Session continue proofs.

A later plan revise, review comment, MR follow-up, or plan-job recovery
must stay on the same chat and must still carry that instruction. These
assert the safe outcome. A failure here is the bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.backends.claude import DEFAULT_CLAUDE_RESUME_PROMPT
from src.backends.codex import DEFAULT_CODEX_RESUME_PROMPT
from src.opencode_serve import (
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_PLAN_UNATTENDED_NUDGE_PROMPT,
    ServeOrchestrator,
)
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_opencode_serve_e2e import FakeServeBackend, FakeServeClient

REPO = "https://gitlab.example.com/acme/app.git"
BRANCH = "feature/KAN-9"
TARGET = "develop"
CODEX_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
OPENCODE_ID = "ses_plan_continue"
REVISE = "REVISE_PLAN_USE_POSTGRES"
REVIEW_NOTE = "REVIEW_COMMENT_CHECK_NULL"


def _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    return proc, sm


def _git(tmp_path):
    git = MagicMock()
    git.remote_url = REPO
    git.work_branch = BRANCH
    git.source_branch = BRANCH
    git.target_branch = TARGET
    git.repo_checkouts = []
    git.get_working_directory.return_value = tmp_path
    return git


def _bind(binds, session_id: str, kind: str, backend: str, issue: str = "KAN-9"):
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=session_id,
        issue_key=issue,
        kind=kind,
        backend=backend,
    )


def _state(sm, issue: str, kind: str):
    sm.create_state(issue, "session check", "Mode: plan" if kind == "plan" else "Mode: build")
    sm.update_state(
        issue,
        status=TaskStatus.PLANNING if kind == "plan" else TaskStatus.EXECUTING,
        metadata={
            "workflow_type": "planning" if kind == "plan" else kind,
            "repository_url": REPO,
            "feature_branch": BRANCH,
            "target_branch": TARGET,
        },
    )


def _attach(proc, sm, git, *, issue, kind, backend, session_prompt, agent):
    _state(sm, issue, kind)
    task = AgentTask(
        description="continue",
        prompt=session_prompt,
        agent=agent,
        issue_key=issue,
        backend=backend,
    )
    chosen = proc._attach_bound_opencode_session(issue, task, git)
    return chosen, task


def _plan_prompt() -> str:
    return (
        f"{REVISE}\n"
        "Revise the existing plan. Finish the plan file only. "
        "Do not implement product code or commit."
    )


def _review_prompt() -> str:
    return (
        f"{REVIEW_NOTE}\n"
        "Review only. Answer the new comment. Do not implement or commit."
    )


def _assert_plan_instruction(prompt: str) -> None:
    text = prompt or ""
    lowered = text.lower()
    assert REVISE in text
    assert "do not implement" in lowered
    assert "finish remaining implementation" not in lowered
    assert "resume implementation" not in lowered
    assert "commit steps" not in lowered


def _assert_review_instruction(prompt: str) -> None:
    text = prompt or ""
    lowered = text.lower()
    assert REVIEW_NOTE in text
    assert "do not implement" in lowered
    assert "finish remaining implementation" not in lowered
    assert "resume implementation" not in lowered


def test_opencode_plan_refactor_stays_on_the_plan_chat(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(isolate_jira_agent_artifacts["session_bind_store"], OPENCODE_ID, "plan", "opencode")
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-9",
        kind="plan",
        backend="opencode",
        session_prompt=_plan_prompt(),
        agent="derman-plan",
    )
    assert chosen == OPENCODE_ID
    _assert_plan_instruction(task.prompt)


def test_codex_plan_refactor_keeps_the_revise_instruction(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(isolate_jira_agent_artifacts["session_bind_store"], CODEX_ID, "plan", "codex")
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-9",
        kind="plan",
        backend="codex",
        session_prompt=_plan_prompt(),
        agent="derman-plan",
    )
    assert chosen == CODEX_ID
    assert task.prompt != DEFAULT_CODEX_RESUME_PROMPT
    _assert_plan_instruction(task.prompt)


def test_claude_plan_refactor_keeps_the_revise_instruction(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(isolate_jira_agent_artifacts["session_bind_store"], CLAUDE_ID, "plan", "claude")
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-9",
        kind="plan",
        backend="claude",
        session_prompt=_plan_prompt(),
        agent="derman-plan",
    )
    assert chosen == CLAUDE_ID
    assert task.prompt != DEFAULT_CLAUDE_RESUME_PROMPT
    _assert_plan_instruction(task.prompt)


def test_codex_review_followup_keeps_the_new_comment(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(
        isolate_jira_agent_artifacts["session_bind_store"],
        CODEX_ID,
        "review",
        "codex",
        issue="KAN-10",
    )
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-10",
        kind="review",
        backend="codex",
        session_prompt=_review_prompt(),
        agent="derman-reviewer",
    )
    assert chosen == CODEX_ID
    assert task.prompt != DEFAULT_CODEX_RESUME_PROMPT
    _assert_review_instruction(task.prompt)


def test_codex_build_comment_keeps_the_operator_text(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """An MR/PR follow-up is a new instruction. A repeated build kit is not."""
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(
        isolate_jira_agent_artifacts["session_bind_store"],
        CODEX_ID,
        "build",
        "codex",
        issue="KAN-12",
    )
    _state(sm, "KAN-12", "build")
    comment = "OPERATOR_SAID_ADD_A_NULL_CHECK\nApply this on the existing MR."
    task = AgentTask(
        description="comment",
        prompt=comment,
        agent="derman-build",
        issue_key="KAN-12",
        backend="codex",
    )
    chosen = proc._attach_bound_opencode_session(
        "KAN-12", task, _git(tmp_path), preserve_prompt=True
    )
    assert chosen == CODEX_ID
    assert task.prompt == comment
    assert task.prompt != DEFAULT_CODEX_RESUME_PROMPT


def test_codex_build_requeue_still_uses_the_short_continue(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(isolate_jira_agent_artifacts["session_bind_store"], CODEX_ID, "build", "codex")
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-9",
        kind="build",
        backend="codex",
        session_prompt="FULL BUILD KIT\nimplement KAN-9",
        agent="derman-build",
    )
    assert chosen == CODEX_ID
    assert task.prompt == DEFAULT_CODEX_RESUME_PROMPT


def test_claude_plan_timeout_keeps_the_plan_instruction(tmp_path):
    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="plan",
        prompt=_plan_prompt(),
        agent="derman-plan",
        issue_key="KAN-9",
        backend="claude",
    )
    runner._resume_opencode_session_for_retry(task, CLAUDE_ID, why="timeout")
    assert task.session_id == CLAUDE_ID
    _assert_plan_instruction(task.prompt)


def test_codex_plan_timeout_keeps_the_plan_instruction(tmp_path):
    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="plan",
        prompt=_plan_prompt(),
        agent="derman-plan",
        issue_key="KAN-9",
        backend="codex",
    )
    runner._resume_opencode_session_for_retry(task, CODEX_ID, why="timeout")
    assert task.session_id == CODEX_ID
    _assert_plan_instruction(task.prompt)


def test_opencode_review_followup_keeps_the_new_comment(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    _bind(
        isolate_jira_agent_artifacts["session_bind_store"],
        OPENCODE_ID,
        "review",
        "opencode",
        issue="KAN-11",
    )
    chosen, task = _attach(
        proc,
        sm,
        _git(tmp_path),
        issue="KAN-11",
        kind="review",
        backend="opencode",
        session_prompt=_review_prompt(),
        agent="derman-reviewer",
    )
    assert chosen == OPENCODE_ID
    _assert_review_instruction(task.prompt)


def test_plan_timeout_retry_does_not_say_implement(tmp_path):
    runner = AgentRunner(working_directory=tmp_path)
    task = AgentTask(
        description="plan",
        prompt=_plan_prompt(),
        agent="derman-plan",
        issue_key="KAN-9",
        backend="opencode",
    )
    runner._resume_opencode_session_for_retry(
        task, OPENCODE_ID, why="timeout"
    )
    assert task.session_id == OPENCODE_ID
    assert task.prompt != DEFAULT_CONTINUE_PROMPT
    _assert_plan_instruction(task.prompt)


@pytest.mark.asyncio
async def test_plan_idle_continue_does_not_tell_the_planner_to_implement():
    backend = FakeServeBackend(required_compacts=1)
    backend.auto_complete_on_idle = False
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=8.0,
        compact_poll_seconds=0.02,
        compact_settle_seconds=0.02,
    )
    result = await orch.run(
        prompt=_plan_prompt(),
        title="KAN-9",
        agent="derman-plan",
    )
    assert result.returncode == 0, result.stderr
    assert backend.message_calls >= 2
    continued = [p for p in backend.prompts if REVISE not in (p or "")]
    assert continued, backend.prompts
    for prompt in continued:
        assert prompt != DEFAULT_CONTINUE_PROMPT
        lowered = (prompt or "").lower()
        assert "resume implementation" not in lowered
        assert "commit steps" not in lowered
        assert "plan file" in lowered or prompt == DEFAULT_PLAN_UNATTENDED_NUDGE_PROMPT
