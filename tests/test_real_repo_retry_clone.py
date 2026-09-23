"""Real git clone: failed push leftovers, then the same folder and a reclone.

Clones the public Hello-World repository (with that repo as a submodule),
mirrors it to a local origin the test can push to, and drives GitManager
the way a job does.

1. Clone, check out feature/{KEY} from the target, update submodules.
2. Commit and dirty the tree, then fail the push. Leftovers stay.
3. A second manager reuses the folder. Startup drops the leftovers and
   the new commit pushes.
4. Deleting the folder makes the next manager reclone and fetch that
   pushed branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.git_manager import GitManager

REAL_REPO = "https://github.com/octocat/Hello-World.git"
ISSUE = "REAL-FLOW"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _mirror_real_repo(tmp_path: Path) -> tuple[Path, str]:
    """Clone Hello-World, add it as a submodule, push to a local bare origin."""
    seed = tmp_path / "seed"
    clone = subprocess.run(
        ["git", "clone", REAL_REPO, str(seed)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    if clone.returncode != 0:
        pytest.skip(
            "Could not clone the public repository "
            f"({(clone.stderr or clone.stdout or '').strip()[:300]})"
        )
    _git(seed, "config", "user.email", "dev@example.com")
    _git(seed, "config", "user.name", "Dev")
    branch = _git(seed, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    _git(seed, "submodule", "add", REAL_REPO, "vendor/hello")
    _git(seed, "commit", "-m", "add hello submodule")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(seed, "remote", "set-url", "origin", str(origin))
    _git(seed, "push", "-u", "origin", f"HEAD:{branch}")
    return origin, branch


def _manager(origin: Path, branch: str, monkeypatch: pytest.MonkeyPatch) -> GitManager:
    monkeypatch.setattr(GitManager, "_assert_remote_host_allowed", lambda self, url: None)
    monkeypatch.setattr(GitManager, "_pat_for_remote", lambda self, url: "local-test")
    monkeypatch.setattr(GitManager, "_apply_settings_pat_to_origin", lambda self: False)
    gm = GitManager(
        issue_key=ISSUE,
        remote_url=origin.as_uri(),
        source_branch=branch,
        target_branch=branch,
    )
    assert gm.temp_dir is not None
    _git(gm.temp_dir, "config", "user.email", "dev@example.com")
    _git(gm.temp_dir, "config", "user.name", "Dev")
    return gm


def test_real_clone_failed_push_then_reuse_and_reclone(tmp_path, monkeypatch):
    from src.config import settings

    clones = tmp_path / "clones"
    clones.mkdir()
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    monkeypatch.setattr(settings, "git_update_submodules", True)

    origin, branch = _mirror_real_repo(tmp_path)
    work = f"feature/{ISSUE}"

    first = _manager(origin, branch, monkeypatch)
    checked = first.ensure_feature_branch(ISSUE)
    assert checked == work
    clone = first.temp_dir
    assert clone is not None
    head = _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == work
    assert (clone / "vendor" / "hello" / "README").is_file()
    target_tip = _git(clone, "rev-parse", f"origin/{branch}").stdout.strip()
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == target_tip

    (clone / "leftover.txt").write_text("never pushed\n", encoding="utf-8")
    _git(clone, "add", "leftover.txt")
    _git(clone, "commit", "-m", "unpushed leftover")
    (clone / "README").write_text("dirty leftover\n", encoding="utf-8")
    (clone / "extra.txt").write_text("untracked\n", encoding="utf-8")
    hook = origin / "hooks" / "pre-receive"
    hook.write_bytes(b"#!/bin/sh\necho rejected by test >&2\nexit 1\n")
    assert first.push(work) is False
    assert first.last_push_error
    hook.unlink()
    assert (clone / "leftover.txt").is_file()
    assert "unpushed leftover" in _git(clone, "log", "--oneline").stdout
    (clone / ".keep-for-reuse").write_text("yes\n", encoding="utf-8")

    second = _manager(origin, branch, monkeypatch)
    assert second.temp_dir == clone
    assert (clone / ".keep-for-reuse").is_file()
    checked = second.ensure_feature_branch(ISSUE)
    assert checked == work
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == target_tip
    assert "unpushed leftover" not in _git(clone, "log", "--oneline").stdout
    assert not (clone / "leftover.txt").exists()
    assert not (clone / "extra.txt").exists()
    assert (clone / "vendor" / "hello" / "README").is_file()
    status = _git(clone, "status", "--porcelain").stdout.strip()
    assert status == ""

    (clone / "retry.txt").write_text("second job\n", encoding="utf-8")
    _git(clone, "add", "retry.txt")
    _git(clone, "commit", "-m", "retry ok")
    assert second.push(work) is True
    pushed = _git(clone, "rev-parse", f"origin/{work}").stdout.strip()
    assert pushed == _git(clone, "rev-parse", "HEAD").stdout.strip()

    from src.temp_fs import force_rmtree

    force_rmtree(clone)
    third = _manager(origin, branch, monkeypatch)
    assert third.temp_dir == clone
    assert (clone / ".git").is_dir()
    checked = third.ensure_feature_branch(ISSUE)
    assert checked == work
    again = third.temp_dir
    assert again is not None
    assert _git(again, "rev-parse", "HEAD").stdout.strip() == pushed
    assert (again / "retry.txt").is_file()
    assert "unpushed leftover" not in _git(again, "log", "--oneline").stdout
    assert (again / "vendor" / "hello" / "README").is_file()
