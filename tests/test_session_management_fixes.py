"""Session resume, cancel, and the Sessions page.

Review and test retries stay on their own instruction. A Codex live
capture keeps thread.started. Stopping a serve job aborts that session.
A Codex job does not resume a Claude chat from another branch. The
Sessions page lists OpenCode chats and shows which clone a bind uses.
"""

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


class _Git:
    def __init__(self, repo: str, branch: str, target: str = "main"):
        self.remote_url = repo
        self.work_branch = branch
        self.target_branch = target
        self.repo_checkouts = []


def _build_issue(proc, sm, issue: str):
    from src.state.models import TaskStatus

    sm.create_state(issue, "build", "d")
    sm.update_state(
        issue,
        status=TaskStatus.EXECUTING,
        metadata={"workflow_type": "execution"},
    )


def test_codex_resume_skips_claude_id_from_another_branch(
    tmp_path, isolate_jira_agent_artifacts
):
    """Same issue, other work branch: resume Codex, never a Claude UUID."""
    from src.orchestrator.agent_runner import AgentTask

    proc, sm = _processor(tmp_path)
    store = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/app.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-40-requeue",
        target_branch="main",
        session_id=CLAUDE_ID,
        issue_key="KAN-40",
        kind="build",
        backend="claude",
        working_directory=str(tmp_path / "requeue"),
    )
    _build_issue(proc, sm, "KAN-40")
    git = _Git(repo, "feature/KAN-40")
    proc.git_manager = git
    task = AgentTask(
        description="build",
        prompt="BUILD KIT",
        agent="derman-build",
        backend="codex",
        issue_key="KAN-40",
    )
    chosen = proc._attach_bound_opencode_session("KAN-40", task, git)
    assert chosen is None
    assert task.session_id is None
    assert task.prompt == "BUILD KIT"
    assert (
        store.get(
            repo,
            "feature/KAN-40",
            "main",
            kind="build",
            backend="codex",
        )
        is None
    )


def test_codex_resume_still_uses_codex_id_from_another_branch(
    tmp_path, isolate_jira_agent_artifacts
):
    proc, sm = _processor(tmp_path)
    store = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/app.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-41-requeue",
        target_branch="main",
        session_id=CODEX_ID,
        issue_key="KAN-41",
        kind="build",
        backend="codex",
        working_directory=str(tmp_path / "requeue"),
    )
    _build_issue(proc, sm, "KAN-41")
    git = _Git(repo, "feature/KAN-41")
    proc.git_manager = git
    task = AgentTask(
        description="build",
        prompt="BUILD KIT",
        agent="derman-build",
        backend="codex",
        issue_key="KAN-41",
    )
    chosen = proc._attach_bound_opencode_session("KAN-41", task, git)
    assert chosen == CODEX_ID
    assert task.session_id == CODEX_ID
    saved = store.get(
        repo, "feature/KAN-41", "main", kind="build", backend="codex"
    )
    assert saved is not None
    assert saved["session_id"] == CODEX_ID


def test_untagged_uuid_without_a_claude_row_stays_codex(
    tmp_path, isolate_jira_agent_artifacts
):
    proc, sm = _processor(tmp_path)
    store = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/app.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-42-old",
        target_branch="main",
        session_id=UNTAGGED_ID,
        issue_key="KAN-42",
        kind="build",
        working_directory=str(tmp_path / "old"),
    )
    _build_issue(proc, sm, "KAN-42")
    git = _Git(repo, "feature/KAN-42")
    proc.git_manager = git
    task = AgentTask(
        description="build",
        prompt="BUILD KIT",
        agent="derman-build",
        backend="codex",
        issue_key="KAN-42",
    )
    chosen = proc._attach_bound_opencode_session("KAN-42", task, git)
    assert chosen == UNTAGGED_ID


def test_codex_does_not_resume_a_row_that_stores_a_claude_id(
    tmp_path, isolate_jira_agent_artifacts
):
    proc, sm = _processor(tmp_path)
    store = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/app.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-43-requeue",
        target_branch="main",
        session_id=CLAUDE_ID,
        issue_key="KAN-43",
        kind="build",
        backend="claude",
    )
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-43",
        target_branch="main",
        session_id=CLAUDE_ID,
        issue_key="KAN-43",
        kind="build",
        backend="codex",
    )
    _build_issue(proc, sm, "KAN-43")
    git = _Git(repo, "feature/KAN-43")
    proc.git_manager = git
    task = AgentTask(
        description="build",
        prompt="BUILD KIT",
        agent="derman-build",
        backend="codex",
        issue_key="KAN-43",
    )
    chosen = proc._attach_bound_opencode_session("KAN-43", task, git)
    assert chosen is None
    assert task.session_id is None
    assert task.prompt == "BUILD KIT"


def test_claude_scan_failure_still_resumes_exact_codex_bind(
    tmp_path, isolate_jira_agent_artifacts
):
    proc, sm = _processor(tmp_path)
    store = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/app.git"
    store.upsert(
        repository_url=repo,
        branch="feature/KAN-44",
        target_branch="main",
        session_id=CODEX_ID,
        issue_key="KAN-44",
        kind="build",
        backend="codex",
    )
    real_list = store.list_binds
    calls = {"n": 0}

    def wrapped(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise TypeError("session index unavailable")
        return real_list(limit=500)

    store.list_binds = wrapped
    _build_issue(proc, sm, "KAN-44")
    git = _Git(repo, "feature/KAN-44")
    task = AgentTask(
        description="build",
        prompt="BUILD KIT",
        agent="derman-build",
        backend="codex",
        issue_key="KAN-44",
    )
    chosen = proc._attach_bound_opencode_session("KAN-44", task, git)
    assert chosen == CODEX_ID
    assert calls["n"] >= 2


def test_sessions_page_lists_opencode_only_and_keeps_scope(
    tmp_path, isolate_jira_agent_artifacts
):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.state.manager import JiraStateManager
    from src.state.session_bind_store import multi_session_scope, workspace_id_for

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example/group/x.git"
    other = "https://gitlab.example/group/y.git"
    scope = multi_session_scope([repo, other])
    work = "feature/KAN-517"
    target = "main"
    single = binds.upsert(
        repository_url=repo,
        branch=work,
        target_branch=target,
        session_id="ses_single",
        issue_key="KAN-517",
        kind="build",
        backend="opencode",
        working_directory=str(tmp_path / "clone_single"),
    )
    multi = binds.upsert(
        repository_url=repo,
        branch=work,
        target_branch=target,
        session_id="ses_aabbcc",
        issue_key="KAN-517",
        kind="build",
        backend="opencode",
        scope=scope,
        working_directory=str(tmp_path / "multi_ws"),
    )
    binds.upsert(
        repository_url=repo,
        branch=work,
        target_branch=target,
        session_id=CLAUDE_ID,
        issue_key="KAN-517",
        kind="build",
        backend="claude",
        working_directory=str(tmp_path / "claude_ws"),
    )
    binds.upsert(
        repository_url=repo,
        branch=work,
        target_branch=target,
        session_id=CODEX_ID,
        issue_key="KAN-517",
        kind="build",
        backend="codex",
        working_directory=str(tmp_path / "codex_ws"),
    )
    claude_repo = "https://gitlab.example/group/only-claude.git"
    binds.upsert(
        repository_url=claude_repo,
        branch="feature/KAN-1",
        target_branch="main",
        session_id=CLAUDE_ID,
        issue_key="KAN-1",
        kind="build",
        backend="claude",
    )
    sm = JiraStateManager(state_dir=tmp_path / "state-ws")
    client = TestClient(create_dashboard_app(processor=None, state_manager=sm))
    wid = workspace_id_for(repo, work, target)
    stored = binds.list_workspaces(limit=None)
    stored_row = next(item for item in stored if item["workspace_id"] == wid)
    reads = {"n": 0}
    real_list = binds.list_binds

    def _counting_list(*args, **kwargs):
        reads["n"] += 1
        return real_list(*args, **kwargs)

    binds.list_binds = _counting_list
    listing = client.get("/api/opencode-workspaces")
    binds.list_binds = real_list
    assert reads["n"] == 1
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    row = body["workspaces"][0]
    assert row["workspace_id"] == wid
    assert stored_row["session_count"] == 4
    assert row["session_count"] == 2
    hidden = workspace_id_for(claude_repo, "feature/KAN-1", "main")
    assert hidden not in {item["workspace_id"] for item in body["workspaces"]}

    detail_response = client.get(f"/api/opencode-workspaces/{wid}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    by_id = {item["session_id"]: item for item in detail["sessions"]}
    assert set(by_id) == {"ses_single", "ses_aabbcc"}
    assert by_id["ses_aabbcc"]["scope"] == scope
    assert by_id["ses_aabbcc"]["scope"].startswith("multi:")
    assert "clone_single" in (by_id["ses_single"]["working_directory"] or "")
    assert "multi_ws" in (by_id["ses_aabbcc"]["working_directory"] or "")
    assert detail["workspace"]["session_count"] == 2
    missing = client.get(f"/api/opencode-workspaces/{hidden}")
    assert missing.status_code == 404

    raw = client.get("/api/opencode-sessions")
    assert raw.status_code == 200
    raw_backend = {
        item["session_id"]: item.get("backend")
        for item in raw.json()["sessions"]
        if item["session_id"] in {"ses_single", "ses_aabbcc", CLAUDE_ID, CODEX_ID}
    }
    assert raw_backend["ses_single"] == "opencode"
    assert raw_backend["ses_aabbcc"] == "opencode"
    assert raw_backend[CLAUDE_ID] == "claude"
    assert raw_backend[CODEX_ID] == "codex"

    reset = client.delete(f"/api/opencode-sessions/{multi['bind_id']}")
    assert reset.status_code == 200
    again = client.get(f"/api/opencode-workspaces/{wid}")
    assert again.status_code == 200
    left = {item["session_id"] for item in again.json()["sessions"]}
    assert left == {"ses_single"}
    assert single["bind_id"] != multi["bind_id"]
    raw_after = {
        item["session_id"] for item in client.get("/api/opencode-sessions").json()["sessions"]
    }
    assert "ses_aabbcc" not in raw_after
    assert CLAUDE_ID in raw_after
    assert CODEX_ID in raw_after
    assert "ses_single" in raw_after


def test_session_reset_copy_renders():
    page = (WEB / "src/pages/storage/StorageFolderPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "resetBody(target)" in page
    assert "Reset ${target.session_id}?" in page
    assert "' · multi-repo'" in page
    assert "session.bind_id" in page
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to render the session reset copy")
    proc = subprocess.run(
        [npx, "--yes", "tsx", "--tsconfig", "tsconfig.app.json", "src/pages/sessions/sessionResetCopy.test.tsx"],
        cwd=WEB,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, (
        f"session reset copy failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "sessionResetCopy ok" in proc.stdout
