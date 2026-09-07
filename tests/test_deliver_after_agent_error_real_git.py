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
async def test_real_git_prior_unpushed_commit_still_delivered(
    processor, state_manager, origin_and_clone
):
    """Prior job committed; this job HEAD is unchanged — still push the tip."""
    origin, clone = origin_and_clone
    key = "REAL-PRIOR"
    work = "feature/REAL-PRIOR"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "prior commit", "d")
    processor._contexts[key] = {"git": gm, "runner": None}

    # Previous job committed, then failed before push.
    (clone / "lead_test.c").write_text("int ok(void) { return 1; }\n")
    _git(clone, "add", "lead_test.c")
    _git(clone, "commit", "-m", "[kan] test: lead service unit tests")
    prior_sha = gm.get_last_commit_sha()
    assert prior_sha
    assert not _remote_has(origin, work, prior_sha)

    # This job starts at that same SHA (OpenCode makes no new commit).
    gm.delivery_baseline_sha = prior_sha
    processor._snapshot_delivery_baseline(key, gm)
    assert gm.get_last_commit_sha() == prior_sha
    assert gm.commits_ahead_of_target(work) >= 1

    pat, allow = _allow_local_push(gm)
    with pat, allow:
        outcome = await processor._deliver_if_new_commits(state)

    assert outcome == "delivered"
    assert _remote_has(origin, work, prior_sha)
    assert gm.head_is_on_remote(work) is True


@pytest.mark.asyncio
async def test_unknown_agent_no_new_sha_stays_error(
    processor, state_manager, origin_and_clone, tmp_path
):
    """Follow-up on a branch that already has commits: unknown agent is ERROR.

    Prior tip ahead of target must not be attributed as this job succeeding.
    """
    from unittest.mock import AsyncMock

    origin, clone = origin_and_clone
    key = "KAN-238"
    work = "feature/KAN-1905"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "follow-up", "d")

    (clone / "old.c").write_text("int old(void) { return 1; }\n")
    _git(clone, "add", "old.c")
    _git(clone, "commit", "-m", "[KAN-7] prior work")
    prior = gm.get_last_commit_sha()
    assert prior
    gm.delivery_baseline_sha = prior
    processor._snapshot_delivery_baseline(key, gm)
    assert gm.commits_ahead_of_target(work) >= 1

    async def fake_prepare(_state):
        processor._contexts[key] = {"git": gm, "runner": runner}
        processor.git_manager = gm
        processor.agent_runner = runner
        return gm

    runner = type("R", (), {})()

    async def fake_agent(_task, **_kw):
        return {
            "returncode": 1,
            "stdout": "[serve] session resumed: ses_f8d3ef9c6ffeBocBaLipXLltEs",
            "stderr": (
                "[serve] agent 'derman-build' is not registered on this "
                "OpenCode serve. Available (sample): build, plan."
            ),
            "incomplete": False,
            "incomplete_reasons": ["unknown agent: derman-build"],
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_f8d3ef9c6ffeBocBaLipXLltEs",
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
                s.default_agent = "derman-build"
                s.agent_task_timeout_seconds = 30
                s.agent_task_max_retries = 3
                s.agent_task_max_incomplete_retries = 0
                s.default_branch = "develop"
                s.full_plans_dir = tmp_path / "plans"
                s.sisyphus_plans_dir = Path(".sisyphus/plans")
                await processor._start_execution_workflow(state)

    st = state_manager.get_state(key)
    assert st is not None
    assert st.status == TaskStatus.ERROR, st.status
    assert "not registered" in (st.error_message or "")
    jid = (st.metadata or {}).get("current_job_id") or processor._active_jobs.get(key)
    if jid:
        job = processor.job_store.get_job(jid) or {}
        assert job.get("status") == "error"


@pytest.mark.asyncio
async def test_zero_ahead_branch_does_not_open_mr_after_agent_error(
    processor, state_manager, origin_and_clone, tmp_path
):
    """KAN-218 / job_7435790adfb0: new feature branch == target → no empty MR.

    Agent failed (unknown agent). Work branch was cut from the target and
    has 0 unique commits. Push + create_merge_request must not run.
    """
    from unittest.mock import AsyncMock

    origin, clone = origin_and_clone
    key = "KAN-218"
    work = "feature/KAN-218"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "new session", "d")
    baseline = gm.get_last_commit_sha()
    assert baseline
    gm.delivery_baseline_sha = baseline
    processor._snapshot_delivery_baseline(key, gm)
    assert gm.commits_ahead_of_target(work) == 0

    async def fake_prepare(_state):
        processor._contexts[key] = {"git": gm, "runner": runner}
        processor.git_manager = gm
        processor.agent_runner = runner
        return gm

    runner = type("R", (), {})()

    async def fake_agent(_task, **_kw):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": (
                "[serve] agent 'derman-build' is not registered on this "
                "OpenCode serve. Available (sample): build, plan."
            ),
            "incomplete": False,
            "incomplete_reasons": ["unknown agent: derman-build"],
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_kan218",
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
            with patch.object(gm, "create_merge_request") as create_mr:
                with patch("src.processor.settings") as s:
                    s.default_agent = "derman-build"
                    s.agent_task_timeout_seconds = 30
                    s.agent_task_max_retries = 0
                    s.agent_task_max_incomplete_retries = 0
                    s.default_branch = "develop"
                    s.full_plans_dir = tmp_path / "plans"
                    s.sisyphus_plans_dir = Path(".sisyphus/plans")
                    await processor._start_execution_workflow(state)
                create_mr.assert_not_called()

    st = state_manager.get_state(key)
    assert st is not None
    assert st.status == TaskStatus.ERROR, st.status
    assert "not registered" in (st.error_message or "")
    assert not (st.metadata or {}).get("merge_request_url")
    # Push is allowed; an empty MR must not be opened.


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


@pytest.mark.asyncio
async def test_real_execution_success_pushes_prior_unpushed_commit(
    processor, state_manager, origin_and_clone, tmp_path
):
    """Success + no new SHA this job still pushes a prior unpushed commit."""
    from unittest.mock import AsyncMock

    origin, clone = origin_and_clone
    key = "REAL-RERUN"
    work = "feature/REAL-RERUN"
    _git(clone, "checkout", "-B", work)
    gm = _real_gm(clone, origin, key, work)
    state = state_manager.create_state(key, "rerun", "d")

    (clone / "lead.c").write_text("int lead(void) { return 0; }\n")
    _git(clone, "add", "lead.c")
    _git(clone, "commit", "-m", "[kan] test: lead service unit tests")
    prior_sha = gm.get_last_commit_sha()
    assert prior_sha
    assert not _remote_has(origin, work, prior_sha)

    async def fake_prepare(_state):
        processor._contexts[key] = {"git": gm, "runner": runner}
        processor.git_manager = gm
        processor.agent_runner = runner
        return gm

    runner = type("R", (), {})()

    async def fake_agent(_task, **_kw):
        return {
            "returncode": 0,
            "stdout": "Work is already done on this branch.",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_rerun",
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
    assert (st.metadata or {}).get("delivery_status") == "delivered"
    assert _remote_has(origin, work, prior_sha)
