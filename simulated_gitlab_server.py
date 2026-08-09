#!/usr/bin/env python3
"""Simulated GitLab (CE-compatible) for webhook + MR note testing.

Implements the REST pieces Virtual Developer uses on **all plans**:

* ``GET  /api/v4/user``
* ``GET  /api/v4/projects``
* ``GET  /api/v4/projects/:id``
* ``GET/POST /api/v4/projects/:id/merge_requests/:iid/notes``

Plus a helper that creates a comment **and** fires a Note Hook at the daemon:

    POST /simulate/comment
    {"project_id": 1, "mr_iid": 1, "body": "@berat_ai what is this?", "username": "alice"}

Run::

    python simulated_gitlab_server.py
    # http://127.0.0.1:8091/
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import httpx
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DAEMON_WEBHOOK = os.environ.get(
    "VD_GITLAB_WEBHOOK_URL", "http://127.0.0.1:8080/webhooks/gitlab"
)
WEBHOOK_SECRET = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
SIM_PORT = int(os.environ.get("GITLAB_SIM_PORT", "8091"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Note:
    id: int
    body: str
    author: str
    created_at: str
    discussion_id: str = ""

    def to_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "body": self.body,
            "created_at": self.created_at,
            "author": {"username": self.author, "name": self.author},
            "noteable_type": "MergeRequest",
            "discussion_id": self.discussion_id,
        }


@dataclass
class MergeRequest:
    iid: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    web_url: str
    notes: List[Note] = field(default_factory=list)
    next_note_id: int = 1


@dataclass
class Project:
    id: int
    path_with_namespace: str
    http_url: str
    web_url: str
    mrs: Dict[int, MergeRequest] = field(default_factory=dict)


_lock = threading.Lock()
_projects: Dict[int, Project] = {}


def reset_demo_data() -> None:
    """Idempotent seed used by tests and first boot."""
    host = f"http://127.0.0.1:{SIM_PORT}"
    with _lock:
        _projects.clear()
        proj = Project(
            id=1,
            path_with_namespace="acme/demo",
            http_url=f"{host}/acme/demo.git",
            web_url=f"{host}/acme/demo",
        )
        proj.mrs[1] = MergeRequest(
            iid=1,
            title="Add login button",
            description="Demo MR for webhook intake.",
            source_branch="feature/login",
            target_branch="develop",
            web_url=f"{host}/acme/demo/-/merge_requests/1",
        )
        _projects[1] = proj


reset_demo_data()


def _project(pid: str) -> Optional[Project]:
    raw = unquote(pid or "")
    with _lock:
        if raw.isdigit():
            return _projects.get(int(raw))
        for p in _projects.values():
            if p.path_with_namespace.lower() == raw.lower():
                return p
    return None


@app.get("/")
def index():
    return (
        """<!doctype html><html><body style="font-family:sans-serif;max-width:40rem;margin:2rem">
<h1>Simulated GitLab</h1>
<p>CE-compatible notes API + Note Hook helper.</p>
<form method="post" action="/simulate/comment" style="display:grid;gap:.5rem">
<label>Project id <input name="project_id" value="1"></label>
<label>MR iid <input name="mr_iid" value="1"></label>
<label>Username <input name="username" value="alice"></label>
<label>Comment<br>
<textarea name="body" rows="4" cols="60">@berat_ai explain the login change</textarea>
</label>
<button type="submit">Post comment + fire webhook</button>
</form>
<p>Notes: <a href="/api/v4/projects/1/merge_requests/1/notes">/api/v4/projects/1/merge_requests/1/notes</a></p>
</body></html>""",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/api/v4/user")
def api_user():
    return jsonify({"id": 9, "username": "sim-bot", "name": "Simulated GitLab"})


@app.get("/api/v4/projects")
def api_projects():
    with _lock:
        rows = [
            {
                "id": p.id,
                "path_with_namespace": p.path_with_namespace,
                "http_url_to_repo": p.http_url,
                "web_url": p.web_url,
            }
            for p in _projects.values()
        ]
    return jsonify(rows)


@app.get("/api/v4/projects/<pid>")
def api_project(pid: str):
    p = _project(pid)
    if not p:
        return jsonify({"message": "404 Project Not Found"}), 404
    return jsonify(
        {
            "id": p.id,
            "path_with_namespace": p.path_with_namespace,
            "http_url_to_repo": p.http_url,
            "web_url": p.web_url,
        }
    )


@app.get("/api/v4/projects/<pid>/merge_requests/<int:iid>")
def api_mr(pid: str, iid: int):
    p = _project(pid)
    if not p or iid not in p.mrs:
        return jsonify({"message": "404 Not found"}), 404
    mr = p.mrs[iid]
    return jsonify(
        {
            "id": iid,
            "iid": mr.iid,
            "title": mr.title,
            "description": mr.description,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "web_url": mr.web_url,
            "state": "opened",
        }
    )


@app.get("/api/v4/projects/<pid>/merge_requests/<int:iid>/notes")
def api_list_notes(pid: str, iid: int):
    p = _project(pid)
    if not p or iid not in p.mrs:
        return jsonify({"message": "404 Not found"}), 404
    return jsonify([n.to_api() for n in p.mrs[iid].notes])


def _add_note(project: Project, mr: MergeRequest, body: str, author: str) -> Note:
    note = Note(
        id=mr.next_note_id,
        body=body,
        author=author,
        created_at=_now(),
        discussion_id=f"disc-{uuid.uuid4().hex[:8]}",
    )
    mr.next_note_id += 1
    mr.notes.append(note)
    return note


@app.post("/api/v4/projects/<pid>/merge_requests/<int:iid>/notes")
def api_post_note(pid: str, iid: int):
    p = _project(pid)
    if not p or iid not in p.mrs:
        return jsonify({"message": "404 Not found"}), 404
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"message": "body is required"}), 400
    with _lock:
        note = _add_note(p, p.mrs[iid], body, "virtual-developer")
    return jsonify(note.to_api()), 201


def _note_hook_payload(project: Project, mr: MergeRequest, note: Note, username: str) -> dict:
    return {
        "object_kind": "note",
        "event_type": "note",
        "user": {"username": username, "name": username},
        "project": {
            "id": project.id,
            "path_with_namespace": project.path_with_namespace,
            "http_url_to_repo": project.http_url,
            "web_url": project.web_url,
        },
        "object_attributes": {
            "id": note.id,
            "note": note.body,
            "noteable_type": "MergeRequest",
            "project_id": project.id,
            "discussion_id": note.discussion_id,
            "url": f"{mr.web_url}#note_{note.id}",
        },
        "merge_request": {
            "id": mr.iid,
            "iid": mr.iid,
            "title": mr.title,
            "description": mr.description,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "web_url": mr.web_url,
            "state": "opened",
        },
        "repository": {
            "name": project.path_with_namespace.split("/")[-1],
            "url": project.http_url,
            "homepage": project.web_url,
        },
    }


def fire_note_webhook(payload: dict) -> Dict[str, Any]:
    headers = {
        "X-Gitlab-Event": "Note Hook",
        "Content-Type": "application/json",
    }
    if WEBHOOK_SECRET:
        headers["X-Gitlab-Token"] = WEBHOOK_SECRET
    try:
        with httpx.Client(timeout=15.0, verify=False) as client:
            resp = client.post(DAEMON_WEBHOOK, json=payload, headers=headers)
            return {
                "status_code": resp.status_code,
                "body": resp.text[:2000],
            }
    except Exception as e:
        return {"status_code": 0, "error": str(e)}


@app.post("/simulate/comment")
def simulate_comment():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form or {}
    try:
        project_id = int(data.get("project_id") or 1)
        mr_iid = int(data.get("mr_iid") or 1)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid project_id/mr_iid"}), 400
    body = (data.get("body") or "").strip()
    username = (data.get("username") or "alice").strip() or "alice"
    if not body:
        return jsonify({"ok": False, "error": "body required"}), 400
    with _lock:
        p = _projects.get(project_id)
        if not p or mr_iid not in p.mrs:
            return jsonify({"ok": False, "error": "unknown project/MR"}), 404
        note = _add_note(p, p.mrs[mr_iid], body, username)
        hook = _note_hook_payload(p, p.mrs[mr_iid], note, username)
    delivered = fire_note_webhook(hook)
    result = {
        "ok": True,
        "note": note.to_api(),
        "webhook": delivered,
        "webhook_url": DAEMON_WEBHOOK,
    }
    if request.is_json:
        return jsonify(result)
    return (
        f"<pre>{json.dumps(result, indent=2)}</pre>"
        f'<p><a href="/">Back</a> · <a href="/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes">Notes</a></p>',
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.post("/simulate/reset")
def simulate_reset():
    reset_demo_data()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"Simulated GitLab on http://127.0.0.1:{SIM_PORT}/")
    print(f"Webhook target: {DAEMON_WEBHOOK}")
    app.run(host="0.0.0.0", port=SIM_PORT, debug=False)
