"""Second-pass proofs. Assert correct operator-facing behaviour.

No unittest.mock. Failures mean the production path is still wrong.
Ignore this file in the green suite until the matching bugs are fixed
(same role as tests/test_logical_issues.py).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src.gitlab.client import GitlabClient
from src.gitlab.webhook import GitlabMrNoteEvent
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


async def _hold_dispatch() -> int:
    return 0


def _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch) -> JobProcessor:
    from src.config import settings

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.dispatch_queue = _hold_dispatch
    return proc


def test_reap_does_not_kill_pending_accept(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """A claim that just forced PENDING is live work, not a leftover after Stop."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12)", "from mr")
    sm.update_state("KAN-12", status=TaskStatus.PENDING, completed_at=None)
    rec = proc.queue_store.enqueue(
        source="gitlab",
        issue_key="KAN-12",
        summary="followup",
    )
    proc.queue_store.update(rec["queue_id"], status="running")
    n = proc._reap_stale_queue_running()
    live = proc.queue_store.get(rec["queue_id"])
    assert n == 0
    assert live["status"] == "running"


@pytest.mark.asyncio
async def test_plan_ready_skip_does_not_burn_the_comment(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """After plan_execute, the same MR /yaver must still be runnable."""
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "plan", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY)
    ev = GitlabMrNoteEvent(
        issue_key="KAN-12",
        note_id="88",
        note_body="@berat_ai /yaver implement now",
        prompt="implement now",
        author_username="alice",
        author_name="Alice",
        project_id=1,
        project_path="acme/demo",
        repository_url="https://gitlab.example.com/acme/demo.git",
        host="gitlab.example.com",
        mr_iid=4,
        mr_title="feat(KAN-12): work",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        discussion_id="disc-88",
    )
    first = await proc.enqueue_gitlab_note(ev)
    assert first["ok"] is True
    qid = first["queue_id"]
    proc.queue_store.update(qid, status="running")
    ran = await proc._run_gitlab_mr_comment(ev)
    assert ran is False
    assert sm.get_state("KAN-12").status == TaskStatus.PLAN_READY
    if proc.queue_store.get(qid)["status"] == "running":
        proc.queue_store.finish(
            qid,
            status="skipped",
            error_message="plan_ready; waiting for plan_execute",
        )
    sm.update_state("KAN-12", force=True, status=TaskStatus.PENDING)
    second = await proc.enqueue_gitlab_note(ev)
    assert second.get("duplicate") is not True, second


def test_claim_next_does_not_starve_behind_blocked_head(tmp_path):
    """A free repo behind many blocked rows must still be claimed."""
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    blocked = store.enqueue(
        source="gitlab",
        issue_key="KAN-1",
        summary="live lock",
        lock_key="lock_busy",
    )
    store.update(blocked["queue_id"], status="running")
    for i in range(300):
        store.enqueue(
            source="gitlab",
            issue_key="KAN-1",
            summary=f"wait-{i}",
            lock_key="lock_busy",
        )
    free = store.enqueue(
        source="gitlab",
        issue_key="KAN-FREE",
        summary="other repo",
        lock_key="lock_free",
    )
    claimed = store.claim_next(max_running=6)
    assert claimed is not None
    assert claimed["queue_id"] == free["queue_id"]


def test_storage_index_does_not_inherit_stale_mr_url(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """A later job on the same folder without an MR URL is not the old MR."""
    from src.config import settings
    from src.dashboard.temp_storage import _build_clone_issue_index, reset_size_cache
    from src.state.job_store import job_store

    reset_size_cache()
    base = tmp_path / "t"
    clone = base / "same_folder"
    clone.mkdir(parents=True)
    monkeypatch.setattr(settings, "temp_dir_base", base)
    old = job_store.create_job(issue_key="KAN-12", summary="first build")
    job_store.update_job(
        old["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        gitlab_project="acme/demo",
        gitlab_mr_iid=4,
        started_at="2026-01-01T00:00:00",
    )
    new = job_store.create_job(issue_key="KAN-12", summary="plan again")
    job_store.update_job(
        new["job_id"],
        working_directory=str(clone.resolve()),
        merge_request_url=None,
        started_at="2026-01-02T00:00:00",
    )
    index = _build_clone_issue_index()
    rec = None
    for row in index.values():
        if str(row.get("job_id") or "") == new["job_id"]:
            rec = row
            break
        if str(row.get("issue_key") or "").upper() == "KAN-12":
            rec = row
    assert rec is not None
    assert not rec.get("merge_request_url"), rec


class _GitlabDiscussionsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        page = int((qs.get("page") or ["1"])[0])
        if "/discussions" not in parsed.path:
            self.send_response(404)
            self.end_headers()
            return
        # Note 99 lives only on page 11. Client must not stop at 10.
        rows = []
        if page == 11:
            rows = [
                {
                    "id": "disc-late",
                    "notes": [{"id": 99, "body": "@bot /yaver"}],
                }
            ]
        body = json.dumps(rows).encode("utf-8")
        nxt = "" if page >= 11 else str(page + 1)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if nxt:
            self.send_header("X-Next-Page", nxt)
        self.end_headers()
        self.wfile.write(body)


def test_gitlab_discussion_lookup_does_not_stop_at_page_10():
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), _GitlabDiscussionsHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                s = socket.create_connection(httpd.server_address, timeout=0.2)
                s.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("gitlab discussion test server did not start")
        host, port = httpd.server_address
        client = GitlabClient(host=f"{host}:{port}", pat="glpat-test")
        client.api_base = f"http://{host}:{port}/api/v4"
        found = client.find_discussion_id_for_note(
            project="acme/demo", mr_iid=4, note_id="99"
        )
        assert found == "disc-late"
    finally:
        httpd.shutdown()
