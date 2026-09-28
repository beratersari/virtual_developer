"""Dashboard multi-repo jobs stay off the Jira description and clone side by side."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

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
    assert desc.count("Repository:") == 1
    assert "acme/web" not in desc


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
