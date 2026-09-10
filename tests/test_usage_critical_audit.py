"""Regression tests for the seven critical usage bugs.

These drive real stores, real processor methods, and real HTTP clients.
No unittest.mock / MagicMock / patch objects.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest

from src.azure.client import AzureDevOpsClient
from src.azure.webhook import AzurePrCommentEvent, azure_comment_key
from src.gitlab.keys import gitlab_note_key
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


def _gl_note(
    *,
    note_id: str,
    project_path: str,
    project_id: int,
    mr_iid: int,
    issue_key: str,
    prompt: str = "fix tests",
    host: str = "gitlab.example.com",
) -> GitlabMrNoteEvent:
    repo = f"https://{host}/{project_path}.git"
    return GitlabMrNoteEvent(
        issue_key=issue_key,
        note_id=str(note_id),
        note_body=f"@berat_ai /yaver {prompt}",
        prompt=prompt,
        author_username="alice",
        author_name="Alice",
        project_id=project_id,
        project_path=project_path,
        repository_url=repo,
        host=host,
        mr_iid=mr_iid,
        mr_title=f"feat({issue_key}): work",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url=f"https://{host}/{project_path}/-/merge_requests/{mr_iid}",
        discussion_id=f"disc-{note_id}",
    )


def _az_ev(
    *,
    project: str,
    repo: str,
    pr_id: int,
    comment_id: str,
    body: str,
    thread_id: str = "",
    issue_key: str = "",
) -> AzurePrCommentEvent:
    path = f"{project}/{repo}"
    url = f"https://tfs.example.com/tfs/DefaultCollection/{project}/_git/{repo}"
    return AzurePrCommentEvent(
        issue_key=issue_key or f"AZ-{project.upper()}-{repo.upper()}-{pr_id}",
        comment_id=str(comment_id),
        comment_body=body,
        prompt=body.replace("@yaver /yaver", "").strip(),
        author_username="alice",
        author_name="Alice",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project=project,
        repository_id=repo,
        repository_name=repo,
        project_path=path,
        repository_url=url,
        host="tfs.example.com",
        pr_id=int(pr_id),
        pr_title=f"feat({issue_key or 'x'}): work",
        pr_description="",
        source_branch="feature/x",
        target_branch="develop",
        pr_url=f"{url}/pullrequest/{pr_id}",
        thread_id=thread_id,
    )


def _processor(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
) -> JobProcessor:
    from src.config import settings

    # Keep JobProcessor on the in-process simulated client — never the
    # operator's real Jira from .env.
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.dispatch_queue = _hold_dispatch
    return proc


def test_gitlab_note_key_is_per_project():
    a = gitlab_note_key(
        host="gitlab.example.com",
        project_path="acme/demo",
        project_id=1,
        note_id="77",
    )
    b = gitlab_note_key(
        host="gitlab.example.com",
        project_path="other/app",
        project_id=2,
        note_id="77",
    )
    assert a != b
    assert a.endswith(":77") and b.endswith(":77")


def test_azure_comment_key_is_per_repo():
    a = azure_comment_key(
        4,
        "8",
        "1",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/a",
    )
    b = azure_comment_key(
        4,
        "8",
        "1",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/Other/_git/b",
    )
    assert a != b
    assert azure_comment_key(4, "8", "1") == "4:8:1"


def test_find_note_does_not_cross_gitlab_projects(tmp_path):
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    first_key = gitlab_note_key(
        host="gitlab.example.com", project_path="acme/demo", note_id="77"
    )
    second_key = gitlab_note_key(
        host="gitlab.example.com", project_path="other/app", note_id="77"
    )
    store.enqueue(
        source="gitlab",
        issue_key="GL-ACME-DEMO-4",
        gitlab_note_id=first_key,
    )
    assert store.find_note(first_key) is not None
    assert store.find_note(second_key) is None
    assert store.find_note("77") is None


@pytest.mark.asyncio
async def test_gitlab_execute_on_second_project_same_note_id_enqueues(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    first = await proc.enqueue_gitlab_note(
        _gl_note(
            note_id="77",
            project_path="acme/demo",
            project_id=1,
            mr_iid=4,
            issue_key="GL-ACME-DEMO-4",
        )
    )
    second = await proc.enqueue_gitlab_note(
        _gl_note(
            note_id="77",
            project_path="other/app",
            project_id=2,
            mr_iid=9,
            issue_key="GL-OTHER-APP-9",
            prompt="other work",
        )
    )
    assert first["ok"] is True
    assert second.get("duplicate") is not True, second
    assert first["queue_id"] != second["queue_id"]
    assert proc.queue_store.get(second["queue_id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_azure_execute_on_second_repo_same_pr_comment_ids_enqueues(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    first = await proc.enqueue_azure_comment(
        _az_ev(
            project="Demo",
            repo="a",
            pr_id=4,
            comment_id="1",
            body="@yaver /yaver fix a",
            thread_id="8",
        )
    )
    second = await proc.enqueue_azure_comment(
        _az_ev(
            project="Other",
            repo="b",
            pr_id=4,
            comment_id="1",
            body="@yaver /yaver fix b",
            thread_id="8",
        )
    )
    assert first["ok"] is True
    assert second.get("duplicate") is not True, second
    assert first["queue_id"] != second["queue_id"]


@pytest.mark.asyncio
async def test_gitlab_execute_leaves_plan_ready_intact(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "plan login", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY)
    ran = await proc._run_gitlab_mr_comment(
        _gl_note(
            note_id="88",
            project_path="acme/demo",
            project_id=1,
            mr_iid=4,
            issue_key="KAN-12",
            prompt="implement now",
        )
    )
    st = sm.get_state("KAN-12")
    assert ran is False
    assert st is not None
    assert st.status == TaskStatus.PLAN_READY


def test_jira_rework_after_gitlab_comment_is_a_jira_job(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12): login", "from mr")
    sm.update_state(
        "KAN-12",
        status=TaskStatus.COMPLETED,
        metadata={
            "source": "gitlab",
            "workflow_type": "gitlab_mr",
            "gitlab_host": "gitlab.example.com",
            "gitlab_project": "acme/demo",
            "gitlab_mr_iid": 4,
            "merge_request_url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
        },
    )
    assert proc._is_gitlab_triggered("KAN-12") is True
    proc._reset_for_reprocess("KAN-12")
    sm.update_state("KAN-12", metadata={"workflow_type": "execution"})
    assert proc._is_gitlab_triggered("KAN-12") is False
    assert proc._is_git_comment_triggered("KAN-12") is False


@pytest.mark.asyncio
async def test_cancel_live_jira_keeps_queued_gitlab_followup(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12): login", "running")
    sm.update_state("KAN-12", status=TaskStatus.EXECUTING)
    proc._contexts["KAN-12"] = {"git": None, "runner": None}
    out = await proc.enqueue_gitlab_note(
        _gl_note(
            note_id="102",
            project_path="acme/demo",
            project_id=1,
            mr_iid=4,
            issue_key="KAN-12",
            prompt="also add tests",
        )
    )
    qid = out["queue_id"]
    assert proc.queue_store.get(qid)["status"] == "queued"
    await proc.cancel_job("KAN-12", reason="stop jira run")
    row = proc.queue_store.get(qid)
    assert row is not None
    assert row["status"] == "queued", row
    assert sm.get_state("KAN-12").status == TaskStatus.CANCELLED


def test_requeue_does_not_revive_cancelled_row(tmp_path):
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    rec = store.enqueue(source="gitlab", issue_key="KAN-12", summary="followup")
    store.update(rec["queue_id"], status="running")
    store.finish(rec["queue_id"], status="cancelled", error_message="dashboard stop")
    assert store.requeue(rec["queue_id"], reason="workspace still in-flight") is None
    assert store.get(rec["queue_id"])["status"] == "cancelled"


def test_finish_open_for_issue_keeps_queued_forge_rows(tmp_path):
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    jira = store.enqueue(source="jira", issue_key="KAN-12", summary="live")
    store.update(jira["queue_id"], status="running")
    follow = store.enqueue(source="gitlab", issue_key="KAN-12", summary="later")
    leftover = store.enqueue(source="jira", issue_key="KAN-12", summary="poller")
    n = store.finish_open_for_issue(
        "KAN-12",
        status="cancelled",
        include_queued=False,
        include_running=True,
    )
    n += store.finish_open_for_issue(
        "KAN-12",
        status="cancelled",
        sources={"jira"},
        include_queued=True,
        include_running=False,
    )
    assert n == 2
    assert store.get(jira["queue_id"])["status"] == "cancelled"
    assert store.get(leftover["queue_id"])["status"] == "cancelled"
    assert store.get(follow["queue_id"])["status"] == "queued"


class _AzureThreadsHandler(BaseHTTPRequestHandler):
    """In-process Azure DevOps threads API used by the reply lookup test."""

    posted: list

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.endswith("/pullrequests/4/threads"):
            body = json.dumps(
                {
                    "value": [
                        {
                            "id": 8,
                            "comments": [{"id": 1, "content": "old overview"}],
                        },
                        {
                            "id": 9,
                            "comments": [
                                {
                                    "id": 1,
                                    "content": "@yaver /yaver fix the tests",
                                }
                            ],
                        },
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        self.posted.append({"path": path, "payload": payload})
        body = json.dumps({"id": 3, "content": payload.get("content")}).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_azure_http() -> tuple[ThreadingHTTPServer, list]:
    posted: list = []

    class Handler(_AzureThreadsHandler):
        pass

    Handler.posted = posted
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            probe = socket.create_connection(httpd.server_address, timeout=0.2)
            probe.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        httpd.shutdown()
        raise RuntimeError("Azure test HTTP server did not start")
    return httpd, posted


def test_azure_reply_uses_original_comment_body(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    httpd, posted = _start_azure_http()
    try:
        host, port = httpd.server_address
        collection = f"http://{host}:{port}/tfs/DefaultCollection"
        client = AzureDevOpsClient(
            host=str(host),
            collection_url=collection,
            pat="azpat-test",
        )
        thread = client.find_thread_id_for_comment(
            project="Demo",
            repository="demo",
            pr_id=4,
            comment_id="1",
            comment_content="@yaver /yaver fix the tests",
        )
        assert thread == "9"
        assert (
            client.find_thread_id_for_comment(
                project="Demo",
                repository="demo",
                pr_id=4,
                comment_id="1",
                comment_content="fix the tests",
            )
            == ""
        )

        proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
        sm = proc.state_manager
        sm.create_state("KAN-12", "feat(KAN-12): x", "fix the tests")
        sm.update_state(
            "KAN-12",
            metadata={
                "source": "azure",
                "azure_host": str(host),
                "azure_collection_url": collection,
                "azure_project": "Demo",
                "azure_repository": "demo",
                "azure_repository_id": "demo",
                "azure_pr_id": 4,
                "azure_thread_id": "",
                "azure_comment_id": "1",
                "azure_comment_body": "@yaver /yaver fix the tests",
            },
        )
        ok = proc._post_azure_pr_reply(sm.get_state("KAN-12"), "*Yaver*\n\ndone")
        assert ok is True
        assert posted, "expected a thread reply POST"
        assert posted[-1]["path"].endswith("/threads/9/comments")
        assert "*Yaver*" in (posted[-1]["payload"].get("content") or "")
    finally:
        httpd.shutdown()
