"""Daily OpenCode serve behavior against a real process and a real database.

These tests do not replace OpenCode, the supervisor, or SQLite with fakes.
A stand-in that raises on demand hides the failure the real store returns.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from src.config import settings
from src.opencode_serve import OpenCodeServeClient
from src.opencode_serve_supervisor import (
    OpenCodeServeSupervisor,
    blocking_issue_keys,
    default_listener_pids,
    probe_healthy,
    resolve_opencode_binary,
    serve_auth_headers,
    serve_command,
    serve_env,
    serve_target,
)
from src.process_kill import kill_pid
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Jobs:
    """The three attributes ``blocking_issue_keys`` reads. Stores are real."""

    def __init__(self, state: JiraStateManager, queue: WorkQueueStore) -> None:
        self.state_manager = state
        self.queue_store = queue
        self._contexts: dict[str, object] = {}

    def list_live_processing_keys(self) -> list[str]:
        return list(self._contexts)


def _planning_issue(state_dir: Path) -> JiraStateManager:
    state = JiraStateManager(state_dir)
    created = state.create_state("KAN-910", "clone while serve reloads")
    assert created is not None
    updated = state.update_state("KAN-910", status=TaskStatus.PLANNING)
    assert updated is not None
    assert updated.status == TaskStatus.PLANNING
    return state


def _listen_pids(port: int) -> list[int]:
    deadline = time.time() + 20
    while time.time() < deadline:
        found = default_listener_pids(port)
        if found:
            return found
        time.sleep(0.1)
    return []


@pytest.fixture
def serve_port(monkeypatch: pytest.MonkeyPatch) -> int:
    port = _free_port()
    monkeypatch.setattr(settings, "opencode_serve_url", f"http://127.0.0.1:{port}")
    return port


def test_serve_auth_defaults_the_user_to_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenCode's basic-auth user is ``opencode`` unless the env overrides it."""
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "review-secret")
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    headers = serve_auth_headers()
    encoded = headers["Authorization"].split(" ", 1)[1]
    assert base64.b64decode(encoded) == b"opencode:review-secret"
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "")
    assert serve_auth_headers() == {}


def test_closed_issue_state_stays_empty_for_a_normal_list(tmp_path: Path) -> None:
    """Dashboard and poller reads still swallow a closed database."""
    state = _planning_issue(tmp_path / "issue" / "state")
    state._conn.close()
    assert state.get_active_issues() == []
    with pytest.raises(Exception):
        state.get_active_issues(raise_on_error=True)


def test_password_protected_serve_stays_up_and_answers(
    tmp_path: Path, serve_port: int
) -> None:
    """OPENCODE_SERVER_PASSWORD makes /global/health return 401 with an empty body.

    That response is the server answering, not a crashed process. The health
    check and the job client must send the same basic auth OpenCode expects,
    including a non-default OPENCODE_SERVER_USERNAME. Otherwise every job
    stops at the opening gate and a quiet listener is replaced.
    """
    binary = resolve_opencode_binary()
    if not binary:
        pytest.skip("opencode is not installed")
    previous = {
        "OPENCODE_SERVER_PASSWORD": os.environ.get("OPENCODE_SERVER_PASSWORD"),
        "OPENCODE_SERVER_USERNAME": os.environ.get("OPENCODE_SERVER_USERNAME"),
    }
    os.environ["OPENCODE_SERVER_PASSWORD"] = "review-secret"
    os.environ["OPENCODE_SERVER_USERNAME"] = "yaver-bot"
    _health, host, port = serve_target()
    proc = subprocess.Popen(
        serve_command(binary, host, port),
        cwd=str(tmp_path),
        env=serve_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=0x08000000 | 0x00000200 if os.name == "nt" else 0,
    )
    sup = OpenCodeServeSupervisor(reload_marker=tmp_path / "reload.pending")
    try:
        pids = _listen_pids(port)
        assert pids, "opencode serve did not listen"
        os.environ["OPENCODE_SERVER_PASSWORD"] = ""
        assert probe_healthy() is False
        os.environ["OPENCODE_SERVER_PASSWORD"] = "review-secret"
        assert probe_healthy() is True

        async def _job_call() -> dict:
            client = OpenCodeServeClient(
                f"http://127.0.0.1:{port}",
                timeout_seconds=10,
                directory=str(tmp_path),
            )
            try:
                health = await client.health(timeout=5)
                session = await client.create_session("yaver daily auth check")
                return {"health": health, "session": session}
            finally:
                await client.aclose()

        body = asyncio.run(_job_call())
        assert body["health"].get("healthy") is True
        assert str(body["session"].get("id") or "").startswith("ses_")

        for _ in range(4):
            result = sup.ensure_started()
            assert result["status"] == "ready", result
        assert _listen_pids(port) == pids
        assert proc.poll() is None
    finally:
        kill_pid(int(proc.pid))
        for pid in default_listener_pids(port):
            kill_pid(pid)
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_unreadable_issue_state_does_not_reload_a_live_serve(
    tmp_path: Path, serve_port: int
) -> None:
    """A planning job is only on disk during clone, before a session exists.

    Closing the state connection makes the real manager return an empty list.
    That must not look like "no job", or an agent save reloads serve under it.
    """
    if not resolve_opencode_binary():
        pytest.skip("opencode is not installed")
    state = _planning_issue(tmp_path / "issue" / "state")
    queue = WorkQueueStore(tmp_path / "work" / "queue")
    jobs = _Jobs(state, queue)
    state._conn.close()
    fresh = JiraStateManager(state.state_dir)
    try:
        still = [
            row
            for row in fresh.get_active_issues()
            if row.issue_key == "KAN-910" and row.status == TaskStatus.PLANNING
        ]
        assert still, "the planning row must still be in the database"
    finally:
        fresh._conn.close()

    sup = OpenCodeServeSupervisor(
        live_jobs=lambda: blocking_issue_keys(jobs),
        reload_marker=tmp_path / "reload.pending",
    )
    try:
        started = sup.ensure_started()
        assert started["status"] in {"ready", "started"}, started
        pid = _listen_pids(serve_port)
        assert pid
        reloaded = sup.request_reload()
        assert reloaded["status"] == "deferred", reloaded
        assert _listen_pids(serve_port) == pid
    finally:
        sup.stop_owned()
        for pid in default_listener_pids(serve_port):
            kill_pid(pid)


def test_unopened_queue_database_does_not_reload_a_live_serve(
    tmp_path: Path, serve_port: int
) -> None:
    """A queue database that never opened is not an empty queue.

    ``list_items`` returns [] in that case and does not raise. A reload must
    still leave the listening serve alone.
    """
    if not resolve_opencode_binary():
        pytest.skip("opencode is not installed")
    root = tmp_path / "broken"
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True)
    (root / "yaver.sqlite").mkdir()
    queue = WorkQueueStore(queue_dir)
    assert queue._conn is None
    assert queue.list_items(status="running", limit=1) == []
    state = JiraStateManager(tmp_path / "issue" / "state")
    jobs = _Jobs(state, queue)
    sup = OpenCodeServeSupervisor(
        live_jobs=lambda: blocking_issue_keys(jobs),
        reload_marker=tmp_path / "reload.pending",
    )
    try:
        started = sup.ensure_started()
        assert started["status"] in {"ready", "started"}, started
        pid = _listen_pids(serve_port)
        assert pid
        reloaded = sup.request_reload()
        assert reloaded["status"] == "deferred", reloaded
        assert _listen_pids(serve_port) == pid
    finally:
        sup.stop_owned()
        state._conn.close()
        for pid in default_listener_pids(serve_port):
            kill_pid(pid)
