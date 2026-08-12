"""E2E: retry after timeout / error resumes the same OpenCode session.

Compact is waited out in-session — no extra user prompt.

Covers ``run_agent_with_retry`` → real ``run_agent`` via serve (FakeServe),
not a stub that skips ``_resume_opencode_session_for_retry``.

Run::

    .venv/bin/python -m pytest tests/test_session_resume_e2e.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_opencode_serve_e2e import (
    FakeServeBackend,
    FakeServeClient,
    FakeSilentCompactStopBackend,
)


@pytest.fixture
def runner(tmp_path):
    return AgentRunner(working_directory=tmp_path)


def _serve_settings(s, *, timeout: float = 30.0):
    s.opencode_cli = "opencode"
    s.opencode_serve_url = "http://127.0.0.1:4096"
    s.default_model = "opencode/deepseek-v4-flash-free"
    s.agent_task_timeout_seconds = timeout
    s.agent_task_max_retries = 1
    s.agent_task_retry_delay_seconds = 0
    s.agent_task_retry_backoff_multiplier = 1.0
    s.agent_task_retry_on_timeout = True
    s.agent_task_retry_on_error = True


# ---------------------------------------------------------------------------
# Serve e2e: create_session once; retry reuses ses_ + Continue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_serve_retry_reuses_session_after_incomplete(
    tmp_path, monkeypatch
):
    """max_compact_continues=0 → first run_agent incomplete; retry same ses_."""
    monkeypatch.chdir(tmp_path)
    backend = FakeSilentCompactStopBackend()
    client = FakeServeClient(backend)
    create_calls = {"n": 0}
    orig_create = backend.create_session

    async def counting_create(title: str, **kw):
        create_calls["n"] += 1
        return await orig_create(title, **kw)

    backend.create_session = counting_create  # type: ignore[method-assign]

    runner = AgentRunner(working_directory=tmp_path)
    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_serve_url = "http://fake/"
        s.opencode_serve_max_compact_continues = 0
        s.default_model = "opencode/deepseek-v4-flash-free"
        s.default_agent = "atlas"
        s.agent_task_timeout_seconds = 60
        s.agent_task_max_retries = 1
        s.agent_task_retry_delay_seconds = 0
        s.agent_task_retry_backoff_multiplier = 1.0
        s.agent_task_retry_on_timeout = True
        s.agent_task_retry_on_error = True
        with patch(
            "src.opencode_serve.OpenCodeServeClient",
            return_value=client,
        ):
            task = AgentTask(
                description="serve resume",
                prompt="ORIGINAL SERVE BUILD PROMPT",
                agent="atlas",
                issue_key="E2E-SRV",
            )
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert create_calls["n"] == 1, "must not create a second session"
    assert backend.prompts == ["ORIGINAL SERVE BUILD PROMPT"]
    # Compact-then-stop after wait is incomplete; do not POST Continue.
    assert result["returncode"] == 2
    assert result.get("incomplete") is True
    assert result["retry_info"]["retried"] is False
    assert (result.get("opencode_session_id") or "").startswith("ses_")


class FakeServeFailOnceBackend(FakeServeBackend):
    """First ``send_message`` raises (timeout / error); second turn succeeds."""

    def __init__(
        self,
        *,
        fail_with: BaseException,
        session_id: str = "ses_fail_once",
    ):
        super().__init__(required_compacts=0)
        self.session_id = session_id
        self._fail_with = fail_with
        self._failed_once = False

    async def send_message(self, session_id: str, text: str, **kwargs):
        if not self._failed_once:
            self._failed_once = True
            self.prompts.append(text)
            raise self._fail_with
        return await super().send_message(session_id, text, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,fail_exc,expect_sid",
    [
        ("timeout", TimeoutError("serve HTTP timeout"), "ses_e2e_srv_to"),
        ("error", RuntimeError("serve send boom"), "ses_e2e_srv_err"),
    ],
)
async def test_e2e_serve_retry_reuses_session_after_timeout_or_error(
    tmp_path, monkeypatch, label, fail_exc, expect_sid
):
    """Serve HTTP timeout / send error: retry same ses_ + Continue prompt."""
    monkeypatch.chdir(tmp_path)
    backend = FakeServeFailOnceBackend(fail_with=fail_exc, session_id=expect_sid)
    client = FakeServeClient(backend)
    create_calls = {"n": 0}
    orig_create = backend.create_session

    async def counting_create(title: str, **kw):
        create_calls["n"] += 1
        return await orig_create(title, **kw)

    backend.create_session = counting_create  # type: ignore[method-assign]

    runner = AgentRunner(working_directory=tmp_path)
    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_serve_url = "http://fake/"
        s.opencode_serve_max_compact_continues = 0
        s.default_model = "opencode/deepseek-v4-flash-free"
        s.default_agent = "atlas"
        s.agent_task_timeout_seconds = 60
        s.agent_task_max_retries = 1
        s.agent_task_retry_delay_seconds = 0
        s.agent_task_retry_backoff_multiplier = 1.0
        s.agent_task_retry_on_timeout = True
        s.agent_task_retry_on_error = True
        with patch(
            "src.opencode_serve.OpenCodeServeClient",
            return_value=client,
        ):
            task = AgentTask(
                description=f"serve {label} resume",
                prompt="ORIGINAL SERVE BUILD PROMPT",
                agent="atlas",
                issue_key=f"E2E-SRV-{label.upper()}",
            )
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert create_calls["n"] == 1, create_calls
    assert len(backend.prompts) >= 2, backend.prompts
    assert backend.prompts[0] == "ORIGINAL SERVE BUILD PROMPT"
    assert backend.prompts[1].lstrip().lower().startswith("continue")
    assert result["returncode"] == 0
    assert result["retry_info"]["retried"] is True
    assert result.get("opencode_session_id") == expect_sid


# ---------------------------------------------------------------------------
# Processor e2e: execution workflow records retry reason + same session
# ---------------------------------------------------------------------------


def _params(repo: str, key: str) -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: feature/{key}\n"
        f"Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,sid,first_extra,expect_reason",
    [
        (
            "timeout",
            "ses_prc_to",
            {"returncode": -1, "stderr": "[TIMEOUT]", "timed_out": True},
            "timeout",
        ),
        (
            "error",
            "ses_prc_err",
            {"returncode": 1, "stderr": "agent boom"},
            "error",
        ),
    ],
)
async def test_e2e_processor_retry_keeps_session(
    tmp_path,
    monkeypatch,
    fake_jira,
    reporter,
    isolate_jira_agent_artifacts,
    label,
    sid,
    first_extra,
    expect_reason,
):
    """Execution workflow: timeout / error / compact then success; same session."""
    monkeypatch.chdir(tmp_path)
    key = f"E2E-PRC-{label.upper()[:3]}"
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state(
        key,
        f"{label} resume",
        _params("https://gitlab.example.com/g/r.git", key),
    )
    sm.update_state(key, timeout_seconds=30, max_retries=1)

    mock_git = MagicMock()
    mock_git.work_branch = f"feature/{key}"
    mock_git.target_branch = "develop"
    mock_git.get_working_directory.return_value = tmp_path
    mock_git.ensure_on_work_branch.return_value = True

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = MagicMock(return_value=None)
    proc._snapshot_delivery_baseline = MagicMock()

    real_runner = AgentRunner(working_directory=tmp_path)
    seen: List[tuple] = []

    async def fake_run(task, **kwargs):
        seen.append(
            (task.session_id, (task.prompt or "")[:80], kwargs.get("attempt_number"))
        )
        if len(seen) == 1:
            first = {
                "task_id": task.task_id,
                "stdout": f"Session: {sid}\n",
                "session_file": str(tmp_path / "t1.log"),
                "opencode_session_id": sid,
                "progress": 5,
            }
            first.update(first_extra)
            return first
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": str(tmp_path / "t2.log"),
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(real_runner, "run_agent", fake_run)

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": mock_git, "runner": real_runner}
        return mock_git

    monkeypatch.setattr(proc, "_prepare_git_workspace", fake_prepare)

    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 1
    live.default_agent = "atlas"
    monkeypatch.setattr("src.config.get_settings", lambda: live)

    with patch("src.orchestrator.agent_runner.settings") as s:
        _serve_settings(s)
        await proc._start_execution_workflow(sm.get_state(key))

    assert len(seen) == 2, seen
    assert seen[0][0] is None
    assert seen[1][0] == sid
    assert seen[1][1].lower().startswith("continue")

    st = sm.get_state(key)
    assert st is not None
    meta = st.metadata or {}
    job_id = meta.get("current_job_id") or (meta.get("job_ids") or [None])[-1]
    job = isolate_jira_agent_artifacts["job_store"].get_job(job_id) if job_id else None
    if job and job.get("retry_attempts"):
        reasons = [
            a.get("reason") for a in job["retry_attempts"] if isinstance(a, dict)
        ]
        assert any(expect_reason in str(r) for r in reasons), reasons


@pytest.mark.asyncio
async def test_e2e_processor_compact_does_not_send_another_prompt(
    tmp_path,
    monkeypatch,
    fake_jira,
    reporter,
    isolate_jira_agent_artifacts,
):
    """Compact incomplete: wait already happened; do not POST another user turn."""
    monkeypatch.chdir(tmp_path)
    key = "E2E-PRC-CMP"
    sid = "ses_prc_cmp"
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state(
        key,
        "compact no extra prompt",
        _params("https://gitlab.example.com/g/r.git", key),
    )
    sm.update_state(key, timeout_seconds=30, max_retries=3)

    mock_git = MagicMock()
    mock_git.work_branch = f"feature/{key}"
    mock_git.target_branch = "develop"
    mock_git.get_working_directory.return_value = tmp_path
    mock_git.ensure_on_work_branch.return_value = True

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = MagicMock(return_value=None)
    proc._snapshot_delivery_baseline = MagicMock()

    real_runner = AgentRunner(working_directory=tmp_path)
    seen: List[tuple] = []

    async def fake_run(task, **kwargs):
        seen.append((task.session_id, (task.prompt or "")[:80]))
        return {
            "task_id": task.task_id,
            "returncode": 2,
            "stdout": f"Session: {sid}\n",
            "stderr": "[INCOMPLETE] compact-then-stop",
            "session_file": str(tmp_path / "t1.log"),
            "opencode_session_id": sid,
            "progress": 5,
            "incomplete": True,
            "incomplete_reasons": ["compaction occurred this turn"],
        }

    monkeypatch.setattr(real_runner, "run_agent", fake_run)

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": mock_git, "runner": real_runner}
        return mock_git

    monkeypatch.setattr(proc, "_prepare_git_workspace", fake_prepare)

    live = MagicMock()
    live.agent_task_timeout_seconds = 30
    live.agent_task_max_retries = 3
    live.default_agent = "atlas"
    monkeypatch.setattr("src.config.get_settings", lambda: live)

    with patch("src.orchestrator.agent_runner.settings") as s:
        _serve_settings(s)
        await proc._start_execution_workflow(sm.get_state(key))

    assert len(seen) == 1, seen
    assert seen[0][0] is None
    assert "continue" not in seen[0][1].lower()
