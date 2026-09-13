"""Critical findings from the 2026-09-13 multi-agent review.

These tests assert the *correct* operator-visible behaviour.
If a test FAILS, the production hole is still open.

Real things used (no live Jira):
- C-BE-1: real ``opencode serve`` process + real HTTP session + the
  watchdog's ``_fail_stuck_issue`` path (abort on the daemon loop)
- C-AR-1: real JSON state/job/queue stores + real asyncio cancel/complete
- C-ST-1: real ``SessionBindStore`` files + real ``JiraStateManager``

Run::

    .venv/Scripts/python -m pytest tests/test_multiagent_review_critical_2026.py -v -s
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.daemon import JiraAgentDaemon
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import SessionBindStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _opencode_bin() -> Optional[str]:
    return shutil.which("opencode")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_serve(port: int, log_path: Path) -> subprocess.Popen:
    bin_path = _opencode_bin()
    if not bin_path:
        raise RuntimeError("opencode binary not on PATH")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            bin_path,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            "--log-level",
            "INFO",
        ],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env={**os.environ, "OPENCODE_SERVER_PASSWORD": ""},
    )
    proc._vd_log_f = log_f  # type: ignore[attr-defined]
    return proc


def _stop_serve(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass
    log_f = getattr(proc, "_vd_log_f", None)
    if log_f is not None:
        try:
            log_f.close()
        except Exception:
            pass


async def _wait_health(base: str, *, timeout: float = 60.0) -> None:
    import httpx

    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(f"{base.rstrip('/')}/global/health")
                if resp.status_code == 200:
                    return
            except Exception as exc:
                last_err = exc
            await asyncio.sleep(0.4)
    raise TimeoutError(f"serve health not ready at {base}: {last_err}")


def _processor(tmp_path: Path, isolate: Dict[str, Any], monkeypatch) -> JobProcessor:
    from src.config import settings

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    return proc


class _DummyGit:
    """Minimal git slot so ``_release_context`` can drop in_use."""

    def __init__(self, issue_key: str, temp_dir: Path) -> None:
        self.issue_key = issue_key
        self.temp_dir = temp_dir
        self.cleaned = False

    def cleanup(self, success: Optional[bool] = None) -> None:
        self.cleaned = True

    def get_working_directory(self) -> Path:
        return self.temp_dir


# ---------------------------------------------------------------------------
# C-BE-1 — Watchdog must abort the live OpenCode serve session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_be_1_watchdog_posts_abort_to_real_opencode_serve(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Stuck-job watchdog must POST /session/{id}/abort on the live serve.

    Dashboard Cancel already does this on the event loop
    (``_abort_serve_sessions_for_issue``). The monitor runs
    ``_fail_stuck_issue``: abort the live session on the daemon loop,
    then fail/release off-loop. The old path only called
    ``_kill_children_for_issue`` on a worker thread, so
    ``POST /session/{id}/abort`` never ran.
    """
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    from src.opencode_serve import OpenCodeServeClient

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "opencode-serve.log"
    proc = _start_serve(port, log_path)
    client: Optional[OpenCodeServeClient] = None
    try:
        await _wait_health(base)
        work = tmp_path / "clone"
        work.mkdir()
        client = OpenCodeServeClient(
            base, timeout_seconds=30.0, directory=str(work)
        )
        created = await client.create_session("C-BE-1 watchdog abort")
        sid = str(created.get("id") or "").strip()
        assert sid.startswith("ses_"), f"live serve did not return ses_*: {created!r}"

        abort_posts: List[str] = []
        orig_post = client._client.post

        async def _spy_post(url, *args, **kwargs):
            abort_posts.append(str(url))
            return await orig_post(url, *args, **kwargs)

        client._client.post = _spy_post  # type: ignore[method-assign]

        isolate = isolate_jira_agent_artifacts
        jp = _processor(tmp_path, isolate, monkeypatch)
        issue = "STUCK-1"
        jp.state_manager.create_state(issue, "hung serve turn")
        task = AgentTask(
            description="stuck",
            prompt="do not ask questions; reply with ok",
            agent="derman-build",
            issue_key=issue,
            session_id=sid,
        )
        jp.state_manager.update_state(
            issue,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
            current_task_id=task.task_id,
            current_opencode_session_id=sid,
            metadata={
                "workflow_type": "execution",
                "last_opencode_session_id": sid,
                "opencode_session_ids": [sid],
            },
        )
        runner = AgentRunner(working_directory=work)
        runner._running_tasks[task.task_id] = {
            "mode": "serve",
            "backend": "opencode",
            "client": client,
            "session_id": sid,
            "cancel": False,
        }
        git = _DummyGit(issue, work)
        jp._contexts[issue] = {"git": git, "runner": runner}

        daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
        daemon.processor = jp
        daemon.state_manager = jp.state_manager

        # Same path the stuck monitor uses after the fix.
        await daemon._fail_stuck_issue(
            jp.state_manager.get_state(issue),
            "Job stuck in 'executing' for 9999s (limit 1s).",
        )

        abort_hit = any("/abort" in path for path in abort_posts)
        still_held = issue in jp._contexts
        assert abort_hit, (
            "Watchdog released the job without POST /session/"
            f"{sid}/abort on the live opencode serve "
            f"(posts={abort_posts!r}, context_held={still_held}, "
            f"cleaned={git.cleaned}). Dashboard Cancel aborts first; "
            "the stuck monitor must do the same."
        )
        assert issue not in jp._contexts
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        _stop_serve(proc)


@pytest.mark.asyncio
async def test_c_be_1_dashboard_cancel_does_abort_real_serve_session(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Control: Cancel on the event loop must reach live serve abort."""
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    from src.opencode_serve import OpenCodeServeClient

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = _start_serve(port, tmp_path / "opencode-serve-cancel.log")
    client: Optional[OpenCodeServeClient] = None
    try:
        await _wait_health(base)
        work = tmp_path / "clone-cancel"
        work.mkdir()
        client = OpenCodeServeClient(
            base, timeout_seconds=30.0, directory=str(work)
        )
        created = await client.create_session("C-BE-1 cancel abort")
        sid = str(created.get("id") or "").strip()
        assert sid.startswith("ses_")

        abort_posts: List[str] = []
        orig_post = client._client.post

        async def _spy_post(url, *args, **kwargs):
            abort_posts.append(str(url))
            return await orig_post(url, *args, **kwargs)

        client._client.post = _spy_post  # type: ignore[method-assign]

        jp = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
        issue = "STOP-1"
        jp.state_manager.create_state(issue, "cancel serve")
        task = AgentTask(
            description="live",
            prompt="ok",
            agent="derman-build",
            issue_key=issue,
            session_id=sid,
        )
        jp.state_manager.update_state(
            issue,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
            current_task_id=task.task_id,
            current_opencode_session_id=sid,
            metadata={"last_opencode_session_id": sid, "opencode_session_ids": [sid]},
        )
        runner = AgentRunner(working_directory=work)
        runner._running_tasks[task.task_id] = {
            "mode": "serve",
            "backend": "opencode",
            "client": client,
            "session_id": sid,
            "cancel": False,
        }
        jp._contexts[issue] = {"git": _DummyGit(issue, work), "runner": runner}

        result = await jp.cancel_job(issue, reason="operator stop")
        assert result.get("ok") is True
        assert any("/abort" in path for path in abort_posts), (
            "Dashboard cancel did not POST /session abort "
            f"(posts={abort_posts!r})"
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        _stop_serve(proc)


# ---------------------------------------------------------------------------
# C-AR-1 — Old GitLab/Azure worker must not complete/fail a newer run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_ar_1_cancel_leaves_queued_gitlab_followup(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Stop must not start the leftover GitLab note until the old worker is done.

    ``cancel_job`` finishes the running queue row only
    (``include_queued=False``) then ``_release_context`` → ``_kick_queue``.
    A queued /yaver for the same issue is allowed to force CANCELLED →
    PENDING and begin a new EXECUTING run while the old stack is still
    inside ``asyncio.to_thread`` git / a late ``_complete_work``.
    """
    jp = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    issue = "GL-HOLD-1"
    jp.state_manager.create_state(issue, "feat(GL-HOLD-1): login")
    task = AgentTask(
        description="first /yaver",
        prompt="fix tests",
        agent=WorkflowRouter.get_agent_for_workflow(WorkflowType.EXECUTION),
        issue_key=issue,
    )
    jp.state_manager.update_state(
        issue,
        status=TaskStatus.EXECUTING,
        current_task_id=task.task_id,
        metadata={"workflow_type": "gitlab_mr", "source": "gitlab"},
    )
    job_rec = jp.job_store.create_job(
        issue_key=issue, summary="first", status="executing"
    )
    job_id = job_rec["job_id"]
    jp._active_jobs[issue] = job_id
    jp.state_manager.update_state(issue, metadata={"current_job_id": job_id})

    running = jp.queue_store.enqueue(
        source="gitlab",
        issue_key=issue,
        summary="first note",
        payload={"issue_key": issue, "note_id": "n1"},
    )
    jp.queue_store.update(running["queue_id"], status="running", job_id=job_id)
    leftover = jp.queue_store.enqueue(
        source="gitlab",
        issue_key=issue,
        summary="second note",
        payload={"issue_key": issue, "note_id": "n2"},
    )

    result = await jp.cancel_job(issue, reason="operator stop")
    assert result.get("ok") is True, result
    st = jp.state_manager.get_state(issue)
    assert st is not None
    assert st.status == TaskStatus.CANCELLED

    leftover_row = jp.queue_store.get(leftover["queue_id"])
    assert leftover_row is not None
    # Correct: do not claim the next /yaver until the cancelled worker has
    # fully left the issue (no late complete/fail against a new EXECUTING).
    assert leftover_row.get("status") == "queued", (
        f"Cancel immediately dispatched leftover GitLab queue row "
        f"(status={leftover_row.get('status')!r}). "
        "_release_context → _kick_queue starts the next note while the "
        "old worker can still write state."
    )


@pytest.mark.asyncio
async def test_c_ar_1_old_complete_work_must_not_complete_new_run(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """After cancel + a new GitLab begin, the old worker's complete is stale.

    ``_complete_work`` CAS-es ``expected=EXECUTING`` with no job_id
    generation. Cancel writes CANCELLED; the leftover note force-resets
    to PENDING and ``_begin_workflow_run`` writes EXECUTING again. The
    cancelled stack then completes the *new* job.
    """
    jp = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    issue = "GL-RACE-1"
    jp.state_manager.create_state(issue, "feat(GL-RACE-1): login")
    old_task = AgentTask(
        description="old",
        prompt="old prompt",
        agent="derman-build",
        issue_key=issue,
    )
    jp.state_manager.update_state(
        issue,
        status=TaskStatus.EXECUTING,
        current_task_id=old_task.task_id,
        metadata={"workflow_type": "gitlab_mr", "source": "gitlab"},
    )
    old_job = jp.job_store.create_job(
        issue_key=issue, summary="old", status="executing"
    )["job_id"]
    jp._active_jobs[issue] = old_job
    jp.state_manager.update_state(issue, metadata={"current_job_id": old_job})
    old_snapshot = jp.state_manager.get_state(issue)
    assert old_snapshot is not None

    cancelled = await jp.cancel_job(issue, reason="operator stop")
    assert cancelled.get("ok") is True
    assert jp.state_manager.get_state(issue).status == TaskStatus.CANCELLED

    # Leftover GitLab note accept (same as _run_gitlab_mr_comment).
    jp.state_manager.update_state(
        issue,
        force=True,
        status=TaskStatus.PENDING,
        error_message=None,
        completed_at=None,
        current_task_id=None,
        current_opencode_session_id=None,
        metadata={"workflow_type": "gitlab_mr", "source": "gitlab"},
    )
    new_task = AgentTask(
        description="new",
        prompt="new prompt",
        agent="derman-build",
        issue_key=issue,
    )
    new_job = jp._begin_workflow_run(
        jp.state_manager.get_state(issue),
        status=TaskStatus.EXECUTING,
        task=new_task,
        workflow_type="gitlab_mr",
        agent="derman-build",
        job_status="executing",
    )
    assert new_job, "new GitLab run should claim EXECUTING after cancel"
    assert new_job != old_job

    # Old worker finishes as if cancel never happened.
    await jp._complete_work(old_snapshot, execution_summary="stale success")

    live = jp.state_manager.get_state(issue)
    assert live is not None
    new_rec = jp.job_store.get_job(new_job) or {}
    assert live.status == TaskStatus.EXECUTING, (
        f"Old _complete_work stamped {live.status.value} on the new run "
        f"(old_job={old_job} new_job={new_job} new_job_status="
        f"{new_rec.get('status')!r}). Fail/complete must be generation-aware."
    )
    assert new_rec.get("status") != "completed"


@pytest.mark.asyncio
async def test_c_ar_1_old_git_missing_must_not_fail_new_run(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """``_finish_after_git_missing`` fails any current in-flight status."""
    jp = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    issue = "GL-RACE-2"
    jp.state_manager.create_state(issue, "feat(GL-RACE-2)")
    jp.state_manager.update_state(
        issue,
        force=True,
        status=TaskStatus.EXECUTING,
        metadata={"workflow_type": "gitlab_mr", "current_job_id": "job_new"},
    )
    new_job = jp.job_store.create_job(
        issue_key=issue, summary="new", status="executing"
    )
    jp._active_jobs[issue] = new_job
    jp.state_manager.update_state(issue, metadata={"current_job_id": new_job})

    # Old cancelled worker: git init returned None because *it* was aborted.
    jp._finish_after_git_missing(issue)

    live = jp.state_manager.get_state(issue)
    assert live is not None
    assert live.status == TaskStatus.EXECUTING, (
        f"_finish_after_git_missing overwrote the new run with "
        f"{live.status.value}. A cancelled clone must not fail a later job."
    )


# ---------------------------------------------------------------------------
# C-ST-1 — GitLab/Azure build must not resume a plan session
# ---------------------------------------------------------------------------


def test_c_st_1_gitlab_build_does_not_resume_plan_session_from_issue_history(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Plan ses_* on a different work branch must not become a build resume.

    Kind maps exclude a live ``kind=plan`` bind for the *same*
    repo+work+target. When the GitLab MR source is not ``feature/{KEY}``,
    that bind misses and ``last_opencode_session_id`` from the plan run
    is still injected because ``kind != plan``.
    """
    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    jp = _processor(tmp_path, isolate, monkeypatch)
    issue = "KAN-88"
    repo = "https://gitlab.example.com/acme/app.git"
    plan_sid = "ses_plan_kan88"

    jp.state_manager.create_state(issue, "plan login")
    jp.state_manager.update_state(
        issue,
        status=TaskStatus.ERROR,
        current_opencode_session_id=None,
        metadata={
            "workflow_type": "planning",
            "repository_url": repo,
            "source_branch": "develop",
            "target_branch": "develop",
            "feature_branch": "feature/KAN-88",
            "last_opencode_session_id": plan_sid,
            "opencode_session_ids": [plan_sid],
        },
    )
    stored = store.upsert(
        repository_url=repo,
        branch="feature/KAN-88",
        target_branch="develop",
        session_id=plan_sid,
        issue_key=issue,
        kind="plan",
    )
    assert stored is not None

    # GitLab /yaver on the MR source (develop), after the plan failed.
    jp.state_manager.update_state(
        issue,
        force=True,
        status=TaskStatus.EXECUTING,
        current_opencode_session_id=None,
        metadata={
            "workflow_type": "gitlab_mr",
            "source": "gitlab",
            "repository_url": repo,
            "source_branch": "develop",
            "target_branch": "develop",
            "feature_branch": "develop",
            "last_opencode_session_id": plan_sid,
            "opencode_session_ids": [plan_sid],
        },
    )

    class _Git:
        remote_url = repo
        work_branch = "develop"
        target_branch = "develop"

    kind = jp._session_kind_for_issue(issue)
    assert kind == "build", f"gitlab_mr + EXECUTING should be kind=build, got {kind!r}"

    sids, _forgotten, _wd = jp._resume_session_candidates(issue, _Git())
    assert plan_sid not in sids, (
        f"GitLab build resume candidates include the plan session {plan_sid} "
        f"({sids!r}). Plan and build chats must stay separate when the MR "
        "source is not the plan work branch."
    )
