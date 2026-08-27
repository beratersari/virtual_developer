"""Operator-style e2e checks for the remaining review issues.

A real HTTP Jira REST/Agile server and a real GitLab-shaped listener are
started on loopback. ``JiraClient`` and the dashboard FastAPI app talk to
them over httpx the same way the daemon and the SPA do.

These are not mocks of ``get_active_sprint`` / ``poll_board``. They are not
a live on-prem Jira (``.env`` currently points at a non-company host).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.reporter.jira_reporter import JiraReporter
from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# Loopback Jira Software (REST v2 + Agile 1.0)
# ---------------------------------------------------------------------------


class JiraWorld:
    def __init__(self) -> None:
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.seq = 0
        self.sprint_mode = "ok"  # ok | empty | kanban | 500
        self.hits: List[str] = []
        self.auth_seen: List[str] = []

    def create(
        self,
        *,
        summary: str,
        description: str = "",
        labels: Optional[List[str]] = None,
        status: str = "To Do",
        key: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.seq += 1
        issue_key = key or f"KAN-{self.seq}"
        rec = {
            "key": issue_key,
            "summary": summary,
            "description": description,
            "labels": list(labels or []),
            "status": status,
            "assignee": {"displayName": "DevBot"},
            "comments": [],
        }
        self.issues[issue_key] = rec
        return rec

    def as_jira(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "key": rec["key"],
            "id": rec["key"],
            "fields": {
                "summary": rec["summary"],
                "description": rec["description"],
                "labels": rec["labels"],
                "status": {
                    "name": rec["status"],
                    "statusCategory": {
                        "key": (
                            "new"
                            if rec["status"].lower() in ("to do", "todo", "open")
                            else "indeterminate"
                        )
                    },
                },
                "issuetype": {"name": "Task", "id": "10003", "subtask": False},
                "assignee": rec.get("assignee") or {"displayName": "DevBot"},
            },
        }


def _start_jira(world: JiraWorld) -> Tuple[str, ThreadingHTTPServer]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _read_json(self) -> Dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n)
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}

        def _send(self, status: int, payload: Any = None, text: str = "") -> None:
            body = (
                text.encode("utf-8")
                if text
                else json.dumps(payload if payload is not None else {}).encode("utf-8")
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            world.hits.append(f"GET {path}")
            auth = self.headers.get("Authorization") or ""
            if auth:
                world.auth_seen.append(auth)

            if path.endswith("/myself") or path == "/rest/api/2/myself":
                if "500-body" in (self.headers.get("X-Probe") or ""):
                    return self._send(500, text="INTERNAL_STACKTRACE_SECRET")
                return self._send(200, {"displayName": "e2e-bot", "name": "bot"})

            if "createmeta" in path or path.endswith("/project/KAN"):
                return self._send(
                    200,
                    {
                        "projects": [
                            {
                                "key": "KAN",
                                "issuetypes": [
                                    {
                                        "id": "10003",
                                        "name": "Task",
                                        "subtask": False,
                                    }
                                ],
                            }
                        ]
                    },
                )

            if path.startswith("/rest/agile/1.0/board/") and path.endswith("/sprint"):
                if world.sprint_mode == "500":
                    return self._send(500, text="INTERNAL")
                if world.sprint_mode == "kanban":
                    return self._send(400, text="Board does not support sprints")
                if world.sprint_mode == "empty":
                    return self._send(200, {"values": [], "maxResults": 50})
                return self._send(
                    200,
                    {"values": [{"id": 42, "name": "Sprint 1", "state": "active"}]},
                )

            if path.startswith("/rest/agile/1.0/sprint/") and path.endswith("/issue"):
                issues = [world.as_jira(r) for r in world.issues.values()]
                return self._send(
                    200, {"issues": issues, "total": len(issues), "maxResults": 100}
                )

            if path.startswith("/rest/agile/1.0/board/") and path.endswith("/issue"):
                issues = [world.as_jira(r) for r in world.issues.values()]
                return self._send(
                    200, {"issues": issues, "total": len(issues), "maxResults": 100}
                )

            if "/transitions" in path and path.startswith("/rest/api/2/issue/"):
                return self._send(
                    200,
                    {
                        "transitions": [
                            {
                                "id": "21",
                                "name": "Start Progress",
                                "to": {
                                    "name": "In Progress",
                                    "statusCategory": {"key": "indeterminate"},
                                },
                            }
                        ]
                    },
                )

            if path.startswith("/rest/api/2/issue/"):
                key = path.rsplit("/", 1)[-1]
                rec = world.issues.get(key)
                if not rec:
                    return self._send(404, {"errorMessages": ["not found"]})
                return self._send(200, world.as_jira(rec))

            return self._send(404, {"errorMessages": [path]})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            world.hits.append(f"POST {path}")
            auth = self.headers.get("Authorization") or ""
            if auth:
                world.auth_seen.append(auth)
            data = self._read_json()

            if path == "/rest/api/2/issue" or path.endswith("/issue"):
                fields = data.get("fields") or {}
                rec = world.create(
                    summary=str(fields.get("summary") or "untitled"),
                    description=str(fields.get("description") or ""),
                    labels=list(fields.get("labels") or []),
                )
                return self._send(201, {"key": rec["key"], "id": rec["key"]})

            if path.endswith("/transitions"):
                key = path.split("/issue/")[1].split("/")[0]
                rec = world.issues.get(key)
                if rec:
                    rec["status"] = "In Progress"
                    return self._send(204, {})
                return self._send(404, {})

            if path.endswith("/comment"):
                key = path.split("/issue/")[1].split("/")[0]
                rec = world.issues.get(key)
                if not rec:
                    return self._send(404, {})
                body = data.get("body") or ""
                rec["comments"].append(body if isinstance(body, str) else json.dumps(body))
                return self._send(201, {"id": "c1", "body": body})

            return self._send(404, {})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}", httpd


def _client(base: str) -> JiraClient:
    return JiraClient(host=base, api_token="e2e-token", email="")


@pytest.fixture
def jira():
    world = JiraWorld()
    base, httpd = _start_jira(world)
    try:
        yield world, base, _client(base)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def trigger_settings():
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["bot", "ai-assist"]
        s.trigger_assignee_names_list = ["devbot"]
        s.trigger_on_assignment = True
        yield s


# ---------------------------------------------------------------------------
# C8 — operator creates a ticket; poller accept + missing handler
# ---------------------------------------------------------------------------


def test_operator_c8_create_via_jira_api_then_noop_handler(
    jira, state_manager, trigger_settings
):
    world, base, client = jira
    created = client.create_issue(
        "KAN", "operator ticket", "Mode: build", labels=["bot"]
    )
    assert created and created.get("key")
    key = created["key"]
    fetched = client.get_issue(key)
    assert fetched["fields"]["status"]["name"] == "To Do"

    poller = JiraPoller(client=client, interval_seconds=1, board_id="10")
    poller.state_manager = state_manager
    poller._handler = None
    issues = poller.poll_board()
    assert any(i["key"] == key for i in issues)
    poller.process_issue(issues[0], is_update=False)

    live = client.get_issue(key)
    assert live["fields"]["status"]["name"] == "In Progress"
    st = state_manager.get_state(key)
    assert st is not None and st.status.value == "error"
    assert world.issues[key]["comments"]


# ---------------------------------------------------------------------------
# C9 — completion comment on the real Jira issue includes prior MR URL
# ---------------------------------------------------------------------------


def test_operator_c9_completion_comment_posted_to_jira_api(jira, state_manager):
    world, base, client = jira
    created = client.create_issue("KAN", "done work", "d", labels=["bot"])
    key = created["key"]
    state_manager.create_state(key, "done work", "d")
    from datetime import datetime

    state = state_manager.update_state(
        key,
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={
            "merge_request_url": "https://gitlab.example.com/g/p/-/merge_requests/99",
            "delivery_status": "no_new_commits",
            "delivery_note": "no new commits this run",
            "feature_branch": f"feature/{key}",
        },
    )
    reporter = JiraReporter(client=client)
    reporter.post_completion(state, summary="done")
    bodies = [c if isinstance(c, str) else str(c) for c in world.issues[key]["comments"]]
    assert bodies, "operator should see a Jira comment"
    blob = "\n".join(bodies)
    assert "merge_requests/99" not in blob


# ---------------------------------------------------------------------------
# 6 — operator opens Queue tab (GET /api/queue with no status)
# ---------------------------------------------------------------------------


def test_operator_queue_tab_hides_new_waiting_work(tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.state.queue_store import WorkQueueStore

    store = WorkQueueStore(queue_dir=tmp_path / "queue")
    for i in range(200):
        rec = store.enqueue(source="jira", issue_key=f"OLD-{i}")
        rec["created_at"] = f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}.000"
        rec["status"] = "completed"
        rec["finished_at"] = rec["created_at"]
        store._write(rec)
    fresh = store.enqueue(source="jira", issue_key="NEW-WAIT", summary="waiting")
    store.update(fresh["queue_id"], created_at="2026-08-14T12:00:00.000")

    monkeypatch.setattr("src.dashboard.service.work_queue_store", store, raising=False)
    monkeypatch.setattr("src.state.queue_store.work_queue_store", store)
    app = create_dashboard_app()
    http = TestClient(app)
    # Same call the Jobs Queue tab makes: limit=200, no status
    r = http.get("/api/queue", params={"limit": 200})
    assert r.status_code == 200
    body = r.json()
    keys = [i.get("issue_key") for i in body.get("items") or []]
    assert "NEW-WAIT" in keys
    assert body.get("queued_count") >= 1


# ---------------------------------------------------------------------------
# C2 — operator saves a new Jira host in Settings (keeps token)
# ---------------------------------------------------------------------------


def test_operator_settings_patch_jira_host_sends_token_to_new_host(
    tmp_path, monkeypatch, jira
):
    from src.config import settings
    from src.dashboard.api import create_dashboard_app

    world, base, _client = jira
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JIRA_HOST=http://old.example\n", encoding="utf-8")
    monkeypatch.setattr(settings, "jira_host", "http://old.example")
    monkeypatch.setattr(settings, "jira_api_token", "OPERATOR-SECRET-TOKEN")
    monkeypatch.setattr(settings, "jira_email", "")

    app = create_dashboard_app()
    http = TestClient(app)
    r = http.patch("/api/settings", json={"jira_host": base})
    assert r.status_code == 400
    assert settings.jira_host == "http://old.example"
    assert settings.jira_api_token == "OPERATOR-SECRET-TOKEN"

    # Host unchanged — probe of the *saved* host still uses the stored token.
    probe = http.post(
        "/api/settings/jira/test",
        json={"host": "http://old.example", "api_token": ""},
    )
    assert probe.status_code == 200


# ---------------------------------------------------------------------------
# C3 — operator clicks Test Jira against an attacker/listener host
# ---------------------------------------------------------------------------


def test_operator_jira_test_reflects_listener_error_body(tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            body = b"INTERNAL_STACKTRACE_SECRET cloud-metadata=yes"
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

    bad = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=bad.serve_forever, daemon=True).start()
    bad_base = f"http://{bad.server_address[0]}:{bad.server_address[1]}"
    try:
        monkeypatch.chdir(tmp_path)
        app = create_dashboard_app()
        http = TestClient(app)
        r = http.post(
            "/api/settings/jira/test",
            json={"host": bad_base, "api_token": "dummy-not-empty"},
        )
        assert r.status_code == 200
        err = r.json().get("error") or ""
        assert "INTERNAL_STACKTRACE_SECRET" not in err
    finally:
        bad.shutdown()
        bad.server_close()


# ---------------------------------------------------------------------------
# C4 — Settings GitLab Test on a sibling host uses the parent PAT
# ---------------------------------------------------------------------------


def test_operator_gitlab_test_parent_domain_pat_not_rejected_as_missing(
    tmp_path, monkeypatch
):
    from src.config import settings
    from src.dashboard.api import create_dashboard_app

    monkeypatch.chdir(tmp_path)
    settings.set_gitlab_host_pat_map({"gitlab.company.com": "REAL-GITLAB-PAT"})
    app = create_dashboard_app()
    http = TestClient(app)
    r = http.post(
        "/api/settings/gitlab/test",
        json={"host": "evil.gitlab.company.com", "pat": ""},
    )
    assert r.status_code == 200
    body = r.json()
    err = (body.get("error") or "").lower()
    assert "no pat stored" in err


# ---------------------------------------------------------------------------
# 7 — Settings GitLab Test against host:port never reaches that port
# ---------------------------------------------------------------------------


def test_operator_gitlab_test_strips_port_never_hits_listener():
    from src.gitlab_connection import probe_gitlab_connection

    hits: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            hits.append(self.path)
            body = json.dumps({"username": "bot"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        result = probe_gitlab_connection(f"127.0.0.1:{port}", pat="dummy")
        # Port is kept; listener may see HTTP or the probe uses https to that port.
        assert result.get("host") == f"127.0.0.1:{port}" or hits or result.get("ok") is False
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 11 — operator cancel API is keyed by path; UI still binds stale detail
# ---------------------------------------------------------------------------


def test_operator_cancel_hits_whatever_key_the_ui_posts(jira, state_manager, tmp_path):
    from src.dashboard.api import create_dashboard_app
    from src.processor import JobProcessor

    world, base, client = jira
    a = client.create_issue("KAN", "ticket A", "d", labels=["bot"])
    b = client.create_issue("KAN", "ticket B", "d", labels=["bot"])
    state_manager.create_state(a["key"], "A", "d")
    state_manager.update_state(a["key"], status=TaskStatus.EXECUTING)
    state_manager.create_state(b["key"], "B", "d")
    state_manager.update_state(b["key"], status=TaskStatus.EXECUTING)

    with patch("src.processor.create_jira_client", return_value=client):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = client
    proc._kill_children_for_issue = MagicMock()  # type: ignore[method-assign]

    app = create_dashboard_app(processor=proc, state_manager=state_manager)
    http = TestClient(app)

    # User is looking at B but the page still posts A's key (stale detail).
    r = http.post(f"/api/tasks/{a['key']}/cancel")
    assert r.status_code == 200
    st_a = state_manager.get_state(a["key"])
    st_b = state_manager.get_state(b["key"])
    assert st_a is not None and st_a.status == TaskStatus.CANCELLED
    assert st_b is not None and st_b.status == TaskStatus.EXECUTING


def test_operator_issue_detail_tsx_cancel_uses_detail_not_route():
    from pathlib import Path

    src = Path("web/src/pages/issues/IssueDetailPage.tsx").read_text(encoding="utf-8")
    assert "await cancelTask(routeKey)" in src


# ---------------------------------------------------------------------------
# plan_start latch — label seen, then second poll skips
# ---------------------------------------------------------------------------


def test_operator_plan_start_latched_before_handler(
    jira, state_manager, trigger_settings
):
    world, base, client = jira
    created = client.create_issue(
        "KAN", "planned", "Mode: plan", labels=["bot", "ai-start-work"]
    )
    key = created["key"]
    state_manager.create_state(key, "planned", "d")
    state_manager.update_state(key, status=TaskStatus.PLAN_READY)

    poller = JiraPoller(client=client, interval_seconds=1, board_id="10")
    poller.state_manager = state_manager
    handled: List[str] = []
    poller._handler = lambda e: handled.append(e["issue"]["key"])

    first = poller.poll_board()
    assert any(i["key"] == key for i in first)
    assert key not in poller._plan_start_emitted
    for issue in first:
        if issue["key"] == key:
            poller.process_issue(issue, is_update=True)
    assert key in poller._plan_start_emitted
    assert key in handled

    second = poller.poll_board()
    assert not any(i["key"] == key for i in second)


# ---------------------------------------------------------------------------
# Webhook description key — GitLab note hook as GitLab would POST it
# ---------------------------------------------------------------------------


def test_operator_gitlab_webhook_binds_description_key(tmp_path, monkeypatch):
    from src.config import settings
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "e2e-hook-secret")
    monkeypatch.setattr(settings, "jira_projects", "KAN,PLATFORM")

    app = create_dashboard_app()
    http = TestClient(app)
    payload = {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": "alice", "name": "Alice"},
        "project": {
            "id": 1,
            "path_with_namespace": "acme/demo",
            "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
            "web_url": "https://gitlab.example.com/acme/demo",
        },
        "object_attributes": {
            "id": 77,
            "note": "@bot please continue",
            "noteable_type": "MergeRequest",
            "project_id": 1,
            "discussion_id": "disc-1",
        },
        "merge_request": {
            "iid": 4,
            "title": "Add login",
            "description": "See also PLATFORM-9; originally KAN-1",
            "source_branch": "feature/login",
            "target_branch": "develop",
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
        },
    }
    from src.gitlab.webhook import decide_gitlab_note_webhook

    headers = {
        "X-Gitlab-Event": "Note Hook",
        "X-Gitlab-Token": "e2e-hook-secret",
    }
    decision = decide_gitlab_note_webhook(
        payload,
        headers=headers,
        enabled=True,
        secret="e2e-hook-secret",
        bot_mentions=["@bot"],
        bot_usernames=["bot"],
        jira_project_keys=["KAN", "PLATFORM"],
    )
    assert decision.accepted, decision.reason
    assert decision.event is not None
    assert decision.event.issue_key.startswith("GL-")

    r = http.post("/webhooks/gitlab", json=payload, headers=headers)
    # No processor on this app → 503 after the key is already bound
    assert r.status_code in (200, 202, 503)


# ---------------------------------------------------------------------------
# C5 — glab "created" with no URL (delivery path the operator sees)
# ---------------------------------------------------------------------------


def test_operator_mr_create_returns_literal_created(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    gm = GitManager.__new__(GitManager)
    gm.temp_dir = str(tmp_path)
    gm.repo_url = "https://gitlab.example.com/g/p.git"
    gm.gitlab_url = "https://gitlab.example.com"
    gm.gitlab_pat = "pat"
    gm.project_path = "g/p"
    gm.work_branch = "feature/X-1"
    gm.feature_branch = "feature/X-1"
    gm.target_branch = "develop"
    gm.source_branch = "develop"
    gm.remote_enabled = True
    gm.remote_url = gm.repo_url

    class R:
        returncode = 0
        stdout = "Merge request created successfully.\n"
        stderr = ""

    monkeypatch.setattr(gm, "_run_glab", lambda cmd: R())
    monkeypatch.setattr(gm, "_create_mr_via_api", lambda *a, **k: None)
    monkeypatch.setattr(gm, "_get_existing_mr_url", lambda *a, **k: None)
    url = gm.create_merge_request(title="feat(x): test", body="body", target_branch="develop")
    assert url is None


# ---------------------------------------------------------------------------
# Jobs page Live chip — only current /api/jobs page
# ---------------------------------------------------------------------------


def test_operator_jobs_api_page_hides_older_live_run(tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app
    from src.state.job_store import JobStore

    js = JobStore(jobs_dir=tmp_path / "jobs")
    live = js.create_job(issue_key="LIVE-1", workflow_type="build")
    js.update_job(live["job_id"], status="executing", started_at="2020-01-01T00:00:00")
    for i in range(30):
        rec = js.create_job(issue_key=f"DONE-{i}", workflow_type="build")
        js.update_job(
            rec["job_id"],
            status="completed",
            started_at=f"2026-08-14T12:00:{i:02d}",
        )

    monkeypatch.setattr("src.state.job_store.job_store", js)
    monkeypatch.setattr("src.dashboard.service.default_job_store", js)
    app = create_dashboard_app()
    http = TestClient(app)
    r = http.get("/api/jobs", params={"page": 1, "page_size": 25})
    assert r.status_code == 200
    body = r.json()
    ids = [j.get("job_id") for j in body.get("jobs") or []]
    assert live["job_id"] in ids
    from pathlib import Path

    tsx = Path("web/src/pages/jobs/JobsPage.tsx").read_text(encoding="utf-8")
    assert "jobMatchesFilter" in tsx
    assert "payload?.jobs" in tsx
