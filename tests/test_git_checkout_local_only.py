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
