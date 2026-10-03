"""Follow-up checkout when one repository's saved source is a primary base.

The first run cuts feature/{KEY} from develop/main/release. A later MR
comment must land on that same branch, in the same multi_* folder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.config import settings
from src.git_manager import GitManager
from src.processor import JobProcessor


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _tip(bare: Path, branch: str) -> str:
    out = _git(bare, "rev-parse", f"refs/heads/{branch}", check=False)
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _origin(root: Path, name: str) -> Path:
    seed = root / f"{name}-seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "dev@example.com")
    _git(seed, "config", "user.name", "Dev")
    (seed / "README").write_text(f"{name} main\n", encoding="utf-8")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", f"{name} main")
    _git(seed, "checkout", "-B", "develop", "main")
    (seed / "README").write_text(f"{name} develop\n", encoding="utf-8")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", f"{name} develop")
    if name == "api":
        _git(seed, "checkout", "-B", "feature/KAN-7", "develop")
        (seed / "api.txt").write_text("feature work\n", encoding="utf-8")
        _git(seed, "add", "api.txt")
        _git(seed, "commit", "-m", "api feature")
    else:
        _git(seed, "checkout", "-B", "hotfix", "develop")
        (seed / "web.txt").write_text("hotfix work\n", encoding="utf-8")
        _git(seed, "add", "web.txt")
        _git(seed, "commit", "-m", "web hotfix")
    _git(seed, "checkout", "main")
    bare = root / f"{name}.git"
    _git(root, "clone", "--bare", str(seed), str(bare))
    return bare


def _allow_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        GitManager, "_assert_remote_host_allowed", lambda self, url: None
    )
    monkeypatch.setattr(
        GitManager, "_pat_for_remote", lambda self, url="": "local-test-pat"
    )
    monkeypatch.setattr(
        GitManager, "_https_url_with_settings_pat", lambda self, url="": None
    )


def _processor(state_manager, fake_jira, tmp_path, monkeypatch) -> JobProcessor:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "clones"))
    from unittest.mock import patch

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    return proc


def test_followup_on_custom_branch_keeps_the_other_repo_on_its_feature_branch(
    state_manager, fake_jira, tmp_path, monkeypatch
):
    """Commenting on the hotfix MR must not move the other repo onto develop.

    The ticket stored Source branch: develop for the API. The first run
    already cut feature/KAN-7 from it and opened that merge request.
    The web repo uses hotfix. A comment on the hotfix MR is a follow-up
    with keep_source. The API clone has to stay on feature/KAN-7, in the
    same multi_* folder, so the previous commits are still in the workspace.
    """
    origins = tmp_path / "origins"
    api = _origin(origins, "api")
    web = _origin(origins, "web")
    url_api = api.resolve().as_uri()
    url_web = web.resolve().as_uri()
    refs = [
        {"url": url_api, "source_branch": "develop", "target_branch": "main"},
        {"url": url_web, "source_branch": "hotfix", "target_branch": "develop"},
    ]
    _allow_local_git(monkeypatch)
    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    first = GitManager(
        issue_key="KAN-7",
        remote_url=url_api,
        source_branch="develop",
        target_branch="main",
        repository_refs=refs,
    )
    try:
        first.ensure_feature_branch("KAN-7")
        state_manager.create_state("KAN-7", "mixed", "d")
        state_manager.update_state(
            "KAN-7",
            metadata={"repository_urls": [url_api, url_web], "repository_refs": refs},
        )
        follow = proc._init_git_manager(
            "KAN-7",
            state_manager.get_state("KAN-7"),
            repository_url=url_web,
            source_branch="hotfix",
            target_branch="develop",
            keep_source_work_branch=True,
        )
        assert follow is not None
        try:
            follow.ensure_feature_branch("KAN-7")
            by_url = {child.remote_url: child for child in follow.repo_checkouts}
            api_dir = by_url[url_api].temp_dir
            web_dir = by_url[url_web].temp_dir
            assert _branch(web_dir) == "hotfix"
            assert follow.temp_dir is not None and first.temp_dir is not None
            problems = []
            if _branch(api_dir) != "feature/KAN-7":
                problems.append(
                    f"api branch is {_branch(api_dir)!r}, not feature/KAN-7"
                )
            if _head(api_dir) != _tip(api, "feature/KAN-7"):
                problems.append("api HEAD is not the feature/KAN-7 commit")
            if not (api_dir / "api.txt").is_file():
                problems.append("api clone does not contain the feature commit")
            if follow.temp_dir.name != first.temp_dir.name:
                problems.append(
                    f"folder {follow.temp_dir.name} is not {first.temp_dir.name}"
                )
            assert not problems, "; ".join(problems)
        finally:
            follow.cleanup()
    finally:
        first.cleanup()
