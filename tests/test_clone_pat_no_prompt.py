"""Clone with a settings PAT must not wait on a credential prompt.

Proves the GitLab MR-comment path: HTTPS Basic + insteadOf + GCM disabled,
then a real ``git clone`` against a local git-http-backend that requires
``oauth2:<PAT>``.
"""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from src.git_manager import GitManager


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _start_auth_git_http(repo_parent: Path, repo_name: str, pat: str):
    """Serve ``repo_parent/repo_name`` via git-http-backend + Basic oauth2:PAT."""
    backend = shutil.which("git-http-backend")
    candidates = []
    if backend:
        candidates.append(Path(backend))
    try:
        exec_path = subprocess.check_output(
            ["git", "--exec-path"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if exec_path:
            candidates.append(Path(exec_path) / "git-http-backend")
    except (OSError, subprocess.CalledProcessError):
        pass
    git_exec = shutil.which("git")
    if git_exec:
        root = Path(git_exec).resolve().parent.parent
        candidates.append(root / "libexec" / "git-core" / "git-http-backend")
        candidates.append(root / "lib" / "git-core" / "git-http-backend")
    candidates.append(Path("/usr/lib/git-core/git-http-backend"))
    backend = next((str(p) for p in candidates if p.is_file()), "")
    if not backend:
        pytest.skip("git-http-backend not installed")

    expected = "Basic " + base64.b64encode(f"oauth2:{pat}".encode("ascii")).decode("ascii")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            return

        def _proxy(self) -> None:
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="git"')
                self.end_headers()
                return
            env = os.environ.copy()
            env["GIT_PROJECT_ROOT"] = str(repo_parent)
            env["GIT_HTTP_EXPORT_ALL"] = "1"
            env["PATH_INFO"] = self.path.split("?", 1)[0]
            env["QUERY_STRING"] = self.path.split("?", 1)[1] if "?" in self.path else ""
            env["REQUEST_METHOD"] = self.command
            env["CONTENT_TYPE"] = self.headers.get("Content-Type", "")
            length = self.headers.get("Content-Length") or "0"
            env["CONTENT_LENGTH"] = length
            body = self.rfile.read(int(length)) if int(length) else b""
            proc = subprocess.run(
                [backend],
                input=body,
                capture_output=True,
                env=env,
            )
            # git-http-backend prints CGI headers then body
            raw = proc.stdout or b""
            if b"\r\n\r\n" in raw:
                head, payload = raw.split(b"\r\n\r\n", 1)
            elif b"\n\n" in raw:
                head, payload = raw.split(b"\n\n", 1)
            else:
                head, payload = b"Status: 500", raw
            status = 200
            headers: list[tuple[str, str]] = []
            for line in head.decode("latin-1", errors="replace").splitlines():
                if line.lower().startswith("status:"):
                    try:
                        status = int(line.split(":", 1)[1].strip().split(" ", 1)[0])
                    except ValueError:
                        status = 200
                elif ":" in line:
                    k, v = line.split(":", 1)
                    headers.append((k.strip(), v.strip()))
            self.send_response(status)
            for k, v in headers:
                if k.lower() != "status":
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self._proxy()

        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/{repo_name}"


def test_apply_pat_env_kills_windows_prompt(tmp_path, monkeypatch):
    from src.config import settings as real_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_settings, "gitlab_pat", "test-pat-xyz")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map(
            {"gitlab.example.com": "test-pat-xyz"}
        )
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="CLN-1")
    gm.remote_url = "git@gitlab.example.com:group/repo.git"
    env = gm._apply_pat_to_git_env(url=gm.remote_url)
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GCM_MODAL_PROMPT"] == "false"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["VD_GIT_PASSWORD"] == "test-pat-xyz"
    assert env.get("GIT_ASKPASS")
    values = [env[k] for k in env if str(k).startswith("GIT_CONFIG_VALUE_")]
    keys = [env[k] for k in env if str(k).startswith("GIT_CONFIG_KEY_")]
    assert any(k.endswith("insteadOf") and "git@gitlab.example.com:" in v for k, v in zip(keys, values))
    assert any(
        k == "http.extraHeader" and v.startswith("Authorization: Basic ")
        for k, v in zip(keys, values)
    )


def test_clone_with_pat_against_basic_auth_http(tmp_path, monkeypatch):
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "develop"], cwd=src, check=False, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README.md").write_text("hello from pat clone\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)

    parent = tmp_path / "published"
    parent.mkdir()
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(parent / "demo.git")],
        check=True,
        capture_output=True,
    )
    # export-all is set in the CGI env; also mark the bare repo exportable
    (parent / "demo.git" / "git-daemon-export-ok").write_text("", encoding="utf-8")

    pat = "clone-me-pat"
    server, url = _start_auth_git_http(parent, "demo.git", pat)
    try:
        from src.config import settings as real_settings

        host = GitManager._host_from_url(url)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(real_settings, "gitlab_pat", pat)
        monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", host)
        if hasattr(real_settings, "set_gitlab_host_pat_map"):
            real_settings.set_gitlab_host_pat_map({host: pat})
        monkeypatch.setattr(real_settings, "git_update_submodules", False)

        dest = tmp_path / "cloned"
        with patch.object(GitManager, "_setup_temp_working_dir"):
            gm = GitManager(issue_key="CLN-HTTP")
        gm.remote_url = url
        gm.temp_dir = dest
        gm._clone_into_temp()
        assert (dest / "README.md").read_text(encoding="utf-8") == "hello from pat clone\n"
    finally:
        server.shutdown()


def test_clone_without_pat_gets_401_not_a_hang(tmp_path, monkeypatch):
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "develop"], cwd=src, check=False, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)
    parent = tmp_path / "published"
    parent.mkdir()
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(parent / "demo.git")],
        check=True,
        capture_output=True,
    )
    (parent / "demo.git" / "git-daemon-export-ok").write_text("", encoding="utf-8")
    server, url = _start_auth_git_http(parent, "demo.git", "real-pat")
    try:
        from src.config import settings as real_settings
        from src.git_manager import GitCloneError

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(real_settings, "gitlab_pat", "")
        monkeypatch.setattr(real_settings, "gitlab_host_pats", "")
        monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "")
        if hasattr(real_settings, "set_gitlab_host_pat_map"):
            real_settings.set_gitlab_host_pat_map({})
        monkeypatch.setattr(real_settings, "git_update_submodules", False)
        dest = tmp_path / "cloned"
        with patch.object(GitManager, "_setup_temp_working_dir"):
            gm = GitManager(issue_key="CLN-401")
        gm.remote_url = url
        gm.temp_dir = dest
        with pytest.raises(GitCloneError):
            gm._clone_into_temp()
    finally:
        server.shutdown()
