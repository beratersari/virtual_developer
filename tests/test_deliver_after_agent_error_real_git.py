"""Real-git delivery after an agent error (local bare origin, no MagicMock git).

OpenCode itself is not invoked. Git commits, push, and remote-tip checks
are real subprocesses against a temp bare repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.git_manager import GitManager
from src.state.models import TaskStatus


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _sha(cwd: Path, ref: str = "HEAD") -> str:
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _remote_has(origin: Path, branch: str, sha: str) -> bool:
    out = _git(origin, "rev-parse", f"refs/heads/{branch}", check=False)
    if out.returncode != 0:
        return False
    return out.stdout.strip() == sha


@pytest.fixture
def origin_and_clone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "checkout", "-B", "develop")
    _git(seed, "config", "user.email", "dev@example.com")
    _git(seed, "config", "user.name", "Dev")
    (seed / "README").write_text("ok\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "develop")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "dev@example.com")
    _git(clone, "config", "user.name", "Dev")
    _git(clone, "checkout", "develop")
    return origin, clone


def _real_gm(clone: Path, origin: Path, issue_key: str, work_branch: str) -> GitManager:
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key=issue_key,
            remote_url=str(origin),
            source_branch=work_branch,
            target_branch="develop",
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_name = "origin"
    gm.work_branch = work_branch
    gm.remote_url = str(origin)
    return gm


def _allow_local_push(gm: GitManager):
    """Keep git push real; only skip GitLab host/PAT gates for a file origin."""
    return (
        patch.object(gm, "_pat_for_remote", return_value="local-test-pat"),
        patch.object(gm, "_assert_remote_host_allowed", return_value=None),
    )


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


@pytest.mark.asyncio
async def test_real_git_push_new_commit_after_agent_error(
    processor, state_manager, origin_and_clone
):
    origin, clone = origin_and_clone
    key = "REAL-1"
    work = "feature/REAL-1"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "real push", "d")
    processor._contexts[key] = {"git": gm, "runner": None}

    baseline = gm.get_last_commit_sha()
    assert baseline
    gm.delivery_baseline_sha = baseline
    processor._snapshot_delivery_baseline(key, gm)

    (clone / "fix.txt").write_text("committed work\n")
    _git(clone, "add", "fix.txt")
    _git(clone, "commit", "-m", "fix(real): agent committed then errored")
    new_sha = gm.get_last_commit_sha()
    assert new_sha != baseline
    assert not _remote_has(origin, work, new_sha)

    pat, allow = _allow_local_push(gm)
    with pat, allow:
        outcome = await processor._deliver_if_new_commits(state)

    assert outcome == "delivered"
    assert _remote_has(origin, work, new_sha)
    assert gm.head_is_on_remote(work) is True


@pytest.mark.asyncio
async def test_real_git_already_pushed_second_push_is_ok(
    processor, state_manager, origin_and_clone
):
    origin, clone = origin_and_clone
    key = "REAL-2"
    work = "feature/REAL-2"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "already remote", "d")
    processor._contexts[key] = {"git": gm, "runner": None}

    baseline = gm.get_last_commit_sha()
    gm.delivery_baseline_sha = baseline
    processor._snapshot_delivery_baseline(key, gm)

    (clone / "a.txt").write_text("one\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-m", "feat: first")
    sha = gm.get_last_commit_sha()

    pat, allow = _allow_local_push(gm)
    with pat, allow:
        assert gm.push(work) is True
        assert _remote_has(origin, work, sha)
        # Second delivery: push is already on remote
        outcome = await processor._deliver_if_new_commits(state)

    assert outcome == "delivered"
    assert _remote_has(origin, work, sha)


@pytest.mark.asyncio
async def test_real_git_always_pushes_when_no_new_commits(
    processor, state_manager, origin_and_clone
):
    origin, clone = origin_and_clone
    key = "REAL-3"
    work = "feature/REAL-3"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "no new", "d")
    processor._contexts[key] = {"git": gm, "runner": None}

    # Push the work branch at the job-start SHA (no later commit)
    baseline = gm.get_last_commit_sha()
    gm.delivery_baseline_sha = baseline
    processor._snapshot_delivery_baseline(key, gm)
    pat, allow = _allow_local_push(gm)
    with pat, allow:
        assert gm.push(work) is True
        outcome = await processor._deliver_if_new_commits(state)

    assert outcome == "none"
    assert _remote_has(origin, work, baseline)
    assert state_manager.get_state(key).status != TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_real_execution_error_pushes_real_commit(
    processor, state_manager, origin_and_clone, tmp_path
):
    """Full execution workflow: mocked agent only, real git commit + push."""
    from src.orchestrator.agent_runner import AgentTask
    from unittest.mock import AsyncMock

    origin, clone = origin_and_clone
    key = "REAL-EX"
    work = "feature/REAL-EX"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "exec error", "d")

    async def fake_prepare(_state):
        processor._contexts[key] = {"git": gm, "runner": runner}
        processor.git_manager = gm
        processor.agent_runner = runner
        return gm

    runner = type("R", (), {})()
    new_sha_box = {"sha": None}

    async def fake_agent(task: AgentTask, **_kw):
        (clone / "unit.c").write_text("int ok(void) { return 1; }\n")
        _git(clone, "add", "unit.c")
        _git(clone, "commit", "-m", "test: lead service unit tests")
        new_sha_box["sha"] = gm.get_last_commit_sha()
        return {
            "returncode": 2,
            "stdout": "There are no remaining todos. The task is fully complete.",
            "stderr": (
                "[INCOMPLETE] session still incomplete: "
                "open todos: 2 pending, 1 in_progress"
            ),
            "incomplete": True,
            "incomplete_reasons": ["open todos: 2 pending, 1 in_progress"],
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_real",
            "retry_info": {"attempts": 1, "retried": False},
            "timed_out": False,
        }

    runner.run_agent_with_retry = fake_agent
    runner.cancel_task = lambda *_a, **_k: True
    runner.cancel_all_tasks = lambda *_a, **_k: 0

    pat, allow = _allow_local_push(gm)
    with pat, allow:
        with patch.object(
            processor, "_prepare_git_workspace", new_callable=AsyncMock
        ) as prep:
            prep.side_effect = fake_prepare
            with patch("src.processor.settings") as s:
                s.default_agent = "atlas"
                s.agent_task_timeout_seconds = 30
                s.agent_task_max_retries = 0
                s.agent_task_max_incomplete_retries = 0
                s.default_branch = "develop"
                s.full_plans_dir = tmp_path / "plans"
                s.sisyphus_plans_dir = Path(".sisyphus/plans")
                await processor._start_execution_workflow(state)

    st = state_manager.get_state(key)
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert new_sha_box["sha"]
    assert _remote_has(origin, work, new_sha_box["sha"])
