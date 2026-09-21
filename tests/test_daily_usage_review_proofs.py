"""Prove critical daily-usage defects found in a full-tree review.

Operators use the dashboard, Jira board, GitLab MR webhooks, Azure PR /
work-item webhooks, and unattended OpenCode serve. Each test drives those
surfaces over real loopback HTTP (uvicorn + httpx + ThreadingHTTPServer)
and real on-disk stores. No unittest.mock / MagicMock of production
methods.

A failure here means the production path is still wrong.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import pytest
import uvicorn

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import reset_delete_jobs, reset_size_cache
from src.jira.client import JiraClient
from src.jira.plan_labels import PLAN_EXECUTE_LABEL, PLAN_EXECUTED_LABEL
from src.jira.poller import JiraPoller
from src.opencode_serve import last_turn_is_live_question
from src.opencode_sessions import assess_session_completeness
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# Loopback helpers
# ---------------------------------------------------------------------------


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


def _json_send(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _isolate_data(tmp_path: Path, monkeypatch) -> Dict[str, Path]:
    data = tmp_path / "yaver-data"
    plans = data / "plans"
    state_dir = data / "state"
    temp = tmp_path / "t"
    plans.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    temp.mkdir(parents=True)
    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setattr(settings, "temp_dir_base", temp)
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    reset_delete_jobs()
    reset_size_cache()
    return {"data": data, "plans": plans, "state_dir": state_dir, "temp": temp}


# ---------------------------------------------------------------------------
# 1) GitLab merge webhook deletes the plan named in the MR title (intentional)
# ---------------------------------------------------------------------------


def test_gitlab_merge_webhook_deletes_plan_named_in_mr_title(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Merging ``feat(KAN-12): …`` deletes ``plans/KAN-12.md`` and local state.

    Intentional: the Jira key in the MR title is the cleanup identity.
    """
    paths = _isolate_data(tmp_path, monkeypatch)
    plan = paths["plans"] / "KAN-12.md"
    plan.write_text("# plan for login\n\n1. implement auth\n", encoding="utf-8")

    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "secret")
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    sm = JiraStateManager(state_dir=paths["state_dir"])
    sm.create_state("KAN-12", "Plan login", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY, plan_path=str(plan))

    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
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
                    "iid": 9,
                    "action": "merge",
                    "state": "merged",
                    "title": "feat(KAN-12): login",
                    "description": "",
                    "source_branch": "feature/login",
                    "target_branch": "develop",
                    "url": "https://gitlab.example.com/acme/app/-/merge_requests/9",
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
        assert not plan.is_file(), "MR title KAN-12 must delete plans/KAN-12.md"
        assert sm.get_state("KAN-12") is None
    finally:
        server.should_exit = True


def test_gitlab_merge_webhook_still_purges_synthetic_gl_key(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Fallback ``GL-…`` keys exist only for that MR job — purge may drop them."""
    paths = _isolate_data(tmp_path, monkeypatch)
    key = "GL-ACME-APP-9"
    plan = paths["plans"] / f"{key}.md"
    plan.write_text("# leftover review notes\n", encoding="utf-8")

    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "secret")
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    sm = JiraStateManager(state_dir=paths["state_dir"])
    sm.create_state(key, "MR !9", "")
    sm.update_state(key, status=TaskStatus.COMPLETED)

    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
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
                    "iid": 9,
                    "action": "merge",
                    "state": "merged",
                    "title": "chore: no jira key",
                    "description": "",
                    "source_branch": "feature/x",
                    "target_branch": "develop",
                    "url": "https://gitlab.example.com/acme/app/-/merge_requests/9",
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
        assert not plan.is_file(), "synthetic GL- plan should still be purged"
        assert sm.get_state(key) is None
    finally:
        server.should_exit = True


# ---------------------------------------------------------------------------
# 2) GitLab /review on feat(KAN-12) must not destroy Jira plan_ready
# ---------------------------------------------------------------------------


def test_gitlab_review_on_plan_ready_jira_key_must_not_reset_the_plan(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """``@bot /review`` on MR titled feat(KAN-12) while Jira is plan_ready.

    /yaver correctly waits. /review force-resets the same issue to PENDING
    and overwrites gitlab metadata, so the waiting plan is gone.
    """
    paths = _isolate_data(tmp_path, monkeypatch)
    plan = paths["plans"] / "KAN-12.md"
    plan.write_text("# plan\n", encoding="utf-8")

    monkeypatch.setattr(settings, "gitlab_webhook_enabled", True)
    monkeypatch.setattr(settings, "gitlab_webhook_secret", "secret")
    monkeypatch.setattr(settings, "gitlab_trigger_user", "yaver")
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    sm = JiraStateManager(state_dir=paths["state_dir"])
    sm.create_state("KAN-12", "Plan login", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY, plan_path=str(plan))

    proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    app = create_dashboard_app(processor=proc, state_manager=sm)
    port = _free_port()
    server = _start_uvicorn(app, port)
    try:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/yaver/webhook/gitlab",
            headers={
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Token": "secret",
            },
            json={
                "object_kind": "note",
                "event_type": "note",
                "user": {"username": "alice", "name": "Alice"},
                "project": {
                    "id": 1,
                    "path_with_namespace": "acme/demo",
                    "http_url_to_repo": "http://127.0.0.1:1/acme/demo.git",
                    "web_url": "http://127.0.0.1:1/acme/demo",
                },
                "object_attributes": {
                    "id": 501,
                    "note": "@yaver /review nits on the diff",
                    "noteable_type": "MergeRequest",
                    "project_id": 1,
                    "discussion_id": "disc-review",
                },
                "merge_request": {
                    "iid": 4,
                    "title": "feat(KAN-12): login",
                    "description": "",
                    "source_branch": "feature/login",
                    "target_branch": "develop",
                    "web_url": "http://127.0.0.1:1/acme/demo/-/merge_requests/4",
                },
                "repository": {"url": "http://127.0.0.1:1/acme/demo.git"},
            },
            timeout=15.0,
            verify=False,
        )
        assert resp.status_code == 200, resp.text
        deadline = time.time() + 3.0
        live = sm.get_state("KAN-12")
        while time.time() < deadline:
            live = sm.get_state("KAN-12")
            if live and live.status != TaskStatus.PLAN_READY:
                break
            time.sleep(0.05)
        assert live is not None
        assert live.status == TaskStatus.PLAN_READY, (
            f"GitLab /review on feat(KAN-12) reset plan_ready to "
            f"{live.status.value}. A review of the MR must not discard the "
            "waiting Jira plan."
        )
        assert plan.is_file()
    finally:
        server.should_exit = True
        try:
            proc.shutdown_processing(reason="test teardown")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 3) Azure PR /yaver on plan_ready must reply (GitLab already does)
# ---------------------------------------------------------------------------


def _start_azure_listener() -> Tuple[str, List[str], ThreadingHTTPServer]:
    posts: List[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.lower()
            if "threads" in path:
                return _json_send(self, 200, {"value": []})
            if "connectiondata" in path:
                return _json_send(
                    self,
                    200,
                    {
                        "authenticatedUser": {
                            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "providerDisplayName": "Yaver",
                            "properties": {
                                "Account": {"$value": "CORP\\yaver"}
                            },
                        }
                    },
                )
            return _json_send(self, 200, {"value": []})

        def do_POST(self) -> None:  # noqa: N802
            data = _read_json(self)
            body = str(data.get("content") or data.get("comments") or data)
            posts.append(body)
            return _json_send(self, 201, {"id": 1, "content": body})

        def do_PATCH(self) -> None:  # noqa: N802
            return self.do_POST()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}", posts, httpd


@pytest.mark.asyncio
async def test_azure_yaver_on_plan_ready_must_post_a_wait_note_on_the_pr(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """``@yaver /yaver`` on a PR titled feat(KAN-12) while Jira is plan_ready.

    Waiting is intentional (do not start implement). The PR thread must
    still get the same wait note GitLab posts, not stay silent.
    """
    paths = _isolate_data(tmp_path, monkeypatch)
    plan = paths["plans"] / "KAN-12.md"
    plan.write_text("# plan\n", encoding="utf-8")

    azure_base, posts, httpd = _start_azure_listener()
    collection = f"{azure_base}/tfs/DefaultCollection"
    try:
        monkeypatch.setattr(settings, "azure_webhook_enabled", True)
        monkeypatch.setattr(settings, "azure_trigger_user", "yaver")
        monkeypatch.setattr(settings, "jira_projects", "KAN")
        monkeypatch.setattr(
            settings,
            "azure_collection_pats",
            json.dumps({collection: "tfs-pat"}),
        )
        if hasattr(settings, "set_azure_collection_pat_map"):
            settings.set_azure_collection_pat_map({collection: "tfs-pat"})

        sm = JiraStateManager(state_dir=paths["state_dir"])
        sm.create_state("KAN-12", "Plan login", "Mode: plan")
        sm.update_state("KAN-12", status=TaskStatus.PLAN_READY, plan_path=str(plan))

        proc = JobProcessor()
        proc.state_manager = sm
        proc.job_store = isolate_jira_agent_artifacts["job_store"]
        proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
        app = create_dashboard_app(processor=proc, state_manager=sm)
        port = _free_port()
        server = _start_uvicorn(app, port)
        try:
            remote = f"{collection}/Demo/_git/demo"
            resp = httpx.post(
                f"http://127.0.0.1:{port}/yaver/webhook/azure",
                json={
                    "eventType": "ms.vss-code.git-pullrequest-comment-event",
                    "resource": {
                        "comment": {
                            "id": 77,
                            "parentCommentId": 0,
                            "threadId": 8,
                            "author": {
                                "displayName": "Alice",
                                "uniqueName": "CORP\\alice",
                                "id": "user-1",
                            },
                            "content": "@yaver /yaver implement the plan",
                            "commentType": 1,
                        },
                        "pullRequest": {
                            "pullRequestId": 44,
                            "status": "active",
                            "title": "feat(KAN-12): login",
                            "description": "",
                            "sourceRefName": "refs/heads/feature/login",
                            "targetRefName": "refs/heads/develop",
                            "repository": {
                                "id": "repo-guid",
                                "name": "demo",
                                "remoteUrl": remote,
                                "project": {"name": "Demo"},
                                "_links": {
                                    "web": {
                                        "href": f"{collection}/Demo/_git/demo/pullrequest/44"
                                    }
                                },
                            },
                        },
                    },
                    "resourceContainers": {
                        "collection": {"baseUrl": collection + "/"}
                    },
                },
                timeout=15.0,
                verify=False,
            )
            assert resp.status_code == 200, resp.text
            deadline = time.time() + 6.0
            while time.time() < deadline and not posts:
                await asyncio.sleep(0.05)
            joined = "\n".join(posts)
            assert posts, (
                "Azure /yaver on a plan_ready Jira key posted nothing on the PR "
                f"(queue={resp.json()!r}). GitLab posts a wait note on the same path."
            )
            assert "plan_execute" in joined.lower() or "plan_ready" in joined.lower(), (
                f"PR reply did not tell the operator to wait for plan_execute: {joined!r}"
            )
            live = sm.get_state("KAN-12")
            assert live is not None
            assert live.status == TaskStatus.PLAN_READY
            qid = resp.json().get("queue_id")
            rec = proc.queue_store.get(qid) if qid else None
            assert rec is not None
            assert rec.get("status") == "skipped", rec
        finally:
            server.should_exit = True
            try:
                proc.shutdown_processing(reason="test teardown")
            except Exception:
                pass
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 4) Failed plan_execute must remain retryable while In Progress
# ---------------------------------------------------------------------------


class _JiraBoard:
    def __init__(self) -> None:
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.comments: Dict[str, List[str]] = {}

    def put_issue(
        self,
        key: str,
        *,
        summary: str,
        description: str,
        labels: List[str],
        status: str,
    ) -> None:
        self.issues[key] = {
            "key": key,
            "summary": summary,
            "description": description,
            "labels": list(labels),
            "status": status,
        }

    def as_jira(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        name = rec["status"]
        category = (
            "indeterminate"
            if name.lower() in {"in progress", "in-progress"}
            else "new"
        )
        return {
            "key": rec["key"],
            "id": rec["key"],
            "fields": {
                "summary": rec["summary"],
                "description": rec["description"],
                "labels": rec["labels"],
                "assignee": {"displayName": "DevBot", "name": "devbot"},
                "status": {
                    "name": name,
                    "statusCategory": {"key": category},
                },
                "issuetype": {"name": "Task"},
            },
        }


def _start_jira_board(board: _JiraBoard) -> Tuple[str, ThreadingHTTPServer]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path.endswith("/sprint"):
                return _json_send(
                    self, 400, {"errorMessages": ["Board does not support sprints"]}
                )
            if path.endswith("/issue") and "/board/" in path:
                issues = [board.as_jira(r) for r in board.issues.values()]
                return _json_send(
                    self, 200, {"issues": issues, "total": len(issues)}
                )
            if "/transitions" in path:
                return _json_send(
                    self,
                    200,
                    {
                        "transitions": [
                            {
                                "id": "21",
                                "name": "In Progress",
                                "to": {
                                    "name": "In Progress",
                                    "statusCategory": {"key": "indeterminate"},
                                },
                            }
                        ]
                    },
                )
            if path.startswith("/rest/api/2/issue/"):
                key = path.split("/issue/")[1].split("/")[0]
                rec = board.issues.get(key)
                if not rec:
                    return _json_send(self, 404, {"errorMessages": ["missing"]})
                return _json_send(self, 200, board.as_jira(rec))
            return _json_send(self, 404, {"errorMessages": [path]})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            data = _read_json(self)
            if path.endswith("/transitions"):
                key = path.split("/issue/")[1].split("/")[0]
                rec = board.issues.get(key)
                if rec:
                    rec["status"] = "In Progress"
                return _json_send(self, 204, {})
            if path.endswith("/comment"):
                key = path.split("/issue/")[1].split("/")[0]
                body = data.get("body") or ""
                board.comments.setdefault(key, []).append(str(body))
                return _json_send(self, 201, {"id": "c1", "body": body})
            return _json_send(self, 404, {})

        def do_PUT(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            data = _read_json(self)
            if path.startswith("/rest/api/2/issue/"):
                key = path.split("/issue/")[1].split("/")[0]
                rec = board.issues.get(key)
                if not rec:
                    return _json_send(self, 404, {})
                fields = data.get("fields") or {}
                if "labels" in fields:
                    rec["labels"] = [str(x) for x in (fields.get("labels") or [])]
                if "summary" in fields:
                    rec["summary"] = str(fields.get("summary") or rec["summary"])
                if "description" in fields:
                    rec["description"] = str(
                        fields.get("description") or rec["description"]
                    )
                return _json_send(self, 204, {})
            return _json_send(self, 404, {})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}", httpd


@pytest.mark.asyncio
async def test_failed_plan_execute_stays_retryable_on_in_progress(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """AGENTS.md: error + In Progress + plan_execute retries implement.

    Clone/agent failure must leave ``plan_execute`` on the ticket so the
    next poll retries implement. To Do rework with Mode: plan starts a
    new plan instead.
    """
    paths = _isolate_data(tmp_path, monkeypatch)
    plan = paths["plans"] / "KAN-8.md"
    plan.write_text("# implement login\n", encoding="utf-8")

    board = _JiraBoard()
    desc = (
        "{params}\n"
        "Repository: https://127.0.0.1:1/acme/app.git\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: plan\n"
        "{params}\n"
    )
    board.put_issue(
        "KAN-8",
        summary="Implement login",
        description=desc,
        labels=["plan_execute"],
        status="In Progress",
    )
    jira_base, httpd = _start_jira_board(board)
    try:
        monkeypatch.setattr(settings, "jira_host", jira_base)
        monkeypatch.setattr(settings, "jira_api_token", "tok")
        monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
        monkeypatch.setattr(settings, "jira_trigger_label", "")
        monkeypatch.setattr(settings, "jira_board_id", "1")
        monkeypatch.setattr(settings, "jira_projects", "KAN")
        monkeypatch.setattr(settings, "gitlab_pat", "")
        if hasattr(settings, "set_gitlab_host_pat_map"):
            settings.set_gitlab_host_pat_map({})

        client = JiraClient(host=jira_base, api_token="tok", email="")
        sm = JiraStateManager(state_dir=paths["state_dir"])
        sm.create_state("KAN-8", "Implement login", desc)
        sm.update_state(
            "KAN-8",
            status=TaskStatus.PLAN_READY,
            plan_path=str(plan),
            description=desc,
            issue_summary="Implement login",
        )

        proc = JobProcessor()
        proc.state_manager = sm
        proc.jira_client = client
        proc.reporter = JiraReporter(client=client)
        proc.job_store = isolate_jira_agent_artifacts["job_store"]
        proc.queue_store = isolate_jira_agent_artifacts["queue_store"]

        poller = JiraPoller(
            client=client, interval_seconds=1, board_id="1", state_manager=sm
        )
        proc._poller = poller

        event = {
            "webhookEvent": "jira:issue_updated",
            "plan_handoff": "execute",
            "issue": board.as_jira(board.issues["KAN-8"]),
        }
        await proc.process_event(event)

        labels = [str(x).lower() for x in board.issues["KAN-8"]["labels"]]
        assert PLAN_EXECUTE_LABEL in labels, (
            f"plan_execute was consumed before the build finished "
            f"(labels={labels}). Clone/agent failure left the ticket unable "
            "to retry implement on the next poll."
        )
        assert PLAN_EXECUTED_LABEL not in labels

        live = sm.get_state("KAN-8")
        assert live is not None
        assert live.status == TaskStatus.ERROR

        intake = poller.poll_board()
        keys = [i.get("key") for i in intake]
        assert "KAN-8" in keys, (
            f"In Progress + ERROR after a failed plan_execute was not on the "
            f"next poll ({keys}). AGENTS.md retries implement when plan_execute "
            "is still set."
        )
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# 5) Compact recap quoting "Shall I…?" is not a live operator question
# ---------------------------------------------------------------------------


def _compaction_recap_quoting_shall_i() -> List[Dict[str, Any]]:
    """OpenCode 1.18 GET /session/{{id}}/message shape after compact-then-stop."""
    return [
        {
            "info": {
                "id": "msg_user_1",
                "role": "user",
                "finish": None,
                "agent": None,
                "summary": None,
            },
            "parts": [{"type": "text", "text": "implement the plan KAN-12.md"}],
        },
        {
            "info": {
                "id": "msg_ask_1",
                "role": "assistant",
                "finish": "stop",
                "agent": "derman-build",
                "summary": None,
            },
            "parts": [
                {
                    "type": "text",
                    "text": "Shall I continue with the remaining 11 steps?",
                }
            ],
        },
        {
            "info": {
                "id": "msg_nudge_1",
                "role": "user",
                "finish": None,
                "agent": None,
                "summary": None,
            },
            "parts": [{"type": "text", "text": "Continue. Do not ask questions."}],
        },
        {
            "info": {
                "id": "msg_compact_1",
                "role": "assistant",
                "finish": None,
                "agent": "compaction",
                "summary": True,
            },
            "parts": [],
        },
        {
            "info": {
                "id": "msg_recap_1",
                "role": "assistant",
                "finish": None,
                "agent": "compaction",
                "summary": True,
            },
            "parts": [
                {
                    "type": "text",
                    "text": (
                        "## Compaction\nPreviously: Shall I continue with the "
                        "remaining 11 steps? Work still in progress."
                    ),
                }
            ],
        },
    ]


def test_compact_recap_quoting_shall_i_is_not_a_live_question():
    """Long jobs compact on the nudge turn. The recap quotes 'Shall I…?'.

    last_turn_is_live_question is the gate used on busy-wait. Idle compact-wait
    still treats assistant_asked_question as a live ask and leaves wait / ERROR.
    """
    messages = _compaction_recap_quoting_shall_i()
    assessment = assess_session_completeness(
        "ses_live_recap",
        messages=messages,
        todos=[
            {"content": f"step {i}", "status": "pending"} for i in range(11)
        ],
    )
    assert assessment.get("last_is_summary") is True, assessment
    assert last_turn_is_live_question(assessment) is False, (
        "compaction recap quoting Shall I…? was classified as a live operator "
        f"question: {assessment}"
    )
    # The idle-wait short-circuit in opencode_serve._wait_for_auto_compact
    # (asked = assistant_asked_question OR reason text) must match this gate.
    asked_idle = bool(assessment.get("assistant_asked_question")) or any(
        "clarifying question" in str(r).lower()
        for r in (assessment.get("reasons") or [])
    )
    assert asked_idle is False or last_turn_is_live_question(assessment), (
        "Idle compact-wait would leave for an unattended nudge because the "
        "recap quotes an earlier Shall I…? "
        f"assistant_asked_question={assessment.get('assistant_asked_question')} "
        f"reasons={assessment.get('reasons')}"
    )


@pytest.mark.asyncio
async def test_live_opencode_session_recap_is_not_treated_as_a_question(
    tmp_path,
):
    """Create a real ``ses_*`` on opencode serve, then assess a recap payload.

    Skipped only when the OpenCode binary cannot start serve.
    """
    import shutil
    import subprocess

    bin_path = shutil.which("opencode") or os.path.expandvars(
        r"%USERPROFILE%\.opencode\bin\opencode.exe"
    )
    if not bin_path or not Path(bin_path).is_file():
        pytest.skip("opencode binary not available")

    port = _free_port()
    env = os.environ.copy()
    env["OPENCODE_SERVER_PASSWORD"] = ""
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    log = tmp_path / "serve.log"
    log_f = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [str(bin_path), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=str(tmp_path),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 45.0
        healthy = False
        while time.time() < deadline:
            try:
                r = httpx.get(f"{base}/global/health", timeout=1.0, verify=False)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                time.sleep(0.3)
        if not healthy:
            pytest.skip("opencode serve did not become healthy")

        created = httpx.post(
            f"{base}/session",
            json={"title": "KAN-12 compact recap proof"},
            timeout=15.0,
            verify=False,
        )
        assert created.status_code < 400, created.text
        payload = created.json()
        sid = str(
            (payload.get("id") if isinstance(payload, dict) else "")
            or (payload.get("data") or {}).get("id")
            or ""
        )
        if not sid.startswith("ses_"):
            inner = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(inner, dict):
                sid = str(inner.get("id") or "")
        assert sid.startswith("ses_"), payload

        listed = httpx.get(
            f"{base}/session/{sid}/message",
            params={"limit": 50},
            timeout=15.0,
            verify=False,
        )
        assert listed.status_code == 200, listed.text
        live_messages = listed.json()
        if isinstance(live_messages, dict):
            live_messages = (
                live_messages.get("data")
                or live_messages.get("messages")
                or []
            )
        assert isinstance(live_messages, list)

        recap = _compaction_recap_quoting_shall_i()
        combined = list(live_messages) + recap
        assessment = assess_session_completeness(
            sid,
            messages=combined,
            todos=[{"content": "still working", "status": "in_progress"}],
        )
        assert last_turn_is_live_question(assessment) is False, assessment
        asked_idle = bool(assessment.get("assistant_asked_question")) or any(
            "clarifying question" in str(r).lower()
            for r in (assessment.get("reasons") or [])
        )
        assert not asked_idle or last_turn_is_live_question(assessment), (
            f"live session {sid} recap would abort compact-wait as a question: "
            f"{assessment}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_f.close()
