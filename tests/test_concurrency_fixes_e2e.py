"""E2E regression tests for concurrency fixes 2, 3, and 7.

Covers end-to-end paths operators hit:

  * Fix 2 — dashboard cancel vs late ``_begin_workflow_run`` (CAS stickiness)
  * Fix 3 — cancel during slow git clone (no context re-arm, no agent start)
  * Fix 7 — hung git push / glab hard-timeout (no infinite job slot hold)

Also includes optional live smoke against a running daemon (8080) when
``VD_E2E_LIVE=1`` is set (skipped otherwise so CI stays hermetic).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params(
    *,
    mode: str = "build",
    repo: str = "https://gitlab.example.com/group/repo.git",
    source: str = "develop",
    target: str = "main",
) -> str:
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def _make_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_jira,
    state_manager: JiraStateManager,
    reporter,
) -> JobProcessor:
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


# ---------------------------------------------------------------------------
# Fix 2 — Dashboard cancel + begin CAS (HTTP e2e)
# ---------------------------------------------------------------------------


def test_e2e_dashboard_cancel_then_begin_stays_cancelled(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """POST /api/tasks/{key}/cancel then late begin must leave CANCELLED."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("E2E-CAS-1", "summary", _params())
    # PENDING — operator cancel before agent claim
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)
    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)

    # Detail shows cancelable (pending is not terminal)
    detail = client.get("/api/tasks/E2E-CAS-1")
    assert detail.status_code == 200

    r = client.post("/api/tasks/E2E-CAS-1/cancel")
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True
    assert sm.get_state("E2E-CAS-1").status == TaskStatus.CANCELLED

    task = AgentTask(
        description="late begin",
        prompt="p",
        agent="atlas",
        issue_key="E2E-CAS-1",
    )
    job_id = proc._begin_workflow_run(
        sm.get_state("E2E-CAS-1"),
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="atlas",
        job_status="executing",
    )
    assert job_id is None
    assert sm.get_state("E2E-CAS-1").status == TaskStatus.CANCELLED

    # Dashboard still reports terminal; re-cancel rejected
    after = client.get("/api/tasks/E2E-CAS-1").json()
    assert after["status"] == "cancelled"
    assert after.get("can_cancel") is False
    r2 = client.post("/api/tasks/E2E-CAS-1/cancel")
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_e2e_cancel_under_issue_lock_via_dashboard_api(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """HTTP cancel completes while workflow holds the per-issue asyncio lock."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("E2E-LOCK-1", "s", _params())
    sm.update_state(
        "E2E-LOCK-1",
        status=TaskStatus.EXECUTING,
        current_task_id="task-e2e-lock",
    )
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    proc._contexts["E2E-LOCK-1"] = {"git": MagicMock(), "runner": runner}

    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)

    lock = proc._get_issue_lock("E2E-LOCK-1")
    await lock.acquire()
    try:
        # TestClient runs cancel on the event loop path via anyio
        def _post_cancel() -> Any:
            return client.post("/api/tasks/E2E-LOCK-1/cancel")

        # Run cancel in a thread so we simulate "dashboard request" while lock held
        # on this loop (cancel itself does not need the issue lock).
        result_box: list = []

        def worker() -> None:
            result_box.append(_post_cancel())

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive(), "cancel blocked (likely waiting on issue lock)"
        assert result_box, "cancel did not return"
        resp = result_box[0]
        assert resp.status_code == 200, resp.text
        assert resp.json().get("ok") is True
    finally:
        if lock.locked():
            lock.release()

    assert sm.get_state("E2E-LOCK-1").status == TaskStatus.CANCELLED
    assert "E2E-LOCK-1" not in proc._contexts


# ---------------------------------------------------------------------------
# Fix 3 — Cancel during clone: full planning workflow path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_cancel_during_slow_clone_skips_agent(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """Slow clone + dashboard cancel → CANCELLED, no agent, no live context."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    key = "E2E-CLONE-1"
    sm.create_state(key, "plan it", _params(mode="plan"))
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)
    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)

    clone_started = asyncio.Event()
    agent_started = {"called": False}

    async def slow_prepare(state):
        """Stand in for to_thread(clone): yield so cancel can land mid-prep."""
        clone_started.set()
        await asyncio.sleep(0.6)
        # Production checks _is_aborted after clone returns
        if proc._is_aborted(state.issue_key):
            return None
        git = MagicMock()
        git.target_branch = "main"
        git.work_branch = f"feature/{state.issue_key}"
        proc._contexts[state.issue_key] = {
            "git": git,
            "runner": MagicMock(
                run_agent_with_retry=AsyncMock(
                    side_effect=lambda *a, **k: (
                        agent_started.__setitem__("called", True)
                        or {
                            "returncode": 0,
                            "stdout": "plan",
                            "stderr": "",
                            "aborted": False,
                            "timed_out": False,
                        }
                    )
                )
            ),
        }
        return git

    async def fake_agent(*_a, **_k):
        agent_started["called"] = True
        return {
            "returncode": 0,
            "stdout": "plan",
            "stderr": "",
            "aborted": False,
            "timed_out": False,
        }

    with patch.object(proc, "_prepare_git_workspace", side_effect=slow_prepare):
        with patch.object(
            proc,
            "_runner_for",
            side_effect=lambda k: (
                proc._contexts.get(k) or {}
            ).get("runner")
            or MagicMock(run_agent_with_retry=AsyncMock(side_effect=fake_agent)),
        ):
            state = sm.get_state(key)
            workflow = asyncio.create_task(proc._start_planning_workflow(state))

            await asyncio.wait_for(clone_started.wait(), timeout=3.0)
            # Cancel via dashboard while clone is in flight
            def _cancel() -> None:
                client.post(f"/api/tasks/{key}/cancel")

            ct = threading.Thread(target=_cancel)
            ct.start()
            ct.join(timeout=5.0)
            assert not ct.is_alive(), "dashboard cancel hung"

            await asyncio.wait_for(workflow, timeout=10.0)

    final = sm.get_state(key)
    assert final is not None
    assert final.status == TaskStatus.CANCELLED, (
        f"expected cancelled after mid-clone cancel, got {final.status.value}"
    )
    assert agent_started["called"] is False, "agent must not start after cancel"
    assert key not in proc._contexts

    # API reflects terminal
    detail = client.get(f"/api/tasks/{key}").json()
    assert detail["status"] == "cancelled"


@pytest.mark.asyncio
async def test_e2e_begin_rejected_skips_clone_entirely(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """If begin CAS fails, _prepare_git_workspace must never run."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    key = "E2E-NOCLONE-1"
    sm.create_state(key, "s", _params(mode="plan"))
    sm.update_state(
        key,
        status=TaskStatus.CANCELLED,
        error_message="Cancelled from ops dashboard",
        completed_at=datetime.now(),
    )
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)

    prepare = AsyncMock(return_value=MagicMock())
    with patch.object(proc, "_prepare_git_workspace", prepare):
        await proc._start_planning_workflow(sm.get_state(key))

    prepare.assert_not_called()
    assert sm.get_state(key).status == TaskStatus.CANCELLED
    assert key not in proc._contexts


@pytest.mark.asyncio
async def test_e2e_init_git_discards_after_construct_when_cancelled(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """GitManager finishes clone → cancel observed → cleanup, no context."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    key = "E2E-DISCARD-1"
    sm.create_state(key, "s", _params())
    sm.update_state(key, status=TaskStatus.EXECUTING)
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)

    fake_git = MagicMock()
    fake_git.get_working_directory.return_value = tmp_path / "clone"
    fake_git.cleanup = MagicMock(return_value=True)

    def construct_and_cancel(*_a, **_k):
        sm.update_state(
            key,
            status=TaskStatus.CANCELLED,
            error_message="Cancelled from ops dashboard",
            completed_at=datetime.now(),
        )
        return fake_git

    with patch("src.processor.GitManager", side_effect=construct_and_cancel):
        with patch("src.processor.AgentRunner") as AR:
            out = proc._init_git_manager(key)
            assert out is None
            AR.assert_not_called()
            fake_git.cleanup.assert_called_once()
            assert key not in proc._contexts


# ---------------------------------------------------------------------------
# Fix 7 — git / glab hard timeout e2e (push path)
# ---------------------------------------------------------------------------


def test_e2e_git_push_timeout_does_not_hang(tmp_path, monkeypatch):
    """push() with hung subprocess raises/returns within timeout budget."""
    from src.git_manager import GitManager

    monkeypatch.chdir(tmp_path)
    # Minimal fake workspace dir for _run_git cwd check
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".git").mkdir()

    gm = GitManager.__new__(GitManager)
    gm.temp_dir = ws
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    gm.remote_enabled = True
    gm.work_branch = "feature/E2E-PUSH"
    gm.source_branch = "develop"
    gm.target_branch = "main"
    gm.issue_key = "E2E-PUSH"
    gm._pat_for_remote = MagicMock(return_value="glpat-test")
    gm._redact_git_args = lambda args: args
    gm._redact_secret_text = lambda t: t
    gm._git_auth_env = MagicMock(return_value={})
    gm._with_auth_remote = MagicMock()
    gm._scrub_remote_credentials = MagicMock()
    gm.get_current_branch = MagicMock(return_value="feature/E2E-PUSH")

    hung_deadline = {"calls": 0}

    def hung_run(*_a, **kwargs):
        hung_deadline["calls"] += 1
        timeout = kwargs.get("timeout")
        assert timeout is not None and timeout > 0, "push must set subprocess timeout"
        # Simulate TimeoutExpired after "waiting"
        raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

    with patch("src.git_manager.subprocess.run", side_effect=hung_run):
        with patch("src.git_manager.settings") as s:
            s.git_command_timeout_seconds = 30
            # push catches RuntimeError and returns False (or raises on first push)
            ok = gm.push("feature/E2E-PUSH")

    assert ok is False
    assert hung_deadline["calls"] >= 1


def test_e2e_run_glab_timeout_returns_nonzero(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    ws = tmp_path / "repo"
    ws.mkdir()
    gm = GitManager.__new__(GitManager)
    gm.temp_dir = ws
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    gm._glab_env = MagicMock(return_value={})

    with patch(
        "src.git_manager.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="glab", timeout=45),
    ):
        with patch("src.git_manager.settings") as s:
            s.git_command_timeout_seconds = 45
            result = gm._run_glab(["mr", "create", "--fill"], check=False)

    assert result.returncode == -1
    assert "timed out" in (result.stderr or "")


def test_e2e_run_git_timeout_check_true_raises(tmp_path):
    from src.git_manager import GitManager

    ws = tmp_path / "repo"
    ws.mkdir()
    gm = GitManager.__new__(GitManager)
    gm.temp_dir = ws
    gm.remote_url = ""
    gm._pat_for_remote = MagicMock(return_value=None)
    gm._redact_git_args = lambda args: args
    gm._redact_secret_text = lambda t: t
    gm._git_auth_env = MagicMock(return_value=None)

    with patch(
        "src.git_manager.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=60),
    ):
        with patch("src.git_manager.settings") as s:
            s.git_command_timeout_seconds = 60
            with pytest.raises(RuntimeError, match="timed out"):
                gm._run_git(["fetch", "origin"], check=True, auth=True)


# ---------------------------------------------------------------------------
# Combined: cancel wins over late success CAS on complete path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_cancel_sticky_against_late_begin_and_context(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    """Full race: cancel via API, begin refuses, prepare abort-safe, status sticky."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    key = "E2E-STICKY-1"
    sm.create_state(key, "s", _params())
    proc = _make_processor(tmp_path, monkeypatch, fake_jira, sm, reporter)
    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)

    # Start as if about to work
    sm.update_state(key, status=TaskStatus.PENDING)
    r = client.post(f"/api/tasks/{key}/cancel")
    assert r.status_code == 200

    task = AgentTask(description="d", prompt="p", agent="a", issue_key=key)
    assert (
        proc._begin_workflow_run(
            sm.get_state(key),
            status=TaskStatus.EXECUTING,
            task=task,
            workflow_type="execution",
            agent="a",
            job_status="executing",
        )
        is None
    )

    # Simulate clone finishing late and trying to register context
    with patch("src.processor.GitManager") as GM:
        inst = MagicMock()
        inst.get_working_directory.return_value = tmp_path
        inst.cleanup = MagicMock(return_value=True)
        GM.return_value = inst
        out = proc._init_git_manager(key)
        assert out is None
        assert key not in proc._contexts

    assert sm.get_state(key).status == TaskStatus.CANCELLED
    tasks = client.get("/api/tasks").json()
    # Envelope may be list or {tasks: ...}
    rows = tasks if isinstance(tasks, list) else tasks.get("tasks") or tasks.get("items") or []
    match = [t for t in rows if (t.get("issue_key") or t.get("key")) == key]
    if match:
        assert match[0].get("status") in ("cancelled", "CANCELLED", TaskStatus.CANCELLED.value)


# ---------------------------------------------------------------------------
# Live daemon smoke (optional)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("VD_E2E_LIVE", "").strip() not in ("1", "true", "yes"),
    reason="Set VD_E2E_LIVE=1 to hit a running daemon on :8080",
)
def test_e2e_live_backend_and_frontend_reachable():
    """Smoke: live backend /api/meta and frontend SPA root."""
    import httpx

    with httpx.Client(timeout=5.0, verify=False) as c:
        meta = c.get("http://127.0.0.1:8080/api/meta")
        assert meta.status_code == 200
        body = meta.json()
        assert "version" in body or "server_time" in body

        spa = c.get("http://127.0.0.1:5173/")
        assert spa.status_code == 200
        assert "html" in (spa.headers.get("content-type") or "").lower() or "<!DOCTYPE" in spa.text[:200] or "<html" in spa.text[:200].lower()

        # Frontend proxy reaches backend
        proxied = c.get("http://127.0.0.1:5173/api/meta")
        assert proxied.status_code == 200
        assert proxied.json().get("version") == body.get("version")
