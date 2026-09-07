"""Full HTTP budget timeout must not start another hours-long compact wait."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from src.opencode_serve import OpenCodeServeClient, ServeOrchestrator


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


class _HungBackend:
    def __init__(self) -> None:
        self.aborted = False
        self.prompts: list[str] = []
        self.session_id = "ses_hung"

    async def health(self):
        return {"healthy": True, "version": "fake"}

    async def create_session(self, title, **kwargs):
        return {"id": self.session_id, "title": title}

    async def send_message(self, session_id, text, **kwargs):
        self.prompts.append(text)
        await asyncio.sleep(0.25)
        raise httpx.ReadTimeout("")

    async def list_messages(self, session_id, *, limit=500):
        return []

    async def list_all_messages(self, session_id, **kwargs):
        return []

    async def session_status(self):
        if self.aborted:
            return {self.session_id: {"type": "idle"}}
        return {self.session_id: {"type": "busy"}}

    async def list_todos(self, session_id):
        return []

    async def abort(self, session_id):
        self.aborted = True
        return True

    async def summarize(self, session_id, **kwargs):
        return True

    async def aclose(self):
        return None


class _HungClient(OpenCodeServeClient):
    def __init__(self, backend: _HungBackend):
        self.base_url = "http://fake/"
        self.timeout_seconds = 0.2
        self.directory = None
        self._owned_client = False
        self._client = None  # type: ignore
        self._backend = backend

    async def health(self):
        return await self._backend.health()

    async def create_session(self, title, **kw):
        return await self._backend.create_session(title, **kw)

    async def send_message(self, session_id, text, **kw):
        return await self._backend.send_message(session_id, text, **kw)

    async def list_messages(self, session_id, *, limit=500):
        return await self._backend.list_messages(session_id, limit=limit)

    async def list_all_messages(self, session_id, **kw):
        return await self._backend.list_all_messages(session_id)

    async def list_todos(self, session_id):
        return await self._backend.list_todos(session_id)

    async def session_status(self):
        return await self._backend.session_status()

    async def abort(self, session_id):
        return await self._backend.abort(session_id)

    async def summarize(self, session_id, **kw):
        return await self._backend.summarize(session_id, **kw)

    async def list_agents(self):
        return []

    def ensure_directory_ready(self):
        return None

    async def aclose(self):
        return await self._backend.aclose()


@pytest.mark.asyncio
async def test_full_http_budget_timeout_aborts_hung_busy_session(monkeypatch):
    # Live Settings of 7200 must not stretch compact wait in this path.
    monkeypatch.setattr(
        "src.config.live_agent_timeout_seconds",
        lambda **_kw: 1800,
    )
    backend = _HungBackend()
    client = _HungClient(backend)
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=30.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    t0 = time.time()
    result = await orch.run(prompt="do work", title="KAN-HUNG")
    elapsed = time.time() - t0
    assert elapsed < 8.0, f"hung wait lasted {elapsed:.1f}s"
    assert result.timed_out is True
    assert result.returncode == -1
    assert backend.aborted is True
    err = result.stderr or ""
    assert "HTTP budget exhausted" in err or "did not finish" in err
    combined = f"{result.stdout or ''}\n{err}"
    assert "not another" in combined
    assert "waiting for auto-compact (poll 100" not in combined


class _EarlyTimeoutThenIdle:
    """Instant ReadTimeout; session is idle and already complete."""

    def __init__(self) -> None:
        self.aborted = False
        self.session_id = "ses_early"
        self._done = {
            "info": {
                "id": "msg_done",
                "role": "assistant",
                "finish": "stop",
                "summary": None,
            },
            "parts": [{"type": "text", "text": "Committed the change."}],
        }

    async def health(self):
        return {"healthy": True, "version": "fake"}

    async def create_session(self, title, **kwargs):
        return {"id": self.session_id, "title": title}

    async def send_message(self, session_id, text, **kwargs):
        raise httpx.ReadTimeout("")

    async def list_messages(self, session_id, *, limit=500):
        return [self._done]

    async def list_all_messages(self, session_id, **kwargs):
        return [self._done]

    async def session_status(self):
        return {self.session_id: {"type": "idle"}}

    async def list_todos(self, session_id):
        return [{"content": "Commit", "status": "completed"}]

    async def abort(self, session_id):
        self.aborted = True
        return True

    async def summarize(self, session_id, **kwargs):
        return True

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_early_http_timeout_still_waits_for_idle_complete(monkeypatch):
    """Instant ReadTimeout is compact/auto-resume, not a full-budget hang."""
    monkeypatch.setattr(
        "src.config.live_agent_timeout_seconds",
        lambda **_kw: 1800,
    )
    backend = _EarlyTimeoutThenIdle()
    client = _HungClient(backend)
    client.timeout_seconds = 30.0
    orch = ServeOrchestrator(
        client=client,
        compact_wait_seconds=0.6,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
    )
    result = await orch.run(prompt="do work", title="KAN-EARLY")
    assert result.returncode == 0, result.stderr
    assert result.timed_out is not True
    assert backend.aborted is False
    assert "HTTP budget exhausted" not in (result.stderr or "")
