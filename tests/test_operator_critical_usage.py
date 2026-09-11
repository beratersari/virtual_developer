"""Prove critical operator-facing bugs with real git, real HTTP, real stores.

No unittest.mock / MagicMock / patch of production methods.
Settings are isolated via monkeypatch so this machine's .env is not used.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from src.azure.identity import reset_identity_cache
from src.azure.webhook import decide_azure_comment_webhook
from src.config import settings
from src.git_manager import GitCloneError, GitManager
from src.gitlab.client import GitlabClient
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.state.manager import JiraStateManager
from src.state.queue_store import WorkQueueStore


BOT_GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


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


def _serve(handler_cls, *, daemon: bool = True) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=daemon)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    return httpd


# ---------------------------------------------------------------------------
# 1) HTTP TFS remotes must stay HTTP (default Azure DevOps Server :8080)
# ---------------------------------------------------------------------------


def test_http_tfs_remote_is_not_rewritten_to_https(tmp_path, monkeypatch):
    """Stock TFS is http://host:8080/tfs/…/_git/…. insteadOf must keep http.

    Rewriting to https://host:8080 speaks TLS to IIS HTTP. Clone never works.
    """
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    monkeypatch.setattr(
        settings, "azure_host_pats", '{"tfs.corp.local:8080":"tfs-pat"}'
    )

    url = "http://tfs.corp.local:8080/tfs/DefaultCollection/Proj/_git/App"
    git = GitManager(
        issue_key=None,
        remote_url=url,
        source_branch="develop",
        target_branch="develop",
    )
    env = git._apply_pat_to_git_env(url=url)

    pairs = []
    count = int(env.get("GIT_CONFIG_COUNT") or "0")
    for i in range(count):
        pairs.append(
            (
                env.get(f"GIT_CONFIG_KEY_{i}") or "",
                env.get(f"GIT_CONFIG_VALUE_{i}") or "",
            )
        )
    instead = [
        (key, value)
        for key, value in pairs
        if key.startswith("url.") and key.endswith(".insteadOf")
    ]
    http_src = [value for key, value in instead if value.startswith("http://")]
    assert http_src, f"no http:// insteadOf sources: {instead}"
    for key, value in instead:
        if value.startswith("http://"):
            dest = key[len("url.") : -len(".insteadOf")]
            assert dest.startswith("http://"), (
                f"HTTP TFS URL is rewritten to TLS on the same port: "
                f"{value!r} → {dest!r}. "
                "git then speaks HTTPS to IIS :8080 and the clone fails."
            )


def test_http_tfs_clone_reaches_a_real_http_git_server(tmp_path, monkeypatch):
    """End-to-end: clone http://127.0.0.1:<port>/tfs/…/_git/app.git with a PAT."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "develop"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)

    http_root = tmp_path / "http"
    bare = http_root / "tfs" / "DefaultCollection" / "Proj" / "_git" / "app.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "--git-dir", str(bare), "update-server-info"],
        check=True,
        capture_output=True,
    )

    class _DumbGit(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            rel = self.path.split("?", 1)[0].lstrip("/").replace("\\", "/")
            path = (http_root / rel).resolve()
            try:
                path.relative_to(http_root.resolve())
            except ValueError:
                self.send_error(404)
                return
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = _serve(_DumbGit)
    try:
        host, port = httpd.server_address
        remote = f"http://{host}:{port}/tfs/DefaultCollection/Proj/_git/app.git"
        monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "clones")
        monkeypatch.setattr(settings, "git_clone_timeout_seconds", 20)
        monkeypatch.setattr(
            settings, "azure_host_pats", json.dumps({f"{host}:{port}": "tfs-pat"})
        )
        monkeypatch.setattr(settings, "gitlab_pat", "")
        monkeypatch.setattr(settings, "gitlab_host_pats", "")

        try:
            git = GitManager(
                issue_key="KAN-HTTP",
                remote_url=remote,
                source_branch="develop",
                target_branch="develop",
            )
        except GitCloneError as exc:
            pytest.fail(
                "HTTP TFS clone failed. Production insteadOf likely rewrote "
                f"http://{host}:{port}/ to https:// on the same port.\n{exc}"
            )
        assert git.temp_dir is not None
        assert (Path(git.temp_dir) / "README.md").is_file()
        assert (Path(git.temp_dir) / "README.md").read_text(encoding="utf-8") == "hello\n"
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 2) Leftover GITLAB_PAT must authenticate MR replies
# ---------------------------------------------------------------------------


def test_leftover_gitlab_pat_authenticates_mr_reply(monkeypatch):
    """Operators who only set GITLAB_PAT still get a thread reply after /yaver."""
    seen: dict = {"token": None, "path": ""}

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            seen["token"] = self.headers.get("PRIVATE-TOKEN")
            seen["path"] = self.path
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            if not seen["token"]:
                body = b'{"message":"401 Unauthorized"}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps({"id": 9, "body": "ok", "type": "DiscussionNote"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = _serve(_Gitlab)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover")

        client = GitlabClient(host=f"{host}:{port}")
        posted = client.post_mr_note(
            project="acme/demo",
            mr_iid=4,
            body="*Yaver*\n\ndone",
        )
        assert posted is not None, (
            "MR reply was dropped. GitlabClient did not send leftover "
            f"GITLAB_PAT (PRIVATE-TOKEN={seen['token']!r} path={seen['path']!r}). "
            "Clone/push still work; the operator never sees a thread reply."
        )
        assert seen["token"] == "glpat-leftover"
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 3) TFS @<GUID> /yaver is a mention, not @name /yaver (intentional)
# ---------------------------------------------------------------------------


def test_azure_guid_only_mention_is_usage_not_a_job(monkeypatch):
    """@<VSID> /yaver without a configured @name is a usage note, not a job."""
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
            path = urlparse(self.path).path
            user = {
                "id": BOT_GUID,
                "displayName": "Yaver",
                "uniqueName": "CORP\\yaver",
                "providerDisplayName": "Yaver",
            }
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
                    "id": 77,
                    "threadId": 8,
                    "author": {
                        "displayName": "Alice",
                        "uniqueName": "CORP\\alice",
                        "id": "user-1",
                    },
                    "content": f"@<{BOT_GUID}> /yaver fix the failing tests",
                },
                "pullRequest": {
                    "pullRequestId": 4,
                    "title": "feat(KAN-12): login",
                    "description": "",
                    "sourceRefName": "refs/heads/feature/login",
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


# ---------------------------------------------------------------------------
# 4) Parallel active sprints — first sprint only (intentional)
# ---------------------------------------------------------------------------


def test_poller_uses_first_active_sprint_only(tmp_path, monkeypatch):
    """Scrum intake is values[0]. Tickets only on later parallel sprints stay off."""
    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    monkeypatch.setattr(settings, "jira_board_id", "1")

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
            parsed = urlparse(self.path)
            path = parsed.path
            if path.endswith("/board/1/sprint"):
                self._json(
                    {
                        "values": [
                            {"id": 11, "name": "Team A", "state": "active"},
                            {"id": 22, "name": "Team B", "state": "active"},
                        ],
                        "isLast": True,
                    }
                )
                return
            if "/sprint/11/issue" in path:
                self._json({"issues": [], "total": 0})
                return
            if "/sprint/22/issue" in path:
                self._json(
                    {
                        "total": 1,
                        "issues": [
                            {
                                "key": "KAN-99",
                                "fields": {
                                    "summary": "second sprint work",
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
            if path.endswith("/issue/KAN-99"):
                self._json(
                    {
                        "key": "KAN-99",
                        "fields": {
                            "summary": "second sprint work",
                            "description": "body",
                            "labels": [],
                            "assignee": {"displayName": "DevBot", "name": "devbot"},
                            "status": {
                                "name": "To Do",
                                "statusCategory": {"key": "new"},
                            },
                        },
                    }
                )
                return
            self._json({"error": path}, status=404)

    httpd = _serve(_Jira)
    try:
        host, port = httpd.server_address
        client = JiraClient(
            host=f"http://{host}:{port}",
            api_token="token",
        )
        sm = JiraStateManager(state_dir=tmp_path / "state")
        poller = JiraPoller(
            client=client,
            interval_seconds=1,
            board_id="1",
            state_manager=sm,
        )
        found = poller.poll_board()
        keys = [i.get("key") for i in found]
        assert "KAN-99" not in keys
        assert keys == []
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 5) Queue: a free repo must not starve behind 300 blocked comments
# ---------------------------------------------------------------------------


def test_claim_next_does_not_starve_behind_a_long_blocked_backlog(tmp_path):
    """Many /yaver comments on one busy MR must not hide other repos."""
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
    time.sleep(0.02)
    free = store.enqueue(
        source="gitlab",
        issue_key="KAN-FREE",
        summary="other repo",
        lock_key="lock_free",
    )
    claimed = store.claim_next(max_running=6)
    assert claimed is not None
    assert claimed["queue_id"] == free["queue_id"]


# ---------------------------------------------------------------------------
# 6) Dashboard model save must survive a later Settings save + restart
# ---------------------------------------------------------------------------


def test_dashboard_model_survives_later_env_save_and_restart(tmp_path, monkeypatch):
    """Save model, then save timeout (rewrites .env), then reload as a new process."""
    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".env").write_text(
        "DEFAULT_MODEL=GLM\nAGENT_BACKEND=opencode\nMAX_CONCURRENT_JOBS=3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEFAULT_MODEL", "GLM")
    monkeypatch.setenv("AGENT_BACKEND", "opencode")
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)

    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update
    from src.config import apply_runtime_settings_to

    apply_settings_update(SettingsUpdate(default_model="opencode/hy3-free"))
    time.sleep(0.05)
    apply_settings_update(SettingsUpdate(agent_task_timeout_seconds=7200))

    # New process: Settings() loads .env (GLM), then runtime overrides apply.
    from src.config import Settings as SettingsCls

    fresh = SettingsCls(_env_file=work / ".env")
    apply_runtime_settings_to(fresh)
    assert fresh.default_model == "opencode/hy3-free", (
        f"After restart, default_model={fresh.default_model!r} "
        "(dashboard Save was overwritten by a later .env rewrite). "
        "Operators change the model in Settings, restart the daemon, "
        "and the next job still uses GLM from .env."
    )
