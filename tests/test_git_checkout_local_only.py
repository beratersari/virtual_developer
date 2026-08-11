"""Real-git: local-only source branch must not checkout origin/{work}."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.git_manager import GitManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


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
    _git(clone, "checkout", "develop")
    return origin, clone


def test_second_prepare_checkouts_local_when_source_never_pushed(
    origin_and_clone, monkeypatch
):
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    _origin, clone = origin_and_clone
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key="KAN-24",
            remote_url="https://gitlab.example.com/g/r.git",
            source_branch="feature/KAN-23",
            target_branch="develop",
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_name = "origin"
    gm.work_branch = "feature/KAN-23"

    first = gm._prepare_work_branch("feature/KAN-23", "develop")
    assert first == "feature/KAN-23"
    head1 = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, text=True
    ).strip()
    assert head1 == "feature/KAN-23"

    # Rework / second job on the same clone (source never pushed).
    second = gm._prepare_work_branch("feature/KAN-23", "develop")
    assert second == "feature/KAN-23"
    head2 = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, text=True
    ).strip()
    assert head2 == "feature/KAN-23"
    remote = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/feature/KAN-23^{commit}"],
        cwd=clone,
        capture_output=True,
        text=True,
    )
    assert remote.returncode != 0


def test_dirty_readme_kept_when_already_on_work_branch(
    origin_and_clone, monkeypatch
):
    """Reused clone already on the MR source must keep intentional uncommitted edits."""
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    _origin, clone = origin_and_clone
    _git(clone, "checkout", "-B", "feature/KAN-12278")
    (clone / "app.txt").write_text("work\n")
    _git(clone, "add", "app.txt")
    _git(clone, "commit", "-m", "feat")
    _git(clone, "push", "-u", "origin", "feature/KAN-12278")
    (clone / "README").write_text("intentional leftover\n")

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key="KAN-12278",
            remote_url="https://gitlab.example.com/g/r.git",
            source_branch="feature/KAN-12278",
            target_branch="develop",
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_name = "origin"
    gm.work_branch = "feature/KAN-12278"

    out = gm._prepare_work_branch("feature/KAN-12278", "develop")
    assert out == "feature/KAN-12278"
    head = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, text=True
    ).strip()
    assert head == "feature/KAN-12278"
    assert (clone / "README").read_text() == "intentional leftover\n"
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=clone, text=True
    ).strip()
    assert "README" in status


def test_dirty_tree_stashed_when_switching_branches(
    origin_and_clone, monkeypatch
):
    """Switching branches must stash leftovers, not reset --hard."""
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    _origin, clone = origin_and_clone
    _git(clone, "checkout", "-B", "feature/KAN-12278")
    (clone / "app.txt").write_text("work\n")
    _git(clone, "add", "app.txt")
    _git(clone, "commit", "-m", "feat")
    _git(clone, "push", "-u", "origin", "feature/KAN-12278")
    _git(clone, "checkout", "develop")
    (clone / "README").write_text("wip on develop\n")

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key="KAN-12278",
            remote_url="https://gitlab.example.com/g/r.git",
            source_branch="feature/KAN-12278",
            target_branch="develop",
        )
    gm.temp_dir = clone
    gm.remote_enabled = True
    gm.remote_name = "origin"

    out = gm._prepare_work_branch("feature/KAN-12278", "develop")
    assert out == "feature/KAN-12278"
    head = subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=clone, text=True
    ).strip()
    assert head == "feature/KAN-12278"
    stash = subprocess.check_output(
        ["git", "stash", "list"], cwd=clone, text=True
    )
    assert "vd: preserve uncommitted" in stash
    # Recoverable — not deleted
    subprocess.run(
        ["git", "stash", "pop"], cwd=clone, check=True, capture_output=True
    )
    assert "wip on develop" in (clone / "README").read_text()
