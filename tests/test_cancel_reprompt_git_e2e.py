"""E2E: Stop then new prompt must evict leftover git and not fail askpass.

Mirrors the operator failure:

  1. Job is stopped mid-checkout
  2. Next prompt reuses the clone
  3. ``git checkout -B`` used to fail on leftover ``.git/index.lock``
  4. Workspace prep used to fail on locked ``vd-git-askpass.cmd``

Hermetic: real ``git`` + real leftover process + dashboard cancel HTTP.
No live Jira / OpenCode. Skip when ``git`` is missing.

Run::

    .venv/bin/python -m pytest tests/test_cancel_reprompt_git_e2e.py -q
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.git_manager import GitManager
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts(tmp_path, monkeypatch):
    """Do not walk the real ``.jira-agent`` (WSL 9p). Keep askpass in tmp."""
    data = tmp_path / "yaver-data"
    data.mkdir()
    monkeypatch.setenv("YAVER_DATA_DIR", str(data))
    monkeypatch.setenv("VD_DATA_DIR", str(data))
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: data)
    yield


@pytest.fixture(autouse=True)
def _clear_live_git_managers():
    GitManager._live_by_issue.clear()
    yield
    GitManager._live_by_issue.clear()


def _native_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="vd-reprompt-e2e-"))


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _make_origin_and_clone() -> tuple[Path, Path, Path]:
    """Bare origin (develop) + working clone. Caller must rmtree ``root``."""
    root = _native_dir()
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "config", "user.email", "e2e@example.com")
    _git(seed, "config", "user.name", "E2E")
    _git(seed, "checkout", "-B", "develop")
    (seed / "README").write_text("e2e seed\n", encoding="utf-8")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "chore: seed")
    origin = root / "origin.git"
    _git(root, "clone", "--bare", str(seed), str(origin))
    clone = root / "clone"
    _git(root, "clone", str(origin), str(clone))
    _git(clone, "checkout", "develop")
    return root, origin, clone


def _hold_index_lock(clone: Path) -> subprocess.Popen:
    """Agent-like leftover: cwd=clone, keeps ``.git/index.lock`` open."""
    lock = clone / ".git" / "index.lock"
    script = (
        "import time\n"
        f"p = {str(lock)!r}\n"
        "f = open(p, 'w')\n"
        "f.write('held-by-e2e\\n')\n"
        "f.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(clone),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        if lock.is_file() and proc.poll() is None:
            return proc
        time.sleep(0.05)
    if proc.poll() is None:
        proc.kill()
    raise RuntimeError("failed to start index.lock holder")


def _checkout_fails_on_lock(clone: Path) -> None:
    r = subprocess.run(
        ["git", "checkout", "-B", "feature/e2e-probe", "origin/develop"],
        cwd=str(clone),
        capture_output=True,
        text=True,
    )
    blob = f"{r.stderr or ''}\n{r.stdout or ''}".lower()
    assert r.returncode != 0, "checkout must fail while index.lock is held"
    assert "index.lock" in blob or "another git process" in blob, blob[:400]


def _bare_gm(clone: Path, *, issue_key: str) -> GitManager:
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key=issue_key,
            remote_url="https://gitlab.example.com/group/repo.git",
            source_branch=f"feature/{issue_key}",
            target_branch="develop",
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_url = "https://gitlab.example.com/group/repo.git"
    gm.remote_name = "repo"
    gm.source_branch = f"feature/{issue_key}"
    gm.target_branch = "develop"
    gm.work_branch = f"feature/{issue_key}"
    gm._init_proc_state()
    gm._register_live()
    return gm


def _processor(tmp_path, monkeypatch, fake_jira, reporter) -> JobProcessor:
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _cleanup(root: Path, *procs: subprocess.Popen) -> None:
    for proc in procs:
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_e2e_dashboard_stop_then_reprompt_checkout(
    tmp_path, monkeypatch, fake_jira, reporter
):
    """Operator path: leftover git holds lock → Stop → next checkout -B works."""
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    monkeypatch.setattr(real_settings, "gitlab_pat", "")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map({})

    root, _origin, clone = _make_origin_and_clone()
    holder = None
    key = "E2E-LOCK-1"
    try:
        holder = _hold_index_lock(clone)
        _checkout_fails_on_lock(clone)

        proc = _processor(tmp_path, monkeypatch, fake_jira, reporter)
        proc.state_manager.create_state(key, "reprompt after stop", "d")
        proc.state_manager.update_state(key, status=TaskStatus.EXECUTING)
        gm = _bare_gm(clone, issue_key=key)
        proc._contexts[key] = {"git": gm, "runner": None}

        app = create_dashboard_app(
            processor=proc, state_manager=proc.state_manager
        )
        client = TestClient(app)
        resp = client.post(f"/api/tasks/{key}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("ok") is True

        try:
            holder.wait(timeout=8)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=3)
            pytest.fail("dashboard cancel left the lock-holder process alive")
        assert holder.poll() is not None
        lock = clone / ".git" / "index.lock"
        assert not lock.exists(), "index.lock must be gone after Stop"

        # New prompt reuses the same clone (complete tree is kept).
        gm2 = _bare_gm(clone, issue_key=key)
        checked = gm2._checkout_work_branch_from_target(
            f"feature/{key}", "develop"
        )
        assert checked == f"feature/{key}"
        head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert head == f"feature/{key}"
        assert proc.state_manager.get_state(key).status == TaskStatus.CANCELLED
    finally:
        _cleanup(root, holder)


def test_e2e_new_prompt_checkout_retries_while_lock_held(
    tmp_path, monkeypatch, fake_jira, reporter
):
    """Next ``git checkout -B`` must reclaim leftover git and succeed."""
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    monkeypatch.setattr(real_settings, "gitlab_pat", "")

    root, _origin, clone = _make_origin_and_clone()
    holder = None
    try:
        holder = _hold_index_lock(clone)
        _checkout_fails_on_lock(clone)

        gm = _bare_gm(clone, issue_key="E2E-LOCK-2")
        out = gm._run_git(
            ["checkout", "-B", "feature/E2E-LOCK-2", "origin/develop"]
        )
        assert out.returncode == 0, out.stderr
        head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert head == "feature/E2E-LOCK-2"
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=3)
        assert not (clone / ".git" / "index.lock").exists()
    finally:
        _cleanup(root, holder)


def test_e2e_askpass_unwritable_does_not_fail_checkout(tmp_path, monkeypatch):
    """Locked/unwritable askpass helper must not fail the next git checkout."""
    from src.config import settings as real_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    monkeypatch.setattr(real_settings, "gitlab_pat", "e2e-pat")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map(
            {"gitlab.example.com": "e2e-pat"}
        )

    path = GitManager._ensure_askpass_script()
    assert path is not None
    path.write_text("stale-askpass\n", encoding="utf-8")
    path.chmod(0o444)

    root, _origin, clone = _make_origin_and_clone()
    try:
        env = None
        with patch.object(GitManager, "_setup_temp_working_dir"):
            gm = GitManager(
                issue_key="E2E-ASK-1",
                remote_url="https://gitlab.example.com/group/repo.git",
                source_branch="feature/E2E-ASK-1",
                target_branch="develop",
            )
        gm.temp_dir = clone
        gm.remote_url = "https://gitlab.example.com/group/repo.git"
        env = gm._apply_pat_to_git_env(url=gm.remote_url)
        assert env.get("VD_GIT_PASSWORD") == "e2e-pat"
        # Helper may be reused (locked) or skipped; must not raise.
        again = GitManager._ensure_askpass_script()
        assert again is None or again.exists()

        gm._init_proc_state()
        out = gm._run_git(
            ["checkout", "-B", "feature/E2E-ASK-1", "origin/develop"]
        )
        assert out.returncode == 0, out.stderr
        head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        assert head == "feature/E2E-ASK-1"
    finally:
        try:
            path.chmod(0o644)
        except OSError:
            pass
        _cleanup(root)
