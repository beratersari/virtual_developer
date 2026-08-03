"""Prove git clone/workspace prep does not freeze the asyncio event loop.

Large clones used to run ``subprocess.run`` on the same loop as uvicorn,
making the ops dashboard unreachable until clone finished.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.state.models import TaskStatus


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


@pytest.mark.asyncio
async def test_prepare_git_workspace_yields_event_loop(processor, state_manager):
    """While clone runs, other coroutines on the loop must still get ticks."""
    state = state_manager.create_state(
        "NB-1",
        "nonblocking",
        (
            "{params}\n"
            "Repository: https://gitlab.com/example/large.git\n"
            "Source branch: develop\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "{params}"
        ),
    )

    def slow_blocking(st):
        time.sleep(0.8)
        git = MagicMock()
        git.target_branch = "develop"
        git.work_branch = "feature/NB-1"
        return git

    ticks: list[float] = []

    async def ticker():
        # If the event loop is blocked, we get 0–1 ticks; free loop → many.
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    with patch.object(
        processor, "_prepare_git_workspace_blocking", side_effect=slow_blocking
    ):
        prep_task = asyncio.create_task(processor._prepare_git_workspace(state))
        tick_task = asyncio.create_task(ticker())
        git, _ = await asyncio.gather(prep_task, tick_task)

    assert git is not None
    assert len(ticks) >= 8, (
        f"event loop was blocked during clone-like work "
        f"(only {len(ticks)} ticks; need >= 8)"
    )


@pytest.mark.asyncio
async def test_dashboard_api_responsive_during_slow_clone(processor, state_manager):
    """E2E-ish: dashboard HTTP stays responsive while workspace prep is slow."""
    state = state_manager.create_state(
        "NB-UI",
        "ui freeze check",
        (
            "{params}\n"
            "Repository: https://gitlab.com/example/huge.git\n"
            "Source branch: develop\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "{params}"
        ),
    )
    state_manager.update_state("NB-UI", status=TaskStatus.PENDING)

    app = create_dashboard_app(
        state_manager=state_manager,
        processor=processor,
    )
    client = TestClient(app)

    def slow_blocking(st):
        # Simulate multi-second large-repo clone on a worker thread
        time.sleep(1.0)
        git = MagicMock()
        git.target_branch = "develop"
        git.work_branch = "feature/NB-UI"
        git.get_working_directory.return_value = "/tmp/nb-ui"
        return git

    latencies: list[float] = []

    async def probe_meta():
        # Run sync TestClient calls in a thread so we do not nest loops badly;
        # measure that probes complete quickly while prepare is outstanding.
        for _ in range(5):
            t0 = time.perf_counter()
            # starlette TestClient is sync; offload so event loop free
            def _hit():
                return client.get("/api/meta")

            resp = await asyncio.to_thread(_hit)
            latencies.append(time.perf_counter() - t0)
            assert resp.status_code == 200
            await asyncio.sleep(0.1)

    with patch.object(
        processor, "_prepare_git_workspace_blocking", side_effect=slow_blocking
    ):
        prepare = asyncio.create_task(processor._prepare_git_workspace(state))
        # Let prepare start on the thread pool
        await asyncio.sleep(0.05)
        await probe_meta()
        await prepare

    # Each meta probe should finish well under a second (not blocked for full clone)
    assert latencies, "no probes ran"
    assert max(latencies) < 0.75, (
        f"dashboard meta probes were slow during clone: {latencies}"
    )
    assert all(t < 0.75 for t in latencies)


@pytest.mark.asyncio
async def test_blocking_path_still_used_by_to_thread(processor, state_manager):
    """Async wrapper must invoke the blocking prepare (not skip clone)."""
    state = state_manager.create_state("NB-2", "s", "d")
    called = {"n": 0}

    def fake_blocking(st):
        called["n"] += 1
        return MagicMock(name="git")

    with patch.object(
        processor, "_prepare_git_workspace_blocking", side_effect=fake_blocking
    ) as m:
        out = await processor._prepare_git_workspace(state)
    assert called["n"] == 1
    m.assert_called_once()
    assert out is not None
