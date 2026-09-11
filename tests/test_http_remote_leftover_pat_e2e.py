"""Real HTTP/git proofs for HTTP TFS clone and leftover GITLAB_PAT replies.

No unittest.mock. These belong in the green suite: a failure means the
fix for HTTP→HTTPS rewrite or leftover PAT replies has regressed.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import settings
from src.git_manager import GitCloneError, GitManager
from src.gitlab.client import GitlabClient
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager


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


def _instead_pairs(env: dict) -> list[tuple[str, str]]:
    count = int(env.get("GIT_CONFIG_COUNT") or "0")
    out = []
    for i in range(count):
        key = env.get(f"GIT_CONFIG_KEY_{i}") or ""
        value = env.get(f"GIT_CONFIG_VALUE_{i}") or ""
        if key.startswith("url.") and key.endswith(".insteadOf"):
            dest = key[len("url.") : -len(".insteadOf")]
            out.append((dest, value))
    return out


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "develop"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_http_tfs_insteadOf_keeps_http_https_ssh_stay_https(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    monkeypatch.setattr(
        settings, "azure_host_pats", '{"tfs.corp.local:8080":"tfs-pat"}'
    )

    git = GitManager(
        issue_key=None,
        remote_url="http://tfs.corp.local:8080/tfs/DefaultCollection/Proj/_git/App",
        source_branch="develop",
        target_branch="develop",
    )
    pairs = _instead_pairs(git._apply_pat_to_git_env(url=git.remote_url))
    http_dests = [dest for dest, src in pairs if src.startswith("http://")]
    https_dests = [
        dest
        for dest, src in pairs
        if src.startswith("https://") or src.startswith("git@") or src.startswith("ssh://")
    ]
    assert http_dests, pairs
    assert all(d.startswith("http://") for d in http_dests), http_dests
    assert https_dests, pairs
    assert all(d.startswith("https://") for d in https_dests), https_dests
    assert any("pat:tfs-pat@" in d for d, _src in pairs)


def test_https_gitlab_insteadOf_stays_https(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    monkeypatch.setattr(
        settings, "gitlab_host_pats", '{"gitlab.example.com":"glpat-x"}'
    )

    git = GitManager(
        issue_key=None,
        remote_url="https://gitlab.example.com/acme/demo.git",
        source_branch="develop",
        target_branch="develop",
    )
    pairs = _instead_pairs(git._apply_pat_to_git_env(url=git.remote_url))
    https_src = [dest for dest, src in pairs if src.startswith("https://gitlab.example.com/")]
    assert https_src
    assert all(d.startswith("https://oauth2:glpat-x@") for d in https_src), https_src


def test_http_tfs_clone_against_real_git_http(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _init_repo(src)
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

        git = GitManager(
            issue_key="KAN-HTTP",
            remote_url=remote,
            source_branch="develop",
            target_branch="develop",
        )
        assert git.temp_dir is not None
        readme = Path(git.temp_dir) / "README.md"
        assert readme.is_file()
        assert readme.read_text(encoding="utf-8") == "hello\n"
    except GitCloneError as exc:
        pytest.fail(f"HTTP TFS clone failed after scheme fix: {exc}")
    finally:
        httpd.shutdown()


def test_leftover_gitlab_pat_posts_mr_note_over_real_http(monkeypatch):
    seen: dict = {"token": None}

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            seen["token"] = self.headers.get("PRIVATE-TOKEN")
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            if not seen["token"]:
                body = b'{"message":"401 Unauthorized"}'
                self.send_response(401)
            else:
                body = json.dumps({"id": 9, "body": "ok"}).encode()
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
        assert posted is not None
        assert seen["token"] == "glpat-leftover"
    finally:
        httpd.shutdown()


def test_leftover_pat_not_used_when_host_map_exists(monkeypatch):
    """A host map must not leak leftover GITLAB_PAT to an unlisted host."""
    seen: dict = {"token": None}

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            seen["token"] = self.headers.get("PRIVATE-TOKEN")
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            body = b'{"message":"401 Unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = _serve(_Gitlab)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover")
        monkeypatch.setattr(
            settings, "gitlab_host_pats", '{"gitlab.example.com":"glpat-mapped"}'
        )

        client = GitlabClient(host=f"{host}:{port}")
        posted = client.post_mr_note(
            project="acme/demo",
            mr_iid=4,
            body="should not send leftover",
        )
        assert posted is None
        assert seen["token"] is None
    finally:
        httpd.shutdown()


def test_processor_mr_reply_uses_leftover_pat(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """The job-success path constructs GitlabClient(host=…) with no PAT arg."""
    seen: dict = {"token": None}

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            seen["token"] = self.headers.get("PRIVATE-TOKEN")
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            body = json.dumps({"id": 3, "body": "ok"}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = _serve(_Gitlab)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "jira_host", "")
        monkeypatch.setattr(settings, "jira_api_token", "")
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover")

        proc = JobProcessor()
        proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
        proc.reporter = JiraReporter()
        st = proc.state_manager.create_state("KAN-12", "feat(KAN-12)", "from mr")
        proc.state_manager.update_state(
            "KAN-12",
            metadata={
                "source": "gitlab",
                "gitlab_host": f"{host}:{port}",
                "gitlab_project": "acme/demo",
                "gitlab_mr_iid": 4,
                "gitlab_discussion_id": "disc-1",
            },
        )
        st = proc.state_manager.get_state("KAN-12")
        assert st is not None
        ok = proc._post_gitlab_mr_reply(st, "*Yaver*\n\nshipped")
        assert ok is True
        assert seen["token"] == "glpat-leftover"
    finally:
        httpd.shutdown()
