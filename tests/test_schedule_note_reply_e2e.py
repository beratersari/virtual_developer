"""Real HTTP proofs: scheduled MR/PR prompts are notes; answers reply to them.

No MagicMock of GitLab or Azure clients. Local ThreadingHTTPServer instances
speak the REST the production clients call. A failure means the schedule
path opened a resolvable thread again, or the answer was not a reply.
"""

from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest

from src.config import settings
from src.processor import JobProcessor
from src.scheduler.service import (
    dispatch_schedule_now,
    schedule_mr_followup,
    schedule_pr_followup,
    wait_inflight_dispatches,
)
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
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{host}:{port} did not accept connections")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, path: str, body: Any) -> None:
        self.calls.append({"method": method, "path": path, "body": body})

    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


def _serve(handler_cls) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    return httpd


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _send(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class _GitlabNoteServer(BaseHTTPRequestHandler):
    rec = _Recorder()
    notes: list[dict[str, Any]] = []
    next_id = 1

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self.rec.add("GET", path, None)
        if path.endswith("/merge_requests/4") and "/notes" not in path:
            _send(
                self,
                200,
                {
                    "iid": 4,
                    "id": 40,
                    "title": "feat(KAN-12): add login",
                    "description": "desc",
                    "source_branch": "feature/login",
                    "target_branch": "develop",
                    "web_url": "http://127.0.0.1/acme/demo/-/merge_requests/4",
                    "project_id": 1,
                    "state": "opened",
                },
            )
            return
        if "/merge_requests/4/notes/" in path:
            nid = path.rsplit("/", 1)[-1]
            for note in self.notes:
                if str(note.get("id")) == nid:
                    _send(self, 200, note)
                    return
            _send(self, 404, {"message": "Not found"})
            return
        if path.endswith("/merge_requests/4/discussions"):
            groups: dict[str, list] = {}
            order: list[str] = []
            for note in self.notes:
                did = str(note.get("discussion_id") or "")
                if did not in groups:
                    groups[did] = []
                    order.append(did)
                groups[did].append(note)
            _send(self, 200, [{"id": did, "notes": groups[did]} for did in order])
            return
        if path.endswith("/projects") or "/projects/" in path:
            _send(
                self,
                200,
                {
                    "id": 1,
                    "path_with_namespace": "acme/demo",
                }
                if "/projects/" in path
                else [{"id": 1, "path_with_namespace": "acme/demo"}],
            )
            return
        _send(self, 404, {"message": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = _read_json(self)
        self.rec.add("POST", path, payload)
        text = str((payload or {}).get("body") or "")
        if path.endswith("/merge_requests/4/notes"):
            note = {
                "id": self.next_id,
                "body": text,
                "discussion_id": "disc-sched",
                "type": None,
                "resolvable": False,
            }
            type(self).next_id += 1
            self.notes.append(note)
            _send(self, 201, note)
            return
        if "/merge_requests/4/discussions/" in path and path.endswith("/notes"):
            did = path.split("/discussions/", 1)[1].split("/", 1)[0]
            note = {
                "id": self.next_id,
                "body": text,
                "discussion_id": did,
                "type": "DiscussionNote",
            }
            type(self).next_id += 1
            self.notes.append(note)
            _send(self, 201, note)
            return
        if path.endswith("/merge_requests/4/discussions"):
            # Creating a resolvable thread — the bug this test exists to catch.
            _send(self, 201, {"id": "disc-thread", "notes": [{"id": 99, "body": text}]})
            return
        _send(self, 404, {"message": "Not found"})


class _AzureNoteServer(BaseHTTPRequestHandler):
    rec = _Recorder()
    next_thread = 8
    next_comment = 1
    threads: dict[str, list[dict[str, Any]]] = {}

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        self.rec.add("GET", path, None)
        if "/pullrequests/4" in path and "/threads" not in path:
            _send(
                self,
                200,
                {
                    "pullRequestId": 4,
                    "title": "feat(KAN-12): add login",
                    "description": "desc",
                    "status": "active",
                    "sourceRefName": "refs/heads/feature/login",
                    "targetRefName": "refs/heads/develop",
                    "repository": {
                        "id": "repo-guid",
                        "name": "demo",
                        "remoteUrl": (
                            f"http://{self.headers.get('Host')}"
                            "/tfs/DefaultCollection/Demo/_git/demo"
                        ),
                        "project": {"name": "Demo"},
                    },
                    "_links": {
                        "web": {
                            "href": (
                                f"http://{self.headers.get('Host')}"
                                "/tfs/DefaultCollection/Demo/_git/demo/pullrequest/4"
                            )
                        }
                    },
                },
            )
            return
        _send(self, 404, {"message": "Not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = _read_json(self)
        self.rec.add("POST", path, payload)
        if path.rstrip("/").endswith("/pullrequests/4/threads"):
            tid = str(self.next_thread)
            type(self).next_thread += 1
            comments = list(payload.get("comments") or [])
            stored = []
            for item in comments:
                cid = self.next_comment
                type(self).next_comment += 1
                stored.append(
                    {
                        "id": cid,
                        "content": item.get("content"),
                        "parentCommentId": item.get("parentCommentId", 0),
                        "commentType": item.get("commentType", 1),
                    }
                )
            self.threads[tid] = stored
            _send(
                self,
                201,
                {
                    "id": int(tid),
                    "status": payload.get("status"),
                    "comments": stored,
                },
            )
            return
        if "/pullrequests/4/threads/" in path and path.rstrip("/").endswith("/comments"):
            tid = path.split("/threads/", 1)[1].split("/", 1)[0]
            cid = self.next_comment
            type(self).next_comment += 1
            comment = {
                "id": cid,
                "content": payload.get("content"),
                "parentCommentId": payload.get("parentCommentId", 0),
                "commentType": payload.get("commentType", 1),
            }
            self.threads.setdefault(tid, []).append(comment)
            _send(self, 201, comment)
            return
        _send(self, 404, {"message": "Not found"})


class _GitlabAnswerProcessor(JobProcessor):
    async def _run_gitlab_mr_comment(self, event) -> bool:
        st = self.state_manager.get_state(event.issue_key)
        if st is None:
            st = self.state_manager.create_state(
                event.issue_key, event.mr_title, event.prompt
            )
        self.state_manager.update_state(
            event.issue_key,
            force=True,
            status=TaskStatus.COMPLETED,
            metadata={
                "source": "gitlab",
                "gitlab_host": event.host,
                "gitlab_project": event.project_path,
                "gitlab_project_id": event.project_id or None,
                "gitlab_mr_iid": event.mr_iid,
                "merge_request_url": event.mr_url,
                "gitlab_discussion_id": event.discussion_id,
                "gitlab_note_id": event.note_id,
                "workflow_type": "gitlab_mr",
            },
        )
        live = self.state_manager.get_state(event.issue_key) or st
        return bool(self._post_gitlab_mr_reply(live, "Added the log line."))


class _AzureAnswerProcessor(JobProcessor):
    async def _run_azure_pr_comment(self, event) -> bool:
        st = self.state_manager.get_state(event.issue_key)
        if st is None:
            st = self.state_manager.create_state(
                event.issue_key, event.pr_title, event.prompt
            )
        self.state_manager.update_state(
            event.issue_key,
            force=True,
            status=TaskStatus.COMPLETED,
            metadata={
                "source": "azure",
                "azure_host": event.host,
                "azure_collection_url": event.collection_url,
                "azure_project": event.project,
                "azure_repository": event.repository_name,
                "azure_repository_id": event.repository_id,
                "azure_pr_id": event.pr_id,
                "azure_thread_id": event.thread_id,
                "azure_comment_id": event.comment_id,
                "workflow_type": "azure_pr",
            },
        )
        live = self.state_manager.get_state(event.issue_key) or st
        return bool(self._post_azure_pr_reply(live, "Added the log line."))


@pytest.fixture
def gitlab_http(monkeypatch):
    _GitlabNoteServer.rec = _Recorder()
    _GitlabNoteServer.notes = []
    _GitlabNoteServer.next_id = 1
    httpd = _serve(_GitlabNoteServer)
    host, port = httpd.server_address
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    monkeypatch.setattr(settings, "gitlab_pat", "glpat-e2e")
    try:
        yield {
            "host": f"{host}:{port}",
            "repo": f"http://{host}:{port}/acme/demo.git",
            "rec": _GitlabNoteServer.rec,
            "notes": _GitlabNoteServer.notes,
        }
    finally:
        httpd.shutdown()


@pytest.fixture
def azure_http(monkeypatch):
    _AzureNoteServer.rec = _Recorder()
    _AzureNoteServer.next_thread = 8
    _AzureNoteServer.next_comment = 1
    _AzureNoteServer.threads = {}
    httpd = _serve(_AzureNoteServer)
    host, port = httpd.server_address
    collection = f"http://{host}:{port}/tfs/DefaultCollection"
    monkeypatch.setattr(settings, "jira_projects", "KAN")
    monkeypatch.setattr(settings, "azure_host_pats", "")
    monkeypatch.setattr(settings, "azure_allowed_hosts", "")
    monkeypatch.setattr(settings, "azure_pat", "azpat-e2e")
    try:
        yield {
            "host": f"{host}:{port}",
            "collection": collection,
            "repo": f"{collection}/Demo/_git/demo",
            "rec": _AzureNoteServer.rec,
            "threads": _AzureNoteServer.threads,
        }
    finally:
        httpd.shutdown()


@pytest.mark.asyncio
async def test_gitlab_schedule_posts_note_then_reply(tmp_path, gitlab_http, fake_jira):
    from unittest.mock import patch

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = _GitlabAnswerProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")

    when = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    created = schedule_mr_followup(
        repository_url=gitlab_http["repo"],
        mr_iid=4,
        prompt="Please add a log line when login fails.",
        scheduled_at=when,
        store=store,
    )
    assert created.get("ok") is True, created
    launched = dispatch_schedule_now(
        created["schedule"]["schedule_id"], processor=proc, store=store
    )
    assert launched.get("ok") is True, launched
    await wait_inflight_dispatches()

    posts = gitlab_http["rec"].posts()
    assert posts, gitlab_http["rec"].calls
    prompt_posts = [
        p
        for p in posts
        if p["path"].endswith("/merge_requests/4/notes")
        and "/discussions/" not in p["path"]
    ]
    reply_posts = [
        p for p in posts if "/discussions/disc-sched/notes" in p["path"]
    ]
    thread_creates = [
        p
        for p in posts
        if p["path"].endswith("/merge_requests/4/discussions")
    ]
    assert prompt_posts, posts
    assert "Please add a log line" in str(prompt_posts[0]["body"])
    assert reply_posts, posts
    assert "Added the log line" in str(reply_posts[0]["body"])
    assert not thread_creates, thread_creates
    notes = gitlab_http["notes"]
    prompt = next(n for n in notes if "Please add a log line" in str(n.get("body")))
    answer = next(n for n in notes if "Added the log line" in str(n.get("body")))
    assert prompt["discussion_id"] == "disc-sched"
    assert answer["discussion_id"] == prompt["discussion_id"]
    assert prompt.get("resolvable") is False


@pytest.mark.asyncio
async def test_azure_schedule_posts_closed_note_then_parent_reply(
    tmp_path, azure_http, fake_jira
):
    from unittest.mock import patch

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = _AzureAnswerProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")

    when = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    created = schedule_pr_followup(
        repository_url=azure_http["repo"],
        pr_id=4,
        prompt="Please add a log line when login fails.",
        scheduled_at=when,
        store=store,
    )
    assert created.get("ok") is True, created
    launched = dispatch_schedule_now(
        created["schedule"]["schedule_id"], processor=proc, store=store
    )
    assert launched.get("ok") is True, launched
    await wait_inflight_dispatches()

    posts = azure_http["rec"].posts()
    assert posts, azure_http["rec"].calls
    note_posts = [
        p
        for p in posts
        if p["path"].rstrip("/").endswith("/pullrequests/4/threads")
    ]
    reply_posts = [
        p
        for p in posts
        if "/pullrequests/4/threads/" in p["path"] and p["path"].endswith("/comments")
    ]
    assert note_posts, posts
    note_body = note_posts[0]["body"] or {}
    assert note_body.get("status") == 4
    first = (note_body.get("comments") or [{}])[0]
    assert first.get("parentCommentId") == 0
    assert "Please add a log line" in str(first.get("content") or "")
    assert reply_posts, posts
    reply_body = reply_posts[0]["body"] or {}
    assert reply_body.get("parentCommentId") == 1
    assert "Added the log line" in str(reply_body.get("content") or "")
    assert note_posts[0]["path"].rstrip("/").endswith("/threads")
    assert "/threads/8/comments" in reply_posts[0]["path"]
