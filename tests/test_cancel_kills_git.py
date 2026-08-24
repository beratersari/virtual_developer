"""Cancel must force-kill git/agent children and delete an in-progress clone."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import GitCancelledError, GitManager
from src.processor import JobProcessor
from src.state.models import TaskStatus


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


@pytest.fixture(autouse=True)
def _clear_live_git_managers():
    GitManager._live_by_issue.clear()
    yield
    GitManager._live_by_issue.clear()


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _hang_cmd(seconds: float = 30) -> list[str]:
    return [
        sys.executable,
        "-c",
        f"import time; time.sleep({seconds})",
    ]


def _native_dir() -> Path:
    """Linux-native temp dir — ``rmtree`` on /mnt/c (WSL 9p) can hang."""
    return Path(tempfile.mkdtemp(prefix="vd-cancel-clone-"))


def _bare_gm(tmp_path: Path | None = None, *, issue_key: str = "KILL-1") -> GitManager:
    gm = GitManager.__new__(GitManager)
    gm.issue_key = issue_key
    gm.temp_dir = (tmp_path / "clone") if tmp_path is not None else _native_dir()
    gm.temp_dir.mkdir(parents=True, exist_ok=True)
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    gm._init_proc_state()
    gm._register_live()
    return gm


def _cleanup_gm(gm: GitManager) -> None:
    path = gm.temp_dir
    gm._unregister_live()
    if path and path.exists():
        shutil.rmtree(path, ignore_errors=True)


def test_popen_wait_cancel_force_kills_child():
    gm = _bare_gm()
    started = threading.Event()
    raised: list[BaseException] = []

    def worker() -> None:
        started.set()
        try:
            gm._popen_wait(_hang_cmd(30), timeout=60, capture_output=True, text=True)
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(timeout=3)
    time.sleep(0.15)
    n = gm.cancel_processes(force=True)
    t.join(timeout=5)
    try:
        assert not t.is_alive(), "cancelled subprocess thread still running"
        assert n >= 1
        assert raised and isinstance(raised[0], GitCancelledError)
    finally:
        _cleanup_gm(gm)


def test_discard_workspace_deletes_incomplete_clone_despite_age_policy(
    monkeypatch,
):
    gm = _bare_gm()
    (gm.temp_dir / "partial.pack").write_text("in-flight", encoding="utf-8")
    gm._clone_in_progress = True
    monkeypatch.setattr("src.git_manager.settings.temp_cleanup_policy", "age")
    monkeypatch.setattr("src.git_manager.settings.temp_cleanup_max_age_days", 30.0)

    path = gm.temp_dir
    assert gm.should_discard_on_cancel() is True
    assert gm.discard_workspace() is True
    assert path is not None and not path.exists()
    assert GitManager.live_for("KILL-1") is None


def test_should_discard_keeps_reused_complete_clone():
    gm = _bare_gm()
    (gm.temp_dir / ".git").mkdir()
    gm._clone_in_progress = False
    try:
        assert gm.should_discard_on_cancel() is False
    finally:
        _cleanup_gm(gm)


def test_should_discard_empty_dir_after_reset():
    gm = _bare_gm()
    gm._clone_in_progress = False
    try:
        assert gm.should_discard_on_cancel() is True
    finally:
        _cleanup_gm(gm)


def test_live_for_registers_during_setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def fake_setup(self):
        self._register_live()
        seen["gm"] = GitManager.live_for("LIVE-1")
        self.temp_dir = tmp_path / "ws"
        self.temp_dir.mkdir()

    with patch.object(GitManager, "_setup_temp_working_dir", fake_setup):
        gm = GitManager(
            issue_key="LIVE-1",
            remote_url="https://gitlab.example.com/g/r.git",
            source_branch="develop",
            target_branch="main",
        )
    assert seen["gm"] is gm
    assert GitManager.live_for("LIVE-1") is gm
    gm._unregister_live()


@pytest.mark.asyncio
async def test_cancel_job_kills_git_and_deletes_in_progress_clone(
    processor, state_manager, fake_jira, tmp_path
):
    key = "CX-CLONE-1"
    state_manager.create_state(key, "s", "d")
    state_manager.update_state(key, status=TaskStatus.EXECUTING)

    gm = _bare_gm(issue_key=key)
    (gm.temp_dir / "objects").mkdir()
    (gm.temp_dir / "objects" / "pack").write_bytes(b"partial")
    gm._clone_in_progress = True
    clone_path = gm.temp_dir

    started = threading.Event()
    raised: list[BaseException] = []

    def worker() -> None:
        started.set()
        try:
            gm._popen_wait(_hang_cmd(30), timeout=60, capture_output=True, text=True)
        except BaseException as e:
            raised.append(e)

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(timeout=3)
    time.sleep(0.15)

    out = await processor.cancel_job(key, reason="stop clone")
    t.join(timeout=5)

    assert out["ok"] is True
    assert state_manager.get_state(key).status == TaskStatus.CANCELLED
    assert not t.is_alive()
    assert raised and isinstance(raised[0], GitCancelledError)
    assert clone_path is not None and not clone_path.exists()
    assert GitManager.live_for(key) is None


@pytest.mark.asyncio
async def test_cancel_job_force_kills_git_without_agent_runner(
    processor, state_manager, tmp_path
):
    """Clone happens before AgentRunner exists — cancel must still kill git."""
    key = "CX-CLONE-2"
    state_manager.create_state(key, "s", "d")
    state_manager.update_state(key, status=TaskStatus.PLANNING)
    assert processor._runner_for(key) is None

    gm = _bare_gm(issue_key=key)
    gm._clone_in_progress = True
    clone_path = gm.temp_dir
    (clone_path / "HEAD").write_text("partial", encoding="utf-8")

    n = processor._kill_git_for_issue(key)
    assert n >= 0
    assert not clone_path.exists()
    assert GitManager.live_for(key) is None


def test_init_git_manager_cancelled_error_does_not_fail_issue(
    processor, state_manager
):
    key = "CX-CLONE-3"
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state_manager.create_state(key, "s", desc)
    state_manager.update_state(key, status=TaskStatus.EXECUTING)

    with patch(
        "src.processor.GitManager",
        side_effect=GitCancelledError("killed"),
    ):
        out = processor._init_git_manager(key)
    assert out is None
    assert state_manager.get_state(key).status == TaskStatus.EXECUTING
    assert key not in processor._contexts


def test_prepare_blocking_cancelled_error_skips_fail_issue(
    processor, state_manager
):
    key = "CX-CLONE-4"
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state = state_manager.create_state(key, "s", desc)
    state_manager.update_state(key, status=TaskStatus.PLANNING)

    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=GitCancelledError("killed"),
    ):
        with patch.object(processor, "_fail_issue") as fail:
            out = processor._prepare_git_workspace_blocking(state)
    assert out is None
    fail.assert_not_called()
    assert state_manager.get_state(key).status == TaskStatus.PLANNING


def test_agent_cancel_task_force_kills_immediately(tmp_path):
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner(working_directory=tmp_path)
    proc = MagicMock()
    proc.returncode = None
    runner._running_tasks["t1"] = proc
    with patch.object(runner, "_kill_process_tree") as kill:
        assert runner.cancel_task("t1") is True
    kill.assert_called_once()
    assert kill.call_args.kwargs.get("force") is True


def test_agent_cancel_all_tasks_force_kills(tmp_path):
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner(working_directory=tmp_path)
    live = MagicMock()
    live.returncode = None
    runner._running_tasks = {"a": live, "b": MagicMock(returncode=0)}
    with patch.object(runner, "_kill_process_tree") as kill:
        n = runner.cancel_all_tasks()
    assert n == 1
    kill.assert_called_once()
    assert kill.call_args.kwargs.get("force") is True


def test_run_tracked_delegates_to_patched_subprocess_run():
    gm = _bare_gm()
    fake = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    with patch("src.git_manager.subprocess.run", return_value=fake) as run:
        out = gm._run_tracked(["git", "status"], timeout=9, capture_output=True, text=True)
    try:
        assert out is fake
        assert run.call_args.kwargs.get("timeout") == 9
    finally:
        _cleanup_gm(gm)


def test_process_kill_none_and_tree():
    from src.process_kill import kill_pid, kill_process_tree

    kill_process_tree(None)
    kill_pid(0)
    kill_pid(-1)

    proc = subprocess.Popen(
        _hang_cmd(30),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        kill_process_tree(proc, force=True)
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)


def test_kill_workspace_processes_kills_tool_in_clone_not_self():
    from src.process_kill import kill_workspace_processes

    root = _native_dir()
    other = _native_dir()
    inside = subprocess.Popen(
        _hang_cmd(30),
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    outsider = subprocess.Popen(
        _hang_cmd(30),
        cwd=str(other),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.15)
        n = kill_workspace_processes(root, force=True)
        inside.wait(timeout=5)
        assert inside.poll() is not None
        assert n >= 1
        assert outsider.poll() is None
    finally:
        for p in (inside, outsider):
            if p.poll() is None:
                p.kill()
                p.wait(timeout=3)
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


def test_processor_kill_children_sweeps_workspace(processor, state_manager):
    state_manager.create_state("WS-SWEEP-1", "s", "d")
    with patch.object(processor, "_kill_workspace_for_issue") as sweep:
        processor._kill_children_for_issue("WS-SWEEP-1")
    sweep.assert_called_once()


def test_codex_cancel_force_kills_immediately():
    from src.backends.codex import CodexBackend

    handle = {"proc": MagicMock(pid=4242), "cancel": False}
    with patch("src.process_kill.kill_process_tree") as kill:
        CodexBackend().cancel(handle)
    assert handle["cancel"] is True
    kill.assert_called_once()
    assert kill.call_args.kwargs.get("force") is True


def test_cancel_all_tasks_force_kills_codex_handle(tmp_path):
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner(working_directory=tmp_path)
    handle = {"mode": "codex", "backend": "codex", "proc": MagicMock(pid=7)}
    runner._running_tasks = {"c1": handle}
    with patch("src.backends.get_agent_backend") as gab:
        backend = MagicMock()
        gab.return_value = backend
        n = runner.cancel_all_tasks()
    assert n == 1
    assert handle["cancel"] is True
    backend.cancel.assert_called_once_with(handle)
