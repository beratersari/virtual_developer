"""LIVE e2e: real HTTP webhooks + real opencode serve + Dashboard Stop.

Proves the GitLab queue path (``POST /yaver/webhook/gitlab`` →
``_run_queue_item``) holds the per-issue lock so Stop + a leftover
``/yaver`` cannot start a second OpenCode session beside the cancelled
worker.

Real things (no MagicMock of serve / webhook / cancel):

- ``opencode serve`` subprocess
- TCP dashboard (uvicorn on 127.0.0.1)
- ``POST /yaver/webhook/gitlab`` twice (Note Hook)
- ``POST /api/tasks/{key}/cancel``
- local git origin + real clone
- live model turn (busy long enough to cancel)

Skipped only when the ``opencode`` binary is missing or serve will not start.

Run::

    .venv312\\Scripts\\python.exe -m pytest tests/test_gitlab_queue_cancel_live_opencode_e2e.py -v -s --tb=short
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.opencode_serve import OpenCodeServeClient
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from tests.conftest import FakeJiraClient


SECRET = "e2e-gitlab-lock-secret"
BOT = "yaver"
ISSUE_TITLE = "feat(KAN-8801): live lock e2e"
_FREE_PREFERENCE = (
    "opencode/north-mini-code-free",
    "opencode/ling-3.0-flash-free",
    "opencode/laguna-s-2.1-free",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/deepseek-v4-flash-free",
)


def _opencode_bin() -> Optional[str]:
    return shutil.which("opencode")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


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


async def _wait_http_ok(url: str, *, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return
            except Exception as exc:
                last = exc
            await asyncio.sleep(0.3)
    raise TimeoutError(f"{url} not ready: {last}")


def _pick_model(known: List[str]) -> str:
    known_set = {str(mid).strip() for mid in known if str(mid).strip()}
    for mid in _FREE_PREFERENCE:
        if mid in known_set:
            return mid
    for mid in known:
        raw = str(mid).strip()
        if raw.startswith("opencode/") and raw.endswith("-free"):
            return raw
    if known:
        return str(known[0]).strip()
    raise RuntimeError("Live OpenCode serve published no models")


def _make_origin(root: Path) -> Path:
    seed = root / "seed"
    seed.mkdir(parents=True)
    _git(seed, "init")
    _git(seed, "config", "user.email", "e2e@example.com")
    _git(seed, "config", "user.name", "E2E")
    _git(seed, "checkout", "-B", "develop")
    (seed / "README.md").write_text("# live lock e2e\n", encoding="utf-8")
    docs = seed / "docs"
    docs.mkdir()
    for i in range(1, 12):
        (docs / f"n{i:02d}.txt").write_text(f"chunk {i}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: seed")
    _git(seed, "checkout", "-B", "feature/login")
    _git(seed, "checkout", "develop")
    origin = root / "origin.git"
    _git(root, "clone", "--bare", str(seed), str(origin))
    return origin


def _note_payload(
    *,
    repo_url: str,
    note_id: int,
    note: str,
    title: str = ISSUE_TITLE,
) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "alice", "name": "Alice"},
        "project": {
            "id": 8801,
            "path_with_namespace": "acme/live-lock",
            "http_url_to_repo": repo_url,
            "web_url": "https://gitlab.example.com/acme/live-lock",
        },
        "object_attributes": {
            "id": note_id,
            "note": note,
            "noteable_type": "MergeRequest",
            "project_id": 8801,
            "discussion_id": f"disc-{note_id}",
        },
        "merge_request": {
            "iid": 4,
            "title": title,
            "description": "live lock e2e",
            "source_branch": "feature/login",
            "target_branch": "develop",
            "web_url": "https://gitlab.example.com/acme/live-lock/-/merge_requests/4",
        },
        "repository": {"url": repo_url},
    }


BUSY_NOTE = (
    f"@{BOT} /yaver Do not ask questions. In this repo, read every file under "
    "docs/ one at a time with the read tool. After the last file, run "
    '`python -c "import time; time.sleep(25)"` and wait for it. Then write '
    "e2e-done.txt containing only the word done. Do not push. Do not open an MR."
)
FOLLOW_NOTE = (
    f"@{BOT} /yaver Do not ask questions. Write followup.txt containing only "
    "the word followup. Do not push."
)


@pytest.mark.asyncio
async def test_live_http_stop_waits_for_opencode_before_queued_yaver(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")
    if shutil.which("git") is None:
        pytest.skip("git not installed")

    isolate = isolate_jira_agent_artifacts
    clones = tmp_path / "clones"
    clones.mkdir()
    origin = _make_origin(tmp_path / "git")
    repo_url = origin.resolve().as_uri()

    serve_port = _free_port()
    dash_port = _free_port()
    serve_base = f"http://127.0.0.1:{serve_port}"
    dash_base = f"http://127.0.0.1:{dash_port}"

    monkeypatch.setenv("TEMP_DIR_BASE", str(clones))
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    monkeypatch.setattr(settings, "jira_enabled", False)
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "gitlab_trigger_user", BOT)
    monkeypatch.setattr(settings, "gitlab_bot_mentions", BOT)
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "opencode_serve_url", serve_base)
    monkeypatch.setattr(settings, "agent_backend", "opencode")
    monkeypatch.setattr(settings, "max_concurrent_jobs", 2)
    monkeypatch.setattr(settings, "agent_task_timeout_seconds", 180)
    monkeypatch.setattr(settings, "agent_task_max_retries", 0)
    monkeypatch.setattr(settings, "agent_task_max_incomplete_retries", 0)
    monkeypatch.setattr(settings, "agent_task_retry_delay_seconds", 0.05)
    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")

    fake_jira = FakeJiraClient()
    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    proc.jira_client = fake_jira
    proc.reporter = JiraReporter(client=fake_jira)
    app = create_dashboard_app(processor=proc, state_manager=sm)

    serve_proc: Optional[subprocess.Popen] = None
    server: Optional[uvicorn.Server] = None
    dash_task: Optional[asyncio.Task] = None
    http: Optional[httpx.AsyncClient] = None
    oc: Optional[OpenCodeServeClient] = None
    issue_key = ""
    try:
        serve_proc = _start_serve(serve_port, tmp_path / "opencode-serve.log")
        await _wait_http_ok(f"{serve_base}/global/health", timeout=90.0)
        oc = OpenCodeServeClient(serve_base, timeout_seconds=30.0)
        known = await oc.list_known_models()
        model = _pick_model(known)
        monkeypatch.setattr(settings, "default_model", model)

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=dash_port,
            log_level="warning",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        dash_task = asyncio.create_task(server.serve())
        await _wait_http_ok(f"{dash_base}/api/health", timeout=20.0)
        http = httpx.AsyncClient(verify=False, timeout=30.0)

        headers = {
            "X-Gitlab-Event": "Note Hook",
            "X-Gitlab-Token": SECRET,
            "Content-Type": "application/json",
        }
        first = await http.post(
            f"{dash_base}/yaver/webhook/gitlab",
            headers=headers,
            json=_note_payload(repo_url=repo_url, note_id=101, note=BUSY_NOTE),
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body.get("ok") is True, body
        issue_key = str(body.get("issue_key") or "")
        assert issue_key, body

        first_job = ""
        first_sid = ""
        deadline = time.time() + 90.0
        detail: Dict[str, Any] = {}
        while time.time() < deadline:
            resp = await http.get(f"{dash_base}/api/tasks/{issue_key}")
            if resp.status_code == 200:
                detail = resp.json()
                first_job = str(detail.get("current_job_id") or "").strip()
                first_sid = str(
                    detail.get("current_opencode_session_id") or ""
                ).strip()
                if (
                    detail.get("status") == "executing"
                    and detail.get("live")
                    and first_job
                    and first_sid.startswith("ses_")
                ):
                    break
                if detail.get("status") == "error":
                    pytest.fail(
                        "first /yaver ERRORed before a live OpenCode session "
                        f"existed: {detail.get('error_message')!r}"
                    )
            await asyncio.sleep(0.2)
        else:
            pytest.fail(
                "first /yaver never reached live EXECUTING with a ses_* "
                f"(last={detail})"
            )

        second = await http.post(
            f"{dash_base}/yaver/webhook/gitlab",
            headers=headers,
            json=_note_payload(repo_url=repo_url, note_id=102, note=FOLLOW_NOTE),
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        leftover_qid = str(second_body.get("queue_id") or "")
        assert leftover_qid, second_body

        # Overlap window: first OpenCode job is still live. The leftover
        # /yaver must not replace it (that was the missing-lock bug).
        await asyncio.sleep(1.0)
        overlap = (await http.get(f"{dash_base}/api/tasks/{issue_key}")).json()
        assert overlap.get("status") == "executing", overlap
        assert overlap.get("live") is True, overlap
        assert str(overlap.get("current_job_id") or "") == first_job, (
            f"Second /yaver stole the live job while the first OpenCode "
            f"run was still in _run_queue_item "
            f"(first={first_job} now={overlap.get('current_job_id')!r})."
        )

        cancel = await http.post(f"{dash_base}/api/tasks/{issue_key}/cancel")
        assert cancel.status_code == 200, cancel.text
        assert cancel.json().get("ok") is True

        follow_job = ""
        follow_detail: Dict[str, Any] = {}
        deadline = time.time() + 90.0
        while time.time() < deadline:
            resp = await http.get(f"{dash_base}/api/tasks/{issue_key}")
            if resp.status_code == 200:
                follow_detail = resp.json()
                follow_job = str(follow_detail.get("current_job_id") or "").strip()
                if (
                    follow_detail.get("status") == "executing"
                    and follow_detail.get("live")
                    and follow_job
                    and follow_job != first_job
                ):
                    break
            await asyncio.sleep(0.3)
        else:
            pytest.fail(
                "Queued /yaver never started a new job after Stop "
                f"(first_job={first_job} first_ses={first_sid} "
                f"last={follow_detail})"
            )
        assert follow_job != first_job
        # Same ses_* is allowed: plan/build binds resume kind=build. A new
        # job row is the generation the lock is protecting.
    finally:
        if http is not None and issue_key:
            try:
                await http.post(f"{dash_base}/api/tasks/{issue_key}/cancel")
            except Exception:
                pass
        if http is not None:
            await http.aclose()
        if oc is not None:
            try:
                await oc.aclose()
            except Exception:
                pass
        if server is not None:
            server.should_exit = True
        if dash_task is not None:
            try:
                await asyncio.wait_for(dash_task, timeout=8.0)
            except Exception:
                dash_task.cancel()
        _stop_serve(serve_proc)
