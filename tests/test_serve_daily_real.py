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
    default_listener_pids,
    probe_healthy,
    resolve_opencode_binary,
    serve_auth_headers,
    serve_command,
    serve_env,
    serve_target,
)
from src.process_kill import kill_pid


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
