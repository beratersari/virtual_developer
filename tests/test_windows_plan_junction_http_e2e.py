"""Clone delete must not wipe ``{YAVER_DATA_DIR}/plans`` via ``.yaver-plans``.

Plan jobs expose the durable plans dir inside the clone as ``.yaver-plans``
(Windows: ``mklink /J`` junction). Storage Delete and GitLab/Azure merge
cleanup must remove the link only.

Real TCP: uvicorn dashboard + httpx. No unittest.mock of the delete path.
"""

from __future__ import annotations

import os
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


def _wait_gone(path: Path, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not path.exists(), f"clone still exists after {timeout}s: {path}"


def _link_plans_like_production(clone: Path, plans: Path) -> None:
    """Same link JobProcessor._link_plans_into_workspace creates.

    Production tries ``os.symlink`` first, then ``mklink /J`` on Windows.
    Developer Mode makes symlink succeed here; typical operator boxes
    have no symlink privilege, so the daily path is the junction. Force
    that fallback on Windows so this test matches real installs.
    """
    link = clone / ".yaver-plans"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(plans)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            pytest.fail(
                "could not create .yaver-plans junction: "
                f"{(completed.stderr or completed.stdout or '').strip()}"
            )
        assert link.is_dir()
        assert not link.is_symlink(), (
            "expected a directory junction (is_symlink is false), not a symlink"
        )
        return
    os.symlink(str(plans), str(link), target_is_directory=True)


def _setup_clone_with_plans(tmp_path: Path, monkeypatch):
    data = tmp_path / "yaver-data"
    plans = data / "plans"
    plans.mkdir(parents=True)
    (plans / "KAN-1.md").write_text("# plan for KAN-1\n", encoding="utf-8")
    (plans / "KAN-2.md").write_text("# plan for KAN-2\n", encoding="utf-8")
    clones = tmp_path / "t"
    clone = clones / "app_planclone"
    clone.mkdir(parents=True)
    (clone / "README.md").write_text("worktree\n", encoding="utf-8")
    _link_plans_like_production(clone, plans)
    assert (clone / ".yaver-plans" / "KAN-1.md").is_file()

    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    reset_delete_jobs()
    reset_size_cache()
    return clone, plans


def test_storage_delete_http_must_not_wipe_durable_plans(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """POST /api/storage/delete is Storage → Delete on a finished plan clone.

    Operators do this after a Mode: plan job. The clone goes away; every
    ``{YAVER_DATA_DIR}/plans/*.md`` must stay so later plan_execute / Mode:
    build can still read the plan. Windows junctions currently follow into
    the plans dir and empty it.
    """
    clone, plans = _setup_clone_with_plans(tmp_path, monkeypatch)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(state_manager=sm)
    port = _free_port()
    server = _start_uvicorn(app, port)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/api/storage/delete",
            json={"name": clone.name, "area": "temp"},
            timeout=10.0,
            verify=False,
        )
        assert resp.status_code == 202, resp.text
        _wait_gone(clone)
        assert (plans / "KAN-1.md").is_file(), (
            "Storage Delete followed .yaver-plans and removed KAN-1.md. "
            "On Windows that junction points at the durable plans directory."
        )
        assert (plans / "KAN-2.md").is_file(), (
            "Storage Delete wiped KAN-2.md (a different ticket's plan) "
            "because .yaver-plans is a junction to the whole plans dir."
        )
    finally:
        server.should_exit = True


def test_gitlab_merge_webhook_http_must_not_wipe_other_issue_plans(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """POST /yaver/webhook/gitlab Merge Request Hook (merged).

    Merge cleanup queues the same force-delete as Storage. 0.9.46 may
    drop this ticket's own plan file on purpose; it must not walk the
    junction and delete every other issue's plan (KAN-2 here).
    """
    clone, plans = _setup_clone_with_plans(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "secret")
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    jobs = isolate_jira_agent_artifacts["job_store"]
    rec = jobs.create_job(
        issue_key="KAN-1",
        summary="feat(KAN-1): login",
        status="completed",
        workflow_type="gitlab_mr",
    )
    mr_url = "https://gitlab.example.com/acme/app/-/merge_requests/4"
    jobs.update_job(
        rec["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url=mr_url,
        gitlab_project="acme/app",
        gitlab_mr_iid=4,
        repository_url="https://gitlab.example.com/acme/app.git",
    )

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "feat(KAN-1): login")
    sm.create_state("KAN-2", "feat(KAN-2): other")
    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = jobs
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    app = create_dashboard_app(processor=proc, state_manager=sm)
    port = _free_port()
    server = _start_uvicorn(app, port)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/yaver/webhook/gitlab",
            headers={
                "X-Gitlab-Event": "Merge Request Hook",
                "X-Gitlab-Token": "secret",
            },
            json={
                "object_kind": "merge_request",
                "object_attributes": {
                    "iid": 4,
                    "action": "merge",
                    "state": "merged",
                    "title": "feat(KAN-1): login",
                    "description": "",
                    "source_branch": "feature/KAN-1",
                    "target_branch": "develop",
                    "url": mr_url,
                },
                "project": {
                    "id": 3,
                    "path_with_namespace": "acme/app",
                    "http_url_to_repo": "https://gitlab.example.com/acme/app.git",
                    "web_url": "https://gitlab.example.com/acme/app",
                },
                "repository": {},
            },
            timeout=15.0,
            verify=False,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("ok") is True, body
        _wait_gone(clone)
        assert (plans / "KAN-2.md").is_file(), (
            "GitLab merge clone delete followed .yaver-plans and removed "
            "KAN-2.md, a different ticket's durable plan."
        )
    finally:
        server.should_exit = True
