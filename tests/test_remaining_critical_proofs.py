"""Prove remaining *non-intentional* critical operator bugs.

Product rules that are NOT bugs (excluded here):
- To Do + bot assignee rework
- Jira plan_ready waits for plan_execute (the Jira ticket itself)
- Settings Test must not send leftover GITLAB_PAT to a newly typed host
- GitLab REST on a real host stays HTTPS even if the clone URL is HTTP
- On-prem Settings save clears JIRA_EMAIL (Bearer PAT)
- Storage Delete refused only while a live job owns the clone

Real HTTP / real disk / real stores. No unittest.mock / MagicMock.

Each test asserts the correct operator-visible behaviour. A failure means
the production bug is still present.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.service import build_jobs
from src.gitlab.webhook import GitlabMrNoteEvent
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{host}:{port} did not accept connections")


def _serve(handler_cls) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    return httpd


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# C1 — Issue document must be exact-key, not Jobs-page search
# ---------------------------------------------------------------------------


def test_c1_issue_document_does_not_include_kan10_under_kan1(
    tmp_path, isolate_jira_agent_artifacts
):
    """GET /api/tasks/KAN-1 is the issue page, not the Jobs search box.

    Jobs-page filter uses the same ``issue_key`` query as a substring needle
    (intentional). Task detail must not reuse that for the issue document.
    """
    jobs = isolate_jira_agent_artifacts["job_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "first")
    sm.create_state("KAN-10", "tenth")
    jobs.create_job(issue_key="KAN-1", summary="KAN-1 work", status="completed")
    jobs.create_job(issue_key="KAN-10", summary="KAN-10 work", status="completed")

    listed = build_jobs(
        issue_key="KAN-1",
        store=jobs,
        state_manager=sm,
        limit=100,
        exact_issue_key=True,
    )
    keys = [j.issue_key for j in listed.jobs]
    assert keys == ["KAN-1"], (
        f"Issue document for KAN-1 also listed {keys}. "
        "task_detail / build_jobs(exact_issue_key=True) must be exact."
    )

    app = create_dashboard_app(state_manager=sm)
    # Bind the isolated store the same way the API module does at import time
    # by going through build_jobs above. HTTP path uses module default store
    # (also isolated by the fixture).
    client = TestClient(app)
    # Jobs search may stay fuzzy — that is the Jobs page. The issue route
    # must not leak KAN-10.
    detail = client.get("/api/tasks/KAN-1")
    if detail.status_code == 200:
        http_keys = [j.get("issue_key") for j in detail.json().get("jobs") or []]
        assert "KAN-10" not in http_keys, (
            f"GET /api/tasks/KAN-1 returned {http_keys}"
        )


# ---------------------------------------------------------------------------
# C2 — /yaver on a plan_ready MR must leave a visible reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c2_gitlab_yaver_on_plan_ready_posts_wait_note(
    tmp_path, monkeypatch
):
    """Jira waiting at plan_ready is intentional. A silent MR thread is not.

    Mention without /yaver already posts a usage note. /yaver that cannot
    start implement must say so on the MR.
    """
    notes: list[str] = []

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            notes.append(str(body.get("body") or ""))
            _json_response(self, 201, {"id": 1, "body": body.get("body")})

        def do_GET(self) -> None:
            self.send_error(404)

    httpd = _serve(_Gitlab)
    proc = None
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "jira_host", "")
        monkeypatch.setattr(settings, "jira_api_token", "")
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-test")
        monkeypatch.setattr(settings, "max_concurrent_jobs", 1)

        proc = JobProcessor()
        proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
        proc.state_manager.create_state("HOLD-12", "plan login")
        proc.state_manager.update_state("HOLD-12", status=TaskStatus.PLAN_READY)

        event = GitlabMrNoteEvent(
            issue_key="HOLD-12",
            note_id="4401",
            note_body="@berat_ai /yaver implement the plan",
            prompt="implement the plan",
            author_username="alice",
            author_name="Alice",
            project_id=3,
            project_path="acme/app",
            repository_url=f"http://{host}:{port}/acme/app.git",
            host=f"{host}:{port}",
            mr_iid=4,
            mr_title="feat(HOLD-12): plan login",
            mr_description="",
            source_branch="feature/HOLD-12",
            target_branch="develop",
            mr_url=f"http://{host}:{port}/acme/app/-/merge_requests/4",
            discussion_id="disc-hold",
        )
        result = await proc.enqueue_gitlab_note(event)
        deadline = time.time() + 3.0
        rec = proc.queue_store.get(result.get("queue_id") or "") or {}
        while time.time() < deadline:
            rec = proc.queue_store.get(result.get("queue_id") or "") or rec
            if notes or rec.get("status") in {
                "skipped",
                "error",
                "cancelled",
                "done",
            }:
                break
            await asyncio.sleep(0.05)

        assert notes, (
            f"GitLab /yaver on plan_ready posted nothing "
            f"(queue={rec.get('status')!r} {rec.get('error_message')!r}). "
            "The Jira ticket correctly waits; the MR thread must not stay silent."
        )
        st = proc.state_manager.get_state("HOLD-12")
        assert st is not None
        assert st.status == TaskStatus.PLAN_READY
    finally:
        httpd.shutdown()
        if proc is not None:
            try:
                proc.shutdown_processing(reason="test teardown")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# C3 — Pending schedule must not vanish behind 500 newer rows
# ---------------------------------------------------------------------------


def test_c3_older_scheduled_ticket_still_suppresses_poller_intake(
    tmp_path, monkeypatch
):
    """A future schedule must still hide the ticket from To Do intake.

    list_schedules(limit=500) walks newest mtime first. After 500 newer
    rows, WAIT-9 is invisible and the poller starts it now.
    """
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    waiting = store.create(
        title="wait",
        description="",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="WAIT-9",
        issue_description="",
    )
    wait_path = store.schedules_dir / f"{waiting['schedule_id']}.json"
    old = time.time() - 86_400
    os.utime(wait_path, (old, old))
    for i in range(500):
        store.create(
            title=f"flood {i}",
            description="",
            repository_url="https://gitlab.example.com/acme/app.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
            scheduled_at=future,
            issue_key=f"FLOOD-{i}",
            issue_description="",
        )

    class _Jira(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.endswith("/sprint"):
                self.send_response(400)
                body = b'{"errorMessages":["Board does not support sprints"]}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "/board/" in parsed.path and parsed.path.endswith("/issue"):
                _json_response(
                    self,
                    200,
                    {
                        "total": 1,
                        "issues": [
                            {
                                "key": "WAIT-9",
                                "fields": {
                                    "summary": "scheduled later",
                                    "labels": [],
                                    "assignee": {
                                        "displayName": "DevBot",
                                        "name": "devbot",
                                    },
                                    "status": {
                                        "name": "To Do",
                                        "statusCategory": {"key": "new"},
                                    },
                                },
                            }
                        ],
                    },
                )
                return
            self.send_error(404)

    httpd = _serve(_Jira)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
        monkeypatch.setattr(settings, "jira_trigger_label", "")
        client = JiraClient(host=f"http://{host}:{port}", api_token="tok")
        poller = JiraPoller(
            client=client,
            interval_seconds=30,
            board_id="1",
            state_manager=JiraStateManager(state_dir=tmp_path / "state"),
        )
        poller.schedule_store = store
        assert poller._issue_has_pending_schedule("WAIT-9") is True, (
            "WAIT-9 has a future schedule but the 500-newest scan missed it."
        )
        intake = poller.poll_board()
        keys = [i.get("key") for i in intake]
        assert "WAIT-9" not in keys, (
            f"Poller intake included scheduled WAIT-9 ({keys})."
        )
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# C4 — Late dropped-accept must not overwrite COMPLETED (CAS rule)
# ---------------------------------------------------------------------------


def test_c4_dropped_accept_does_not_overwrite_completed(tmp_path, monkeypatch):
    """AGENTS.md: fail/cancel/watchdog use CAS so late ERROR cannot clobber COMPLETED."""
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.state_manager.create_state("DONE-1", "shipped")
    proc.state_manager.update_state("DONE-1", status=TaskStatus.COMPLETED)

    proc.record_dropped_accept("DONE-1", "shipped", reason="loop closed")
    st = proc.state_manager.get_state("DONE-1")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED, (
        f"record_dropped_accept overwrote COMPLETED with {st.status.value}."
    )


# ---------------------------------------------------------------------------
# C5 — Distinct issue keys must not share one state file
# ---------------------------------------------------------------------------


def test_c5_hyphen_and_underscore_issue_keys_stay_separate(tmp_path):
    """GL-KAN-12 (GitLab fallback) and GL_KAN-12 (Jira project) are different tickets."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("GL-KAN-12", "gitlab fallback")
    sm.create_state("GL_KAN-12", "jira project GL_KAN")
    sm.update_state("GL_KAN-12", status=TaskStatus.COMPLETED)

    left = sm.get_state("GL-KAN-12")
    right = sm.get_state("GL_KAN-12")
    assert left is not None and right is not None
    assert left.issue_key == "GL-KAN-12"
    assert right.issue_key == "GL_KAN-12"
    assert left.status == TaskStatus.PENDING, (
        f"GL-KAN-12 became {left.status.value} / {left.issue_summary!r} "
        "because '-' is folded to '_' in the state filename."
    )
    assert right.status == TaskStatus.COMPLETED
    assert left.issue_summary == "gitlab fallback"
    assert right.issue_summary == "jira project GL_KAN"


# ---------------------------------------------------------------------------
# C6 — create_state must not return a phantom PENDING over COMPLETED
# ---------------------------------------------------------------------------


def test_c6_create_state_does_not_return_unsaved_pending(tmp_path):
    """If disk refuses the write, callers must not think they own PENDING."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("FOO-1", "original")
    sm.update_state("FOO-1", status=TaskStatus.COMPLETED)

    returned = sm.create_state("FOO-1", "second create")
    disk = sm.get_state("FOO-1")
    assert disk is not None
    assert disk.status == TaskStatus.COMPLETED
    assert returned.status == TaskStatus.COMPLETED, (
        f"create_state returned {returned.status.value} while disk is "
        f"{disk.status.value}."
    )
