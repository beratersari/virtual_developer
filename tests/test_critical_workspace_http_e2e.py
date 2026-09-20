"""Prove Sessions workspace detail does not os.walk the clone on GET.

The SPA aborts GETs at 15s; a real clone on Windows/WSL can take longer.
Real TCP: uvicorn dashboard, httpx, git origin, and (when present) a live
``opencode serve`` session.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import pytest
import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import reset_delete_jobs, reset_size_cache
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.session_bind_store import workspace_id_for


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=0.5, verify=False)
            if resp.status_code < 500:
                return
        except Exception as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"{url} not ready: {last}")


def _start_uvicorn(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_http(f"http://127.0.0.1:{port}/api/health")
    return server


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_origin(root: Path) -> Path:
    seed = root / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init")
    _git(seed, "config", "user.email", "e2e@example.com")
    _git(seed, "config", "user.name", "E2E")
    _git(seed, "checkout", "-B", "develop")
    (seed / "README.md").write_text("# workspace e2e\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: seed")
    origin = root / "origin.git"
    _git(root, "clone", "--bare", str(seed), str(origin))
    return origin


def test_workspace_detail_http_does_not_wait_on_clone_walk(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """GET /api/opencode-workspaces/{id} must return before a 15s SPA abort.

    Storage already sizes clones off the request path. Workspace detail used
    to call os.walk inline; operators opening Sessions after a real job saw
    Request timed out and an empty page.
    """
    clones = tmp_path / "t"
    folder = clones / "app_deadbeef"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text("clone\n", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    reset_size_cache()
    reset_delete_jobs()

    def _slow_size(path):  # noqa: ARG001
        time.sleep(16)
        return 99

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    rec = binds.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-44",
        target_branch="develop",
        session_id="ses_live_workspace",
        issue_key="KAN-44",
        working_directory=str(folder),
        kind="build",
        job_id="job_kan44",
    )
    assert rec is not None
    wid = workspace_id_for(
        "https://gitlab.example.com/acme/app.git",
        "feature/KAN-44",
        "develop",
    )
    assert wid

    jobs = isolate_jira_agent_artifacts["job_store"]
    job = jobs.create_job(
        issue_key="KAN-44",
        summary="feat(KAN-44): workspace",
        status="completed",
    )
    jobs.update_job(
        job["job_id"],
        working_directory=str(folder.resolve()),
        opencode_session_id="ses_live_workspace",
        merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/7",
    )

    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = jobs
    app = create_dashboard_app(processor=proc, state_manager=sm)
    app.state.job_store = jobs
    port = _free_port()
    server = _start_uvicorn(app, port)
    try:
        monkeypatch.setattr(
            "src.dashboard.temp_storage._dir_size_bytes", _slow_size
        )
        started = time.monotonic()
        resp = httpx.get(
            f"http://127.0.0.1:{port}/api/opencode-workspaces/{wid}",
            timeout=5.0,
            verify=False,
        )
        elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        assert elapsed < 2.0, (
            f"Workspace detail took {elapsed:.1f}s (SPA aborts at 15s). "
            "describe_clone_folder still walks the clone on the GET path."
        )
        body = resp.json()
        clone = body.get("clone") or {}
        assert clone.get("exists") is True
        sessions = body.get("sessions") or []
        assert any(
            str(s.get("session_id") or "") == "ses_live_workspace" for s in sessions
        ), sessions
        mrs = body.get("merge_requests") or []
        assert any("merge_requests/7" in str(u) for u in mrs), mrs
    finally:
        server.should_exit = True


@pytest.mark.asyncio
async def test_live_opencode_session_workspace_detail_and_mr_http(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Live opencode serve session + real MR URL on workspace detail over TCP."""
    bin_path = shutil.which("opencode")
    if not bin_path:
        pytest.skip("opencode binary not found on PATH")
    if shutil.which("git") is None:
        pytest.skip("git not installed")

    from src.opencode_serve import OpenCodeServeClient

    clones = tmp_path / "t"
    clones.mkdir()
    origin = _make_origin(tmp_path / "git")
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    reset_size_cache()

    serve_port = _free_port()
    dash_port = _free_port()
    serve_base = f"http://127.0.0.1:{serve_port}"
    log_path = tmp_path / "opencode-serve.log"
    log_f = open(log_path, "w", encoding="utf-8")
    serve_proc = subprocess.Popen(
        [
            bin_path,
            "serve",
            "--port",
            str(serve_port),
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
    oc: Optional[OpenCodeServeClient] = None
    server: Optional[uvicorn.Server] = None
    try:
        deadline = time.time() + 90
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                r = httpx.get(f"{serve_base}/global/health", timeout=1.0, verify=False)
                if r.status_code == 200:
                    break
            except Exception as exc:
                last = exc
            time.sleep(0.3)
        else:
            pytest.skip(f"opencode serve did not start: {last}")

        folder = clones / "app_live"
        folder.mkdir()
        (folder / "README.md").write_text("live\n", encoding="utf-8")
        oc = OpenCodeServeClient(serve_base, timeout_seconds=30.0, directory=str(folder))
        created = await oc.create_session("workspace critical e2e")
        sid = str(created.get("id") or "")
        assert sid.startswith("ses_"), created

        repo = origin.resolve().as_uri()
        binds = isolate_jira_agent_artifacts["session_bind_store"]
        rec = binds.upsert(
            repository_url=repo,
            branch="feature/KAN-90",
            target_branch="develop",
            session_id=sid,
            issue_key="KAN-90",
            working_directory=str(folder),
            kind="build",
        )
        assert rec is not None
        wid = workspace_id_for(repo, "feature/KAN-90", "develop")

        jobs = isolate_jira_agent_artifacts["job_store"]
        job = jobs.create_job(issue_key="KAN-90", summary="feat(KAN-90): live", status="completed")
        jobs.update_job(
            job["job_id"],
            working_directory=str(folder.resolve()),
            opencode_session_id=sid,
            merge_request_url="https://gitlab.example.com/acme/app/-/merge_requests/90",
        )

        sm = JiraStateManager(state_dir=tmp_path / "state")
        proc = JobProcessor()
        proc.state_manager = sm
        proc.job_store = jobs
        app = create_dashboard_app(processor=proc, state_manager=sm)
        app.state.job_store = jobs
        server = _start_uvicorn(app, dash_port)

        started = time.monotonic()
        resp = httpx.get(
            f"http://127.0.0.1:{dash_port}/api/opencode-workspaces/{wid}",
            timeout=5.0,
            verify=False,
        )
        elapsed = time.monotonic() - started
        assert resp.status_code == 200, resp.text
        assert elapsed < 2.0, elapsed
        body = resp.json()
        sessions = body.get("sessions") or []
        assert any(str(s.get("session_id") or "") == sid for s in sessions), sessions
        mrs = body.get("merge_requests") or []
        assert any(str(u).endswith("/merge_requests/90") for u in mrs), mrs
        clone = body.get("clone") or {}
        assert clone.get("exists") is True
    finally:
        if oc is not None:
            try:
                await oc.aclose()
            except Exception:
                pass
        if server is not None:
            server.should_exit = True
        serve_proc.terminate()
        try:
            serve_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            serve_proc.kill()
            serve_proc.wait(timeout=5)
        try:
            log_f.close()
        except Exception:
            pass
