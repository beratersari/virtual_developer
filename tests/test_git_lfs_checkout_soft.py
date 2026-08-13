"""Git LFS filter noise must not fail a successful checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.git_manager import GitManager


def test_looks_like_lfs_filter_noise():
    assert GitManager._looks_like_lfs_filter_noise(
        "Switched to a new branch 'feature/X'\n"
        "git version >= 1.8.2 is required for Git LFS, your version:\n"
    )
    assert GitManager._looks_like_lfs_filter_noise(
        "error: failed to filter smudge filter lfs failed"
    )
    assert not GitManager._looks_like_lfs_filter_noise("fatal: pathspec did not match")


def test_run_git_checkout_lfs_noise_soft_success(tmp_path: Path, monkeypatch):
    """Non-zero checkout + LFS noise + HEAD on intended branch → returncode 0."""
    monkeypatch.chdir(tmp_path)
    gm = GitManager.__new__(GitManager)
    gm.temp_dir = tmp_path
    gm.repo_url = "https://gitlab.example.com/g/p.git"
    gm.remote_url = gm.repo_url
    gm.issue_key = "KAN-1"
    gm.work_branch = "feature/KAN-1"
    gm.source_branch = "feature/KAN-1"
    gm.target_branch = "develop"
    gm.gitlab_pat = ""
    (tmp_path / ".git").mkdir()

    fake = subprocess.CompletedProcess(
        args=["git", "checkout", "-B", "feature/KAN-1", "origin/feature/KAN-1"],
        returncode=1,
        stdout="Switched to a new branch 'feature/KAN-1'\n",
        stderr="git version >= 1.8.2 is required for Git LFS, your version:\n",
    )

    with patch("src.git_manager.subprocess.run", return_value=fake):
        with patch.object(gm, "get_current_branch", return_value="feature/KAN-1"):
            with patch.object(gm, "_apply_settings_pat_to_origin", return_value=False):
                result = gm._run_git(
                    ["checkout", "-B", "feature/KAN-1", "origin/feature/KAN-1"],
                    check=True,
                )
    assert result.returncode == 0


def test_run_git_checkout_real_failure_still_raises(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    gm = GitManager.__new__(GitManager)
    gm.temp_dir = tmp_path
    gm.repo_url = "https://gitlab.example.com/g/p.git"
    gm.remote_url = gm.repo_url
    gm.issue_key = "KAN-1"
    (tmp_path / ".git").mkdir()

    fake = subprocess.CompletedProcess(
        args=["git", "checkout", "nope"],
        returncode=1,
        stdout="",
        stderr="error: pathspec 'nope' did not match any file(s) known to git\n",
    )
    with patch("src.git_manager.subprocess.run", return_value=fake):
        with patch.object(gm, "get_current_branch", return_value="develop"):
            with patch.object(gm, "_apply_settings_pat_to_origin", return_value=False):
                with pytest.raises(RuntimeError, match="pathspec"):
                    gm._run_git(["checkout", "nope"], check=True)


def test_base_git_env_skips_lfs_smudge():
    env = GitManager._base_git_env()
    assert env.get("GIT_LFS_SKIP_SMUDGE") == "1"
