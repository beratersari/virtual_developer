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


def _latest_jira_description(client) -> str:
    if client.update_issue.called:
        fields = client.update_issue.call_args.kwargs.get("fields") or {}
        return str(fields.get("description") or "")
    return str(client.create_issue.call_args.kwargs.get("description") or "")


def test_issue_key_source_is_feature_key_on_every_repository(tmp_path):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-22"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Both from the issue",
        description="add the field",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
        project_key="KAN",
        source_branch_mode="issue_key",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "",
                "target_branch": "develop",
                "source_branch_mode": "issue_key",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "develop",
                "target_branch": "main",
                "source_branch_mode": "issue_key",
            },
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert [row["source_branch"] for row in refs] == [
        "feature/KAN-22",
        "feature/KAN-22",
    ]
    assert refs[1]["target_branch"] == "main"
    text = _latest_jira_description(client)
    assert text.count("Source branch: feature/KAN-22") == 2
    assert "Source branch: develop" not in text


def test_mixed_custom_and_issue_key_sources_stay_on_their_repositories(tmp_path):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-23"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Mixed sources",
        description="add the field",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="release",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
        project_key="KAN",
        source_branch_mode="custom",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "release",
                "target_branch": "develop",
                "source_branch_mode": "custom",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "develop",
                "target_branch": "main",
                "source_branch_mode": "from_issue",
            },
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert refs[0]["source_branch"] == "release"
    assert refs[1]["source_branch"] == "feature/KAN-23"
    assert refs[1]["target_branch"] == "main"
    text = _latest_jira_description(client)
    assert "Source branch: release" in text
    assert "Source branch: feature/KAN-23" in text
    assert "Source branch: develop" not in text
    assert out["schedule"]["issue_description"] == text


def test_issue_key_then_custom_keeps_each_repository_source(tmp_path):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-24"}
    client.transition_to_in_progress.return_value = True
    client.update_issue.return_value = True
    out = create_scheduled_job(
        title="Issue key then custom",
        description="add the field",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
        project_key="KAN",
        source_branch_mode="issue_key",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "develop",
                "target_branch": "develop",
                "source_branch_mode": "issue_key",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "hotfix",
                "target_branch": "main",
                "source_branch_mode": "custom",
            },
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert refs[0]["source_branch"] == "feature/KAN-24"
    assert refs[1]["source_branch"] == "hotfix"
    text = _latest_jira_description(client)
    assert "Source branch: feature/KAN-24" in text
    assert "Source branch: hotfix" in text
    assert "Source branch: develop" not in text


def test_existing_issue_records_issue_key_source_for_every_repository(tmp_path):
    from src.scheduler.service import schedule_existing_issue
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-5",
        "fields": {
            "summary": "Existing",
            "description": "plain prompt",
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
        source_branch="release",
        target_branch="develop",
        mode="build",
        source_branch_mode="custom",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "release",
                "target_branch": "develop",
                "source_branch_mode": "custom",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "develop",
                "target_branch": "main",
                "source_branch_mode": "issue_key",
            },
        ],
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert refs[0]["source_branch"] == "release"
    assert refs[1]["source_branch"] == "feature/KAN-5"
    text = out["schedule"]["issue_description"]
    assert "Source branch: release" in text
    assert "Source branch: feature/KAN-5" in text
    assert "Source branch: develop" not in text


def test_azure_issue_key_source_is_feature_key_on_every_repository(tmp_path, monkeypatch):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    updates: list = []

    def _create(self, project, wtype, fields):
        return {"id": 99, "rev": 1, "fields": {"System.Title": "Both"}}

    def _update(self, project, iid, fields):
        updates.append(dict(fields))
        return {"ok": True}

    monkeypatch.setattr("src.azure.client.AzureDevOpsClient.create_work_item", _create)
    monkeypatch.setattr(
        "src.azure.client.AzureDevOpsClient.update_work_item_fields", _update
    )
    monkeypatch.setattr(
        "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.azure.tracker.fetch_pat_myself",
        lambda **_k: {
            "name": "CORP\\Yaver",
            "uniqueName": "CORP\\Yaver",
            "displayName": "Yaver Bot",
            "key": "guid-1",
            "names": ["Yaver Bot", "CORP\\Yaver"],
        },
    )
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    out = create_scheduled_job(
        title="Azure both",
        description="add the field",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/api",
        source_branch="release",
        target_branch="develop",
        mode="build",
        scheduled_at="2099-01-01T10:00:00",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        azure_project="Demo",
        source_branch_mode="custom",
        repository_refs=[
            {
                "url": "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/api",
                "source_branch": "release",
                "target_branch": "develop",
                "source_branch_mode": "custom",
            },
            {
                "url": "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/web",
                "source_branch": "develop",
                "target_branch": "main",
                "source_branch_mode": "issue_key",
            },
        ],
        store=store,
    )
    assert out["ok"] is True
    refs = out["schedule"]["repository_refs"]
    assert refs[0]["source_branch"] == "release"
    assert refs[1]["source_branch"] == "feature/WIT-DEMO-99"
    text = out["schedule"]["issue_description"]
    assert "Source branch: release" in text
    assert "Source branch: feature/WIT-DEMO-99" in text
    assert "Source branch: develop" not in text
    assert updates
    assert "feature/WIT-DEMO-99" in str(updates[-1])


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


def _branch_event(key: str, repos: list[tuple[str, str, str]]) -> dict:
    lines = ["{params}"]
    for url, src, tgt in repos:
        lines.extend(
            [
                f"Repository: {url}",
                f"Source branch: {src}",
                f"Target branch: {tgt}",
            ]
        )
    lines.extend(["Mode: build", "{params}"])
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": key,
            "fields": {
                "summary": key,
                "description": "\n".join(lines) + "\n",
                "status": {"name": "To Do"},
            },
        },
    }


@pytest.mark.asyncio
async def test_single_repo_job_waits_for_the_running_multi_repo_job(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    """Same repo, source, and target on another ticket waits, then runs.

    An open review is not an error. The queue holds the later job only
    while the multi-repo job is still executing.
    """
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_concurrent_jobs", 4)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    opened = proc.job_store.create_job(issue_key="KAN-605", summary="already open")
    proc.job_store.update_job(
        opened["job_id"],
        deliveries=[
            {
                "repository_url": "https://gitlab.example.com/acme/api.git",
                "feature_branch": "feature/shared",
                "target_branch": "main",
                "merge_request_url": "https://gitlab.example.com/acme/api/-/merge_requests/7",
                "merge_request_state": "opened",
            }
        ],
    )

    started: list[str] = []
    multi_held = asyncio.Event()
    release_multi = asyncio.Event()
    single_started = asyncio.Event()
    other_started = asyncio.Event()

    async def fake_process(event):
        key = event["issue"]["key"]
        started.append(key)
        if key == "KAN-30":
            multi_held.set()
            await release_multi.wait()
        if key == "KAN-31":
            single_started.set()
        if key == "KAN-32":
            other_started.set()
        return {"ok": True, "work_started": True}

    proc.process_event = fake_process  # type: ignore[method-assign]
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    multi = await proc.enqueue_jira_event(
        _branch_event(
            "KAN-30",
            [(api, "feature/shared", "main"), (web, "feature/web-side", "develop")],
        )
    )
    await asyncio.wait_for(multi_held.wait(), timeout=2)
    assert multi["ok"] is True
    single = await proc.enqueue_jira_event(
        _branch_event("KAN-31", [(api, "feature/shared", "main")])
    )
    other = await proc.enqueue_jira_event(
        _branch_event("KAN-32", [(api, "feature/shared", "develop")])
    )
    await asyncio.wait_for(other_started.wait(), timeout=2)
    assert single["ok"] is True
    assert single["queued"] is True
    assert single["started"] is False
    assert other["ok"] is True
    assert "KAN-31" not in started
    assert "KAN-32" in started
    waiting = proc.queue_store.list_items(status="queued", limit=20)
    assert [row.get("issue_key") for row in waiting] == ["KAN-31"]
    release_multi.set()
    await asyncio.wait_for(single_started.wait(), timeout=2)
    assert started.index("KAN-30") < started.index("KAN-31")
