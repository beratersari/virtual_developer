"""Real HTTP / queue proofs for intake design and the queue scan window.

No unittest.mock, MagicMock, FakeJira, or production-method patches.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from src.azure.identity import reset_identity_cache
from src.azure.webhook import decide_azure_comment_webhook
from src.config import settings
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.state.manager import JiraStateManager
from src.state.queue_store import WorkQueueStore

BOT_GUID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


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


def test_tfs_guid_only_mention_stays_a_usage_note(monkeypatch):
    """GUID-only ``@<VSID> /yaver`` is a mention (usage note), not a job.

    Identity HTTP resolves the GUID to Yaver. ``/yaver`` still requires a
    configured trigger name. Intentional.
    """
    reset_identity_cache()

    class _Tfs(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _json(self, payload: dict, status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            user = {
                "id": BOT_GUID,
                "displayName": "Yaver",
                "uniqueName": "CORP\\yaver",
                "providerDisplayName": "Yaver",
            }
            path = urlparse(self.path).path
            if path.endswith("/_apis/connectionData"):
                self._json({"authenticatedUser": user})
                return
            if "/_apis/identities" in path:
                self._json({"value": [user]})
                return
            self._json({"error": path}, status=404)

    httpd = _serve(_Tfs)
    try:
        host, port = httpd.server_address
        origin = f"http://{host}:{port}"
        if hasattr(settings, "set_azure_host_pat_map"):
            settings.set_azure_host_pat_map(
                {f"{host}:{port}": "tfs-pat", str(host): "tfs-pat"}
            )
        monkeypatch.setattr(settings, "azure_pat", "tfs-pat")
        payload = {
            "eventType": "ms.vss-code.git-pullrequest-comment-event",
            "resource": {
                "comment": {
                    "id": 91,
                    "threadId": 3,
                    "author": {
                        "displayName": "Dev",
                        "uniqueName": "CORP\\dev",
                        "id": "user-9",
                    },
                    "content": f"@<{BOT_GUID}> /yaver add a log line",
                },
                "pullRequest": {
                    "pullRequestId": 7,
                    "title": "feat(KAN-44): logs",
                    "description": "",
                    "sourceRefName": "refs/heads/feature/logs",
                    "targetRefName": "refs/heads/develop",
                    "repository": {
                        "id": "repo-guid",
                        "name": "App",
                        "remoteUrl": f"{origin}/tfs/DefaultCollection/Proj/_git/App",
                        "project": {"name": "Proj"},
                    },
                },
            },
            "resourceContainers": {
                "collection": {"baseUrl": f"{origin}/tfs/DefaultCollection/"}
            },
        }
        decision = decide_azure_comment_webhook(
            payload,
            enabled=True,
            secret="",
            bot_mentions=["yaver"],
            jira_project_keys=["KAN"],
        )
        assert decision.accepted is False
        assert decision.reason == "mention without /yaver"
        assert decision.usage_note is True
        assert decision.event is not None
    finally:
        httpd.shutdown()
        reset_identity_cache()


def test_poller_stays_on_first_active_sprint(tmp_path, monkeypatch):
    """Scrum: only the first active sprint. Sprint-3-only tickets stay off."""
    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    monkeypatch.setattr(settings, "jira_board_id", "9")

    class _Jira(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def _json(self, payload: dict, status: int = 200) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path.endswith("/board/9/sprint"):
                self._json(
                    {
                        "values": [
                            {"id": 1, "name": "A", "state": "active"},
                            {"id": 2, "name": "B", "state": "active"},
                            {"id": 3, "name": "C", "state": "active"},
                        ],
                        "isLast": True,
                    }
                )
                return
            if "/sprint/1/issue" in path or "/sprint/2/issue" in path:
                self._json({"issues": [], "total": 0})
                return
            if "/sprint/3/issue" in path:
                self._json(
                    {
                        "total": 1,
                        "issues": [
                            {
                                "key": "KAN-301",
                                "fields": {
                                    "summary": "only in sprint 3",
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
                    }
                )
                return
            self._json({"error": path}, status=404)

    httpd = _serve(_Jira)
    try:
        host, port = httpd.server_address
        poller = JiraPoller(
            client=JiraClient(host=f"http://{host}:{port}", api_token="token"),
            interval_seconds=1,
            board_id="9",
            state_manager=JiraStateManager(state_dir=tmp_path / "state"),
        )
        found = poller.poll_board()
        keys = [i.get("key") for i in found]
        assert "KAN-301" not in keys
        assert keys == []
    finally:
        httpd.shutdown()


def test_queue_claim_reaches_a_free_repo_behind_blocked_backlog(tmp_path):
    """A free workspace behind 400 blocked comments on one MR must still start."""
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    live = store.enqueue(
        source="gitlab",
        issue_key="KAN-BUSY",
        summary="running",
        lock_key="lock_busy",
    )
    store.update(live["queue_id"], status="running")
    for i in range(400):
        store.enqueue(
            source="gitlab",
            issue_key="KAN-BUSY",
            summary=f"blocked-{i}",
            lock_key="lock_busy",
        )
    time.sleep(0.02)
    free = store.enqueue(
        source="gitlab",
        issue_key="KAN-OTHER",
        summary="other repo",
        lock_key="lock_other",
    )
    claimed = store.claim_next(max_running=8)
    assert claimed is not None
    assert claimed["queue_id"] == free["queue_id"]
