"""Real-process / real-store proofs for day-to-day critical bugs.

No unittest.mock / MagicMock / patch of production methods.
Settings are isolated via monkeypatch so this machine's .env is not used.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from src.config import settings
from src.git_manager import GitCloneError, GitManager
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


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


def _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch) -> JobProcessor:
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    return proc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# 1) Dashboard Stop on macOS must kill tools whose argv names the clone
# ---------------------------------------------------------------------------


def test_reclaim_workspace_kills_real_child_named_in_argv(tmp_path):
    """pgrep finds the tool; /proc is missing on macOS. The child must still die."""
    from src.process_kill import reclaim_workspace

    clone = tmp_path / "repo_clone_workspace"
    clone.mkdir()
    marker = clone / "keep_alive"
    # argv contains the clone path so ``pgrep -f`` matches (same as npm/git
    # started inside an OpenCode tool with the workspace on the command line).
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep(60)  # {clone}"],
        cwd=clone,
    )
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not _pid_alive(proc.pid):
            time.sleep(0.05)
        assert _pid_alive(proc.pid)
        n = reclaim_workspace(clone, force=True)
        assert n >= 1, f"reclaim killed nothing (pid={proc.pid})"
        proc.wait(timeout=3)
        assert not _pid_alive(proc.pid), (
            f"tool pid {proc.pid} still alive after reclaim_workspace; "
            "Stop during a live job leaves writers in the clone"
        )
        marker.write_text("ok\n", encoding="utf-8")
    finally:
        if _pid_alive(proc.pid):
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# 2) Cancel must not SIGKILL the shared opencode serve process group
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="process groups are a Unix cancel path")
def test_kill_pid_does_not_take_down_session_parent():
    """A tool that shares serve's session must not kill serve via killpg(getpgid)."""
    from src.process_kill import kill_pid

    script = (
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "    sys.exit(0)\n"
        "print(child, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        raw = parent.stdout.readline() if parent.stdout is not None else b""
        child_pid = int(raw.decode("ascii").strip())
        assert child_pid > 0
        assert os.getpgid(child_pid) == parent.pid
        assert os.getpgid(child_pid) != child_pid
        kill_pid(child_pid, force=True)
        time.sleep(0.2)
        assert _pid_alive(parent.pid), (
            f"kill_pid({child_pid}) killed session leader {parent.pid}; "
            "Cancel would take down shared opencode serve and every other job"
        )
        child_state = ""
        try:
            child_state = subprocess.check_output(
                ["ps", "-o", "state=", "-p", str(child_pid)],
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            child_state = ""
        assert (not _pid_alive(child_pid)) or child_state[:1] in {"Z", ""}, (
            f"tool pid {child_pid} still running after kill_pid "
            f"(state={child_state!r})"
        )
    finally:
        if _pid_alive(parent.pid):
            try:
                os.killpg(parent.pid, signal.SIGKILL)
            except OSError:
                parent.kill()
        parent.wait(timeout=3)


# ---------------------------------------------------------------------------
# 3) Leftover GITLAB_PAT still authenticates GitLab when Azure hosts exist
# ---------------------------------------------------------------------------


def test_leftover_gitlab_pat_survives_azure_host_map(tmp_path, monkeypatch):
    """Mixed shop: AZURE_HOST_PATS set, GITLAB_HOST_PATS empty, GITLAB_PAT set."""
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover-mixed")
    monkeypatch.setattr(
        settings, "azure_host_pats", '{"tfs.corp.local:8080":"tfs-pat"}'
    )

    url = "https://gitlab.example.com/acme/app.git"
    git = GitManager(
        issue_key=None,
        remote_url=url,
        source_branch="develop",
        target_branch="develop",
    )
    assert git._pat_for_remote(url) == "glpat-leftover-mixed"
    git._assert_remote_host_allowed(url)


def test_leftover_gitlab_pat_clones_http_git_while_azure_map_set(tmp_path, monkeypatch):
    """End-to-end: leftover GITLAB_PAT clones a real HTTP git repo."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-b", "develop"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=src, check=True, capture_output=True)
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=src, check=True, capture_output=True)

    http_root = tmp_path / "http"
    bare = http_root / "acme" / "app.git"
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

    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), _DumbGit)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    try:
        monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover-mixed")
        monkeypatch.setattr(
            settings, "azure_host_pats", '{"tfs.corp.local:8080":"tfs-pat"}'
        )
        url = f"http://{host}:{port}/acme/app.git"
        git = GitManager(
            issue_key="KAN-12",
            remote_url=url,
            source_branch="develop",
            target_branch="develop",
        )
        wd = git.get_working_directory()
        assert wd is not None
        assert (Path(wd) / "README.md").read_text(encoding="utf-8") == "hello\n"
    except GitCloneError as e:
        pytest.fail(f"leftover GITLAB_PAT was refused while Azure map set: {e}")
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 4) Stop during clone must stay CANCELLED (not ERROR "workspace not prepared")
# ---------------------------------------------------------------------------


def test_finish_after_git_missing_does_not_overwrite_cancel(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12)", "clone")
    sm.update_state("KAN-12", status=TaskStatus.EXECUTING)
    proc._cancelling.add("KAN-12")
    proc._cancel_issue_state(
        "KAN-12", message="Cancelled from ops dashboard", status=TaskStatus.CANCELLED
    )
    proc._finish_after_git_missing("KAN-12")
    st = sm.get_state("KAN-12")
    assert st is not None
    assert st.status == TaskStatus.CANCELLED, (
        f"Stop during clone became {st.status.value}: {st.error_message!r}"
    )


@pytest.mark.asyncio
async def test_cancel_job_keeps_cancelled_when_git_prep_returns_none(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "feat(KAN-12)", "clone")
    sm.update_state("KAN-12", status=TaskStatus.PLANNING)
    out = await proc.cancel_job("KAN-12", reason="Cancelled from ops dashboard")
    assert out.get("ok") is True
    proc._finish_after_git_missing("KAN-12")
    st = sm.get_state("KAN-12")
    assert st is not None
    assert st.status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 5) Stop on plan_ready must not discard the implement handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_refuses_plan_ready(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    proc = _processor(tmp_path, isolate_jira_agent_artifacts, monkeypatch)
    sm = proc.state_manager
    sm.create_state("KAN-12", "plan login", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY)
    out = await proc.cancel_job("KAN-12", reason="Cancelled from ops dashboard")
    assert out.get("ok") is False
    assert sm.get_state("KAN-12").status == TaskStatus.PLAN_READY


def test_issue_detail_hides_stop_on_plan_ready(tmp_path, monkeypatch):
    from src.dashboard.service import build_task_detail

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-12", "plan login", "Mode: plan")
    sm.update_state("KAN-12", status=TaskStatus.PLAN_READY)
    detail = build_task_detail("KAN-12", state_manager=sm, processor=None)
    assert detail is not None
    assert detail["can_cancel"] is False
    assert detail["status"] == "plan_ready"
