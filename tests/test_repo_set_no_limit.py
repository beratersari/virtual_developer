"""A repo set can hold any number of repositories.

Each clone still uses GIT_CLONE_TIMEOUT_SECONDS on its own. A multi-repo
job may spend that timeout once per repository before the agent clock
starts.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import clone_phase_limit_seconds, settings
from src.dashboard.repo_sets import (
    normalize_repository_refs,
    parse_repository_sets,
    repository_count,
)
from src.dashboard.schemas import RepositorySetItem, ScheduleCreateRequest
from src.dashboard.service import _schedule_repository_refs
from src.state.models import TaskStatus


def _urls(count: int) -> list[str]:
    return [f"https://gitlab.example.com/acme/repo-{i}.git" for i in range(count)]


def test_repository_set_keeps_every_url():
    urls = _urls(13)
    rows = parse_repository_sets([{"name": "Platform", "repositories": urls}])
    assert rows == [{"name": "Platform", "repositories": urls}]
    item = RepositorySetItem(name="Platform", repositories=urls)
    assert item.repositories == urls
    body = ScheduleCreateRequest(
        title="all",
        repository_url=urls[0],
        target_branch="develop",
        mode="build",
        scheduled_at="2026-10-09T12:00:00",
        repository_urls=urls,
        repository_refs=[
            {"url": url, "source_branch": "develop", "target_branch": "develop"}
            for url in urls
        ],
    )
    assert body.repository_urls == urls
    assert len(body.repository_refs) == 13
    refs = normalize_repository_refs(urls[0], "develop", "develop", urls[1:])
    assert [row["url"] for row in refs] == urls
    assert normalize_repository_refs(urls[0], "develop", "develop", []) == []


def test_schedule_keeps_every_repository(tmp_path):
    from src.processor import JobProcessor
    from src.state.schedule_store import ScheduleStore

    urls = _urls(13)
    refs = [
        {"url": url, "source_branch": "develop", "target_branch": "main"}
        for url in urls
    ]
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="wide",
        description="",
        repository_url=urls[0],
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at="2026-10-09T12:00:00",
        issue_key="KAN-13",
        issue_description="",
        repository_urls=urls,
        repository_refs=refs,
    )
    assert rec["repository_urls"] == urls
    assert len(rec["repository_refs"]) == 13
    assert len(_schedule_repository_refs(rec)) == 13

    proc = JobProcessor.__new__(JobProcessor)
    saved = proc._repository_urls_from_event(
        {"repository_url": urls[0], "repository_urls": urls, "repository_refs": refs}
    )
    assert saved["repository_urls"] == urls
    assert len(saved["repository_refs"]) == 13


def test_repository_count_uses_the_longer_list():
    urls = _urls(4)
    assert repository_count({"repository_urls": urls}) == 4
    assert repository_count({"repository_refs": [{"url": url} for url in urls[:2]]}) == 2
    assert repository_count({"repository_urls": urls[:1], "repository_refs": []}) == 1
    assert repository_count({}) == 1
    assert repository_count(None) == 1


def test_clone_phase_allows_each_repository_its_own_timeout():
    assert clone_phase_limit_seconds(3, 100) == 360
    assert clone_phase_limit_seconds(3, 100, submodule_timeout_seconds=80) == 600
    assert clone_phase_limit_seconds(1, 1800) == 1860


def test_each_clone_uses_the_full_env_timeout(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    monkeypatch.setattr(settings, "git_clone_timeout_seconds", 321)
    timeouts: list[int] = []

    def tracked(self, cmd, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(GitManager, "_run_tracked", tracked)
    monkeypatch.setattr(GitManager, "_assert_remote_host_allowed", lambda self, url: None)
    monkeypatch.setattr(GitManager, "_pat_for_remote", lambda self, url="": "")
    monkeypatch.setattr(GitManager, "_scrub_remote_credentials", lambda self: None)
    monkeypatch.setattr(GitManager, "_enable_git_longpaths", lambda self: None)
    monkeypatch.setattr(GitManager, "_materialize_job_remote_refs", lambda self: None)

    for index, url in enumerate(_urls(2)):
        gm = GitManager(issue_key=None)
        gm.temp_dir = tmp_path / f"clone-{index}"
        gm.temp_dir.mkdir()
        gm.remote_url = url
        gm._clone_into_temp()

    assert timeouts == [321, 321]


def test_new_run_clears_the_previous_clone_stamp(tmp_path, monkeypatch):
    from src.orchestrator.agent_runner import AgentTask
    from src.processor import JobProcessor
    from src.state.job_store import JobStore
    from src.state.manager import JiraStateManager

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = JobStore(jobs_dir=tmp_path / "jobs")
    sm.create_state("KAN-14", "sum", "desc")
    sm.update_state(
        "KAN-14",
        status=TaskStatus.PENDING,
        metadata={"clone_ready_at": "2020-01-01T00:00:00"},
    )
    job_id = proc._begin_workflow_run(
        sm.get_state("KAN-14"),
        status=TaskStatus.EXECUTING,
        task=AgentTask(description="t", prompt="do it", agent="atlas", issue_key="KAN-14"),
        workflow_type="execution",
        agent="atlas",
        job_status="executing",
    )
    assert job_id
    live = sm.get_state("KAN-14")
    assert live is not None
    assert not live.metadata.get("clone_ready_at")

    proc._note_clones_ready("KAN-14")
    stamped = sm.get_state("KAN-14")
    assert stamped is not None
    datetime.fromisoformat(stamped.metadata["clone_ready_at"])


def _daemon(state_manager):
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon._running = True
    daemon.state_manager = state_manager
    daemon.processor = MagicMock()
    daemon.processor._kill_children_for_issue = MagicMock()
    daemon.processor._fail_issue = MagicMock()
    daemon.processor._release_context = MagicMock()
    daemon.processor._is_live_processing = MagicMock(return_value=False)
    daemon.processor._abort_serve_sessions_for_issue = AsyncMock()
    return daemon


async def _one_monitor_pass(daemon) -> None:
    async def stop(_seconds):
        daemon._running = False

    daemon._running = True
    with patch("asyncio.sleep", side_effect=stop):
        await daemon._monitor_active_issues()


@pytest.mark.asyncio
async def test_multi_repo_clone_is_not_charged_to_the_agent_budget(
    state_manager, monkeypatch
):
    monkeypatch.setattr("src.config.live_agent_timeout_seconds", lambda: 60)
    monkeypatch.setattr(settings, "git_clone_timeout_seconds", 100)
    monkeypatch.setattr(settings, "git_update_submodules", False)
    monkeypatch.setattr(settings, "agent_task_max_retries", 0)
    urls = _urls(3)
    state_manager.create_state("SET-1", "s", "d")
    state_manager.update_state(
        "SET-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(seconds=250),
        max_retries=0,
        current_task_id="task-1",
        metadata={"repository_urls": urls},
    )
    daemon = _daemon(state_manager)
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_not_called()

    state_manager.update_state(
        "SET-1",
        started_at=datetime.now() - timedelta(seconds=400),
    )
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_called_once()


@pytest.mark.asyncio
async def test_submodule_timeout_is_allowed_once_per_repository(
    state_manager, monkeypatch
):
    monkeypatch.setattr("src.config.live_agent_timeout_seconds", lambda: 60)
    monkeypatch.setattr(settings, "git_clone_timeout_seconds", 100)
    monkeypatch.setattr(settings, "git_update_submodules", True)
    monkeypatch.setattr(settings, "git_submodule_timeout_seconds", 80)
    state_manager.create_state("SET-2", "s", "d")
    state_manager.update_state(
        "SET-2",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(seconds=500),
        max_retries=0,
        metadata={"repository_urls": _urls(3)},
    )
    daemon = _daemon(state_manager)
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_not_called()

    state_manager.update_state(
        "SET-2",
        started_at=datetime.now() - timedelta(seconds=650),
    )
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_called_once()


@pytest.mark.asyncio
async def test_agent_clock_starts_when_clones_finish(state_manager, monkeypatch):
    monkeypatch.setattr("src.config.live_agent_timeout_seconds", lambda: 60)
    monkeypatch.setattr(settings, "git_clone_timeout_seconds", 100)
    monkeypatch.setattr(settings, "git_update_submodules", False)
    state_manager.create_state("SET-3", "s", "d")
    state_manager.update_state(
        "SET-3",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(hours=2),
        max_retries=0,
        current_task_id="task-3",
        metadata={
            "repository_urls": _urls(3),
            "clone_ready_at": (datetime.now() - timedelta(seconds=30)).isoformat(
                timespec="seconds"
            ),
        },
    )
    daemon = _daemon(state_manager)
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_not_called()

    state_manager.update_state(
        "SET-3",
        metadata={
            "clone_ready_at": (datetime.now() - timedelta(seconds=200)).isoformat(
                timespec="seconds"
            ),
        },
    )
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_called_once()


@pytest.mark.asyncio
async def test_single_repo_stuck_limit_stays_the_agent_budget(
    state_manager, monkeypatch
):
    monkeypatch.setattr("src.config.live_agent_timeout_seconds", lambda: 60)
    monkeypatch.setattr(settings, "git_clone_timeout_seconds", 1800)
    monkeypatch.setattr(settings, "git_update_submodules", True)
    state_manager.create_state("ONE-1", "s", "d")
    state_manager.update_state(
        "ONE-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(seconds=50),
        timeout_seconds=60,
        max_retries=0,
        current_task_id="task-one",
    )
    daemon = _daemon(state_manager)
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_not_called()

    state_manager.update_state(
        "ONE-1",
        started_at=datetime.now() - timedelta(seconds=120),
    )
    await _one_monitor_pass(daemon)
    daemon.processor._fail_issue.assert_called_once()
