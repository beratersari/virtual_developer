"""Execution-workflow MR gates for different Jira prompt shapes.

Hermetic: local bare origin + real GitManager. The agent is stubbed so the
prompt decides whether a commit happens. A new MR is opened only when the
work branch is ahead of the target.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.git_manager import GitManager
from src.processor import JobProcessor
from src.state.models import TaskStatus
from tests.conftest import make_issue_event


def _ticket(body: str, source: str, target: str) -> str:
    return (
        f"{body}\n"
        "{params}\n"
        "Repository: LOCAL\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}\n"
    )


INVESTIGATE = "INVESTIGATION ONLY. Report findings. Do not change files or commit."
IMPLEMENT = "IMPLEMENT this change. Add vd_prompt_e2e.txt and commit."


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def origin_clone(tmp_path, monkeypatch):
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
    return origin, clone


def _gm(clone: Path, origin: Path, key: str, source: str, target: str = "develop") -> GitManager:
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key=key,
            remote_url=str(origin),
            source_branch=source,
            target_branch=target,
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_name = "origin"
    gm.remote_url = str(origin)
    gm.source_branch = source
    gm.target_branch = target
    gm.work_branch = source
    if source != "develop":
        _git(clone, "checkout", "-B", source)
    return gm


def _stub_agent(processor, gm: GitManager, *, commit: bool, returncode: int = 0):
    async def run(task, **_k):
        if commit and gm.temp_dir:
            path = Path(gm.temp_dir) / "vd_prompt_e2e.txt"
            path.write_text(f"{task.issue_key or 'job'} implement\n", encoding="utf-8")
            _git(Path(gm.temp_dir), "add", "vd_prompt_e2e.txt")
            _git(
                Path(gm.temp_dir),
                "commit",
                "-m",
                f"feat({task.issue_key or 'job'}): implement from prompt",
            )
        return {
            "returncode": returncode,
            "stdout": "investigated" if not commit else "implemented",
            "stderr": "" if returncode == 0 else "agent failed",
            "session_file": None,
            "opencode_session_id": "ses_prompt",
        }

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(side_effect=run)

    def init(_issue_key, _state=None):
        processor._contexts[_issue_key] = {"git": gm, "runner": runner}
        processor.git_manager = gm
        processor.agent_runner = runner
        return gm

    return runner, init


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


async def _run_job(processor, gm, key, summary, description, *, init):
    ev = make_issue_event(key=key, summary=summary, description=description)
    with patch.object(processor, "_init_git_manager", side_effect=init):
        with patch.object(gm, "_pat_for_remote", return_value="local-test-pat"):
            with patch.object(gm, "_assert_remote_host_allowed", return_value=None):
                await processor.process_event(ev)
    return processor.state_manager.get_state(key)


@pytest.mark.asyncio
async def test_investigation_prompt_no_commit_skips_mr(
    processor, origin_clone
):
    origin, clone = origin_clone
    key = "INV-1"
    work = "feature/INV-1"
    gm = _gm(clone, origin, key, work)
    _runner, init = _stub_agent(processor, gm, commit=False)
    with patch.object(gm, "create_merge_request", wraps=gm.create_merge_request) as spy:
        st = await _run_job(
            processor,
            gm,
            key,
            "Investigate login",
            _ticket(INVESTIGATE, work, "develop"),
            init=init,
        )
    spy.assert_not_called()
    assert st is not None
    assert (st.metadata or {}).get("merge_request_url") in (None, "")
    assert gm.commits_ahead_of_target(work) == 0


@pytest.mark.asyncio
async def test_implement_prompt_commits_opens_mr(processor, origin_clone):
    origin, clone = origin_clone
    key = "IMP-1"
    work = "feature/IMP-1"
    gm = _gm(clone, origin, key, work)
    _runner, init = _stub_agent(processor, gm, commit=True)
    with patch.object(gm, "create_merge_request", return_value="http://mr/imp-1") as spy:
        st = await _run_job(
            processor,
            gm,
            key,
            "Add flag",
            _ticket(IMPLEMENT, work, "develop"),
            init=init,
        )
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert (st.metadata or {}).get("merge_request_url") == "http://mr/imp-1"
    assert gm.commits_ahead_of_target(work) >= 1


@pytest.mark.asyncio
async def test_agent_error_without_commits_skips_mr(processor, origin_clone):
    origin, clone = origin_clone
    key = "ERR-0"
    work = "feature/ERR-0"
    gm = _gm(clone, origin, key, work)
    _runner, init = _stub_agent(processor, gm, commit=False, returncode=1)
    with patch.object(gm, "create_merge_request") as spy:
        st = await _run_job(
            processor,
            gm,
            key,
            "Investigate then fail",
            _ticket(INVESTIGATE, work, "develop"),
            init=init,
        )
    spy.assert_not_called()
    assert st is not None
    assert (st.metadata or {}).get("merge_request_url") in (None, "")


@pytest.mark.asyncio
async def test_agent_error_with_commits_still_opens_mr(processor, origin_clone):
    origin, clone = origin_clone
    key = "ERR-1"
    work = "feature/ERR-1"
    gm = _gm(clone, origin, key, work)
    _runner, init = _stub_agent(processor, gm, commit=True, returncode=2)
    with patch.object(gm, "create_merge_request", return_value="http://mr/err-1") as spy:
        st = await _run_job(
            processor,
            gm,
            key,
            "Implement then incomplete",
            _ticket(IMPLEMENT, work, "develop"),
            init=init,
        )
    spy.assert_called()
    assert st is not None
    assert (st.metadata or {}).get("merge_request_url") == "http://mr/err-1"


@pytest.mark.asyncio
async def test_source_equals_target_skips_new_mr(processor, origin_clone):
    origin, clone = origin_clone
    key = "SAME-1"
    gm = _gm(clone, origin, key, "develop", target="develop")
    processor.state_manager.create_state(key, "s", "d")
    processor.state_manager.update_state(key, status=TaskStatus.EXECUTING)
    processor._contexts[key] = {"git": gm, "runner": MagicMock()}
    with patch.object(gm, "create_merge_request") as spy:
        with patch.object(gm, "_pat_for_remote", return_value="local-test-pat"):
            with patch.object(gm, "_assert_remote_host_allowed", return_value=None):
                ok = await processor._push_and_create_mr(
                    processor.state_manager.get_state(key)
                )
    spy.assert_not_called()
    assert ok is False


def test_should_open_merge_request_conditions():
    gm = GitManager.__new__(GitManager)
    gm.work_branch = "feature/x"
    gm.target_branch = "develop"
    with patch.object(gm, "commits_ahead_of_target", return_value=2):
        assert gm.should_open_merge_request() is True
    with patch.object(gm, "commits_ahead_of_target", return_value=0):
        assert gm.should_open_merge_request() is False
    gm.work_branch = "main"
    gm.target_branch = "main"
    with patch.object(gm, "commits_ahead_of_target", return_value=9):
        assert gm.should_open_merge_request() is False
    gm.work_branch = "Feature/X"
    gm.target_branch = "feature/x"
    with patch.object(gm, "commits_ahead_of_target", return_value=3):
        assert gm.should_open_merge_request() is False
