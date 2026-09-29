"""Dashboard multi-repo jobs stay off the Jira description and clone side by side."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.dashboard.repo_sets import normalize_repository_urls, parse_repository_sets
from src.git_manager import GitManager


def test_running_multi_repo_row_blocks_each_repository(tmp_path):
    from src.state.queue_store import WorkQueueStore, workspace_lock_key

    store = WorkQueueStore(queue_dir=tmp_path)
    api = workspace_lock_key(
        "https://gitlab.example.com/acme/api.git", "feature/shared", "develop"
    )
    web = workspace_lock_key(
        "https://gitlab.example.com/acme/web.git", "feature/shared", "develop"
    )
    store.enqueue(
        source="jira",
        issue_key="KAN-20",
        repository_url="https://gitlab.example.com/acme/api.git",
        work_branch="feature/shared",
        target_branch="develop",
        lock_key=api,
        lock_keys=[api, web],
    )
    assert store.claim_next() is not None
    store.enqueue(
        source="jira",
        issue_key="KAN-21",
        repository_url="https://gitlab.example.com/acme/web.git",
        work_branch="feature/shared",
        target_branch="develop",
        lock_key=web,
    )
    assert store.claim_next() is None


def test_workspace_root_push_is_not_a_successful_delivery(monkeypatch):
    """Pushing the multi-repo root must not report that the commit is on the remote."""
    git = GitManager(issue_key=None)
    git.repo_checkouts = [object()]
    git.remote_enabled = True
    git.remote_url = "https://gitlab.example/acme/api.git"
    git.work_branch = "feature/KAN-20"
    git.target_branch = "main"
    monkeypatch.setattr(git, "_pat_for_remote", lambda _url: "secret")
    monkeypatch.setattr(git, "_with_auth_remote", lambda: None)
    assert git.push("feature/KAN-20") is False


def test_workspace_root_does_not_run_git(monkeypatch):
    ran = []

    def boom(*_a, **_k):
        ran.append(1)
        raise AssertionError("git must not run in the workspace root")

    monkeypatch.setattr("src.git_manager.subprocess.run", boom)
    git = GitManager(issue_key=None)
    git.repo_checkouts = [object()]
    result = git._run_git(["log", "-1", "--format=%H"], check=False)
    assert result.returncode == 1
    assert ran == []
    assert "not a git repository" in result.stderr


def test_repository_set_keeps_two_urls_and_drops_duplicates():
    rows = parse_repository_sets(
        [
            {
                "name": "Orders",
                "repositories": [
                    "https://gitlab.example.com/acme/api.git",
                    "https://gitlab.example.com/acme/api",
                    "https://gitlab.example.com/acme/web.git",
                ],
            },
            {"name": "short", "repositories": ["https://gitlab.example.com/acme/only.git"]},
        ]
    )
    assert rows == [
        {
            "name": "Orders",
            "repositories": [
                "https://gitlab.example.com/acme/api.git",
                "https://gitlab.example.com/acme/web.git",
            ],
        }
    ]
    assert normalize_repository_urls(
        "https://gitlab.example.com/acme/api.git",
        ["https://gitlab.example.com/acme/web.git"],
    ) == [
        "https://gitlab.example.com/acme/api.git",
        "https://gitlab.example.com/acme/web.git",
    ]


def test_dashboard_job_stores_extra_repos_outside_the_jira_description(tmp_path):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-20"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Span services",
        description="add the field",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
        project_key="KAN",
        source_branch_mode="custom",
        repository_urls=[
            "https://gitlab.example.com/acme/api.git",
            "https://gitlab.example.com/acme/web.git",
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["repository_urls"] == [
        "https://gitlab.example.com/acme/api.git",
        "https://gitlab.example.com/acme/web.git",
    ]
    desc = client.create_issue.call_args.kwargs.get("description") or ""
    assert desc.count("Repository:") == 2
    assert "acme/web" in desc
    assert "Source branch: develop" in desc


def test_schedule_keeps_a_branch_for_each_repository(tmp_path):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-21"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Split branches",
        description="add the field",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
        project_key="KAN",
        source_branch_mode="custom",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "develop",
                "target_branch": "develop",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "feature/web-side",
                "target_branch": "main",
            },
        ],
        jira_client=client,
        store=store,
    )
    refs = out["schedule"]["repository_refs"]
    assert refs[1]["source_branch"] == "feature/web-side"
    assert refs[1]["target_branch"] == "main"
    assert refs[0]["url"].endswith("/api.git")


def test_existing_issue_schedule_keeps_each_repository_branch(tmp_path):
    from src.scheduler.service import schedule_existing_issue
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-5",
        "fields": {
            "summary": "Existing",
            "description": (
                "{params}\n"
                "Repository: https://gitlab.example.com/acme/api.git\n"
                "Source branch: develop\n"
                "Target branch: develop\n"
                "Mode: build\n"
                "{params}"
            ),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
        },
    }
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = True
    out = schedule_existing_issue(
        "KAN-5",
        scheduled_at="2026-12-01T10:00:00",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        source_branch_mode="custom",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "develop",
                "target_branch": "develop",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "feature/web-side",
                "target_branch": "main",
            },
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert refs[1]["source_branch"] == "feature/web-side"
    assert refs[1]["target_branch"] == "main"
    text = out["schedule"]["issue_description"]
    assert text.count("Repository:") == 2
    assert "acme/web" in text
    assert "Source branch: feature/web-side" in text
    assert "Target branch: main" in text


def test_each_repository_keeps_its_own_branches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path))

    def fake_clone(self):
        assert self.temp_dir is not None
        (self.temp_dir / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(GitManager, "_clone_into_temp", fake_clone)
    git = GitManager(
        issue_key="KAN-21",
        remote_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "develop",
                "target_branch": "develop",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "feature/web-side",
                "target_branch": "main",
            },
        ],
    )
    by_name = {child.temp_dir.name: child for child in git.repo_checkouts}
    assert by_name["api"].source_branch == "develop"
    assert by_name["web"].source_branch == "feature/web-side"
    assert by_name["web"].target_branch == "main"
    same = GitManager(
        issue_key="KAN-21",
        remote_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        repository_urls=[
            "https://gitlab.example.com/acme/api.git",
            "https://gitlab.example.com/acme/web.git",
        ],
    )
    assert git.temp_dir is not None and same.temp_dir is not None
    assert git.temp_dir.name != same.temp_dir.name


def test_multi_repo_workspace_clones_each_repo_under_one_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path))

    def fake_clone(self):
        assert self.temp_dir is not None
        (self.temp_dir / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(GitManager, "_clone_into_temp", fake_clone)
    git = GitManager(
        issue_key="KAN-20",
        remote_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="develop",
        repository_urls=[
            "https://gitlab.example.com/acme/api.git",
            "https://gitlab.example.com/acme/web.git",
        ],
    )
    root = git.get_working_directory()
    assert root is not None
    assert root.name.startswith("multi_")
    names = sorted(child.temp_dir.name for child in git.repo_checkouts)
    assert names == ["api", "web"]
    assert all((child.temp_dir.parent == root) for child in git.repo_checkouts)
    assert all((child.temp_dir / ".git").is_dir() for child in git.repo_checkouts)
    assert not (root / ".git").exists()
    # The root is not a git repo. Cancel must not treat a finished set as
    # an incomplete clone, or the second job on this folder loses it.
    assert git.should_discard_on_cancel() is False


def test_incomplete_multi_repo_clone_is_still_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path))

    def fake_clone(self):
        assert self.temp_dir is not None
        if self.temp_dir.name == "api":
            (self.temp_dir / ".git").mkdir(parents=True, exist_ok=True)
            return
        raise RuntimeError("second clone failed")

    monkeypatch.setattr(GitManager, "_clone_into_temp", fake_clone)
    try:
        GitManager(
            issue_key="KAN-21",
            remote_url="https://gitlab.example.com/acme/api.git",
            source_branch="develop",
            target_branch="develop",
            repository_urls=[
                "https://gitlab.example.com/acme/api.git",
                "https://gitlab.example.com/acme/web.git",
            ],
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("second clone should fail")
    roots = list(tmp_path.glob("multi_*"))
    assert len(roots) == 1
    git = GitManager(issue_key=None)
    git.temp_dir = roots[0]
    done = GitManager(issue_key=None)
    done.temp_dir = roots[0] / "api"
    missing = GitManager(issue_key=None)
    missing.temp_dir = roots[0] / "web"
    git.repo_checkouts = [done, missing]
    # web never got a .git. Cancel during setup must still delete the set.
    assert git.should_discard_on_cancel() is True


def test_live_lock_uses_each_repository_target(
    tmp_path, monkeypatch, fake_jira, state_manager
):
    """A child clone is locked on its own target, not the first repository's."""
    from src.processor import JobProcessor
    from src.state.queue_store import WorkQueueStore, workspace_lock_key

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path))

    def fake_clone(self):
        assert self.temp_dir is not None
        (self.temp_dir / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(GitManager, "_clone_into_temp", fake_clone)
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    state_manager.create_state("KAN-21", "span", "d")
    state_manager.update_state(
        "KAN-21",
        metadata={
            "repository_urls": [api, web],
            "repository_refs": [
                {
                    "url": api,
                    "source_branch": "develop",
                    "target_branch": "develop",
                },
                {
                    "url": web,
                    "source_branch": "feature/web-side",
                    "target_branch": "main",
                },
            ],
        },
    )
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    git = proc._init_git_manager(
        "KAN-21",
        repository_url=api,
        source_branch="develop",
        target_branch="develop",
    )
    assert git is not None
    expected = workspace_lock_key(web, "feature/web-side", "main")
    store = WorkQueueStore(queue_dir=tmp_path / "queue")
    store.enqueue(
        source="jira",
        issue_key="KAN-22",
        repository_url=web,
        work_branch="feature/web-side",
        target_branch="main",
        lock_key=expected,
    )
    assert (
        store.claim_next(
            blocked_locks=proc.live_workspace_lock_keys(),
            max_running=6,
        )
        is None
    )


@pytest.mark.asyncio
async def test_board_rework_locks_every_repository_in_the_description(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    """A To Do rework has no schedule payload. The description names every repo.

    The second repository's branch must block another job before either clone starts.
    """
    from src.processor import JobProcessor
    from src.state.queue_store import workspace_lock_key

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    held = asyncio.Event()
    release = asyncio.Event()

    async def fake_process(_event):
        held.set()
        await release.wait()
        return {"ok": True, "work_started": True}

    proc.process_event = fake_process  # type: ignore[method-assign]
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "KAN-30",
            "fields": {
                "summary": "Rework the pair",
                "description": (
                    "{params}\n"
                    f"Repository: {api}\n"
                    "Source branch: develop\n"
                    "Target branch: develop\n"
                    f"Repository: {web}\n"
                    "Source branch: feature/web-side\n"
                    "Target branch: main\n"
                    "Mode: build\n"
                    "{params}\n"
                ),
                "status": {"name": "To Do"},
            },
        },
    }
    result = await proc.enqueue_jira_event(event)
    await asyncio.wait_for(held.wait(), timeout=2)
    try:
        web_lock = workspace_lock_key(web, "feature/web-side", "main")
        proc.queue_store.enqueue(
            source="jira",
            issue_key="KAN-31",
            repository_url=web,
            work_branch="feature/web-side",
            target_branch="main",
            lock_key=web_lock,
        )
        assert proc.queue_store.claim_next(max_running=6) is None
    finally:
        release.set()
