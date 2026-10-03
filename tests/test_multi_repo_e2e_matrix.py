"""Multi-repo checkout, OpenCode root, and per-repo push.

Each case clones local bare remotes through GitManager (the same path a
job uses), checks the branch and commit each clone lands on, and checks
that OpenCode's working directory is the multi_* root. Push cases use
real git push against those remotes. No network and no GitLab PAT.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.config import settings
from src.git_manager import GitCloneError, GitManager, GitSourceBranchError
from src.orchestrator.agent_runner import AgentRunner
from src.processor import JobProcessor
from src.scheduler.service import (
    refs_with_issue_key_branches,
    work_branch_for_issue_key,
)
from src.state.models import TaskStatus

SOURCES = [
    "",
    "main",
    "develop",
    "master",
    "dev",
    "trunk",
    "release/1.0",
    "release/9.9",
    "feature/existing",
    "feature/new-side",
    "fix/hotfix",
    "feature/legacy-name",
]
TARGETS = ["main", "develop", "release/1.0"]
MODES = ["issue_key", "custom"]
KEYS = ["KAN-7", "KAN-42", "AZ-3"]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _tip(bare: Path, branch: str) -> str:
    out = _git(bare, "rev-parse", f"refs/heads/{branch}", check=False)
    if out.returncode != 0:
        return ""
    return (out.stdout or "").strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _make_origin(root: Path, name: str) -> Path:
    seed = root / f"{name}-seed"
    seed.mkdir(parents=True)
    _git(seed, "init", "-b", "main")
    _git(seed, "config", "user.email", "dev@example.com")
    _git(seed, "config", "user.name", "Dev")
    (seed / "README").write_text(f"{name} main\n", encoding="utf-8")
    _git(seed, "add", "README")
    _git(seed, "commit", "-m", f"{name} main")
    for branch, text in (
        ("develop", f"{name} develop\n"),
        ("release/1.0", f"{name} release\n"),
    ):
        _git(seed, "checkout", "-B", branch, "main")
        (seed / "README").write_text(text, encoding="utf-8")
        _git(seed, "add", "README")
        _git(seed, "commit", "-m", f"{name} {branch}")
    _git(seed, "checkout", "-B", "feature/existing", "main")
    (seed / "extra.txt").write_text(f"{name} existing\n", encoding="utf-8")
    _git(seed, "add", "extra.txt")
    _git(seed, "commit", "-m", f"{name} existing")
    _git(seed, "checkout", "main")
    bare = root / f"{name}.git"
    _git(root, "clone", "--bare", str(seed), str(bare))
    return bare


def _allow_file_url(url: str, original) -> bool:
    raw = (url or "").strip()
    if raw.lower().startswith("file://") and len(raw) > 8:
        return True
    return original(url)


def _build_cases() -> List[Dict[str, Any]]:
    raw: List[Dict[str, Any]] = []
    for src_a in SOURCES:
        for tgt_a in TARGETS:
            for mode_a in MODES:
                for src_b in SOURCES:
                    for tgt_b in TARGETS:
                        for mode_b in MODES:
                            raw.append(
                                {
                                    "src": [src_a, src_b, SOURCES[(len(raw) + 3) % len(SOURCES)]],
                                    "tgt": [tgt_a, tgt_b, TARGETS[(len(raw) + 1) % 3]],
                                    "mode": [mode_a, mode_b, MODES[len(raw) % 2]],
                                    "repos": 3 if len(raw) % 4 == 0 else 2,
                                    "key": KEYS[len(raw) % 3],
                                    "same_leaf": len(raw) % 17 == 0,
                                }
                            )
    step = max(1, len(raw) // 200)
    picked = raw[::step][:200]
    # Always include the pairs that fail closed if one repo inherits the other.
    must = [
        {
            "src": ["", "", "feature/existing"],
            "tgt": ["main", "develop", "release/1.0"],
            "mode": ["issue_key", "issue_key", "custom"],
            "repos": 3,
            "key": "KAN-7",
            "same_leaf": False,
        },
        {
            "src": ["feature/existing", "", "fix/hotfix"],
            "tgt": ["main", "develop", "main"],
            "mode": ["custom", "issue_key", "custom"],
            "repos": 3,
            "key": "KAN-42",
            "same_leaf": False,
        },
        {
            "src": ["", "feature/existing", ""],
            "tgt": ["develop", "main", "release/1.0"],
            "mode": ["issue_key", "custom", "issue_key"],
            "repos": 2,
            "key": "AZ-3",
            "same_leaf": True,
        },
        {
            "src": ["release/1.0", "develop", "main"],
            "tgt": ["develop", "main", "release/1.0"],
            "mode": ["custom", "custom", "custom"],
            "repos": 3,
            "key": "KAN-7",
            "same_leaf": False,
        },
        {
            "src": ["feature/new-side", "feature/legacy-name", "feature/existing"],
            "tgt": ["release/1.0", "develop", "main"],
            "mode": ["custom", "custom", "custom"],
            "repos": 3,
            "key": "KAN-42",
            "same_leaf": True,
        },
    ]
    seen = set()
    out: List[Dict[str, Any]] = []
    for row in must + picked:
        token = (
            tuple(row["src"]),
            tuple(row["tgt"]),
            tuple(row["mode"]),
            row["repos"],
            row["key"],
            row["same_leaf"],
        )
        if token in seen:
            continue
        seen.add(token)
        out.append(row)
    assert len(out) >= 200
    return out


CASES = _build_cases()


def _intended(mode: str, source: str, target: str, key: str) -> str:
    if mode == "issue_key":
        return GitManager.resolve_work_branch_name(key, "", target)
    return GitManager.resolve_work_branch_name(key, source, target)


def _stored_refs(rows: List[Dict[str, str]], key: str) -> List[Dict[str, str]]:
    """Same rewrite the schedule API applies before GitManager sees the rows."""
    from src.dashboard.repo_sets import normalize_repository_refs

    described = refs_with_issue_key_branches(
        rows, work_branch_for_issue_key(key)
    )
    top_mode = rows[0].get("source_branch_mode") or "custom"
    top_src = rows[0].get("source_branch") or ""
    if top_mode == "issue_key":
        top_src = work_branch_for_issue_key(key)
    stored = normalize_repository_refs(
        rows[0]["url"],
        top_src,
        rows[0]["target_branch"],
        described,
    )
    assert stored, "schedule normalize dropped the repository list"
    stored[0]["source_branch"] = top_src
    stored[0]["target_branch"] = rows[0]["target_branch"]
    return stored


class _Prompt:
    """Just enough of JobProcessor to render the multi-repo prompt block."""

    def __init__(self, key: str, git: GitManager):
        self._contexts = {key: {"git": git}}

    _git_for = JobProcessor._git_for
    _with_repo_layout = JobProcessor._with_repo_layout


def _prepare_workspace(
    *,
    clones: Path,
    origins: Dict[str, Path],
    case: Dict[str, Any],
) -> GitManager:
    from src.dashboard import repo_sets as repo_sets_mod
    from src import issue_git_spec as git_spec

    count = int(case["repos"])
    key = case["key"]
    if case["same_leaf"]:
        chosen = ["left", "right", "svc-c"][:count]
    else:
        chosen = ["svc-a", "svc-b", "svc-c"][:count]
    rows = []
    for i, name in enumerate(chosen):
        mode = case["mode"][i]
        source = case["src"][i] if mode == "custom" else ""
        rows.append(
            {
                "url": origins[name].resolve().as_uri(),
                "source_branch": source,
                "target_branch": case["tgt"][i],
                "source_branch_mode": mode,
            }
        )
    original = git_spec._looks_like_git_url

    def _ok(url: str) -> bool:
        return _allow_file_url(url, original)

    # File remotes are not operator URLs. The patch only lets this test
    # walk the same normalize → clone path https remotes use in production.
    repo_sets_mod._looks_like_git_url = _ok
    git_spec._looks_like_git_url = _ok
    try:
        stored = _stored_refs(rows, key)
    finally:
        repo_sets_mod._looks_like_git_url = original
        git_spec._looks_like_git_url = original
    git = GitManager(
        issue_key=key,
        remote_url=stored[0]["url"],
        source_branch=stored[0]["source_branch"],
        target_branch=stored[0]["target_branch"],
        repository_refs=stored,
    )
    git.ensure_feature_branch(key)
    assert git.temp_dir is not None
    assert git.temp_dir.parent.resolve() == clones.resolve()
    return git


def _assert_case(
    git: GitManager,
    origins: Dict[str, Path],
    case: Dict[str, Any],
) -> None:
    key = case["key"]
    root = git.get_working_directory()
    assert root is not None
    assert root.name.startswith("multi_")
    assert not (root / ".git").exists()
    runner = AgentRunner(working_directory=root)
    assert Path(runner.working_directory) == root
    assert str(runner.working_directory) == str(root)
    children = list(git.repo_checkouts)
    assert len(children) == int(case["repos"])
    names = [child.temp_dir.name for child in children]
    assert len(names) == len(set(names))
    prompt = _Prompt(key, git)._with_repo_layout(key, "implement the ticket")
    assert "folder of clones" in prompt
    by_url = {child.remote_url: child for child in children}
    count = int(case["repos"])
    chosen = (
        ["left", "right", "svc-c"][:count]
        if case["same_leaf"]
        else ["svc-a", "svc-b", "svc-c"][:count]
    )
    seen_sha = []
    for i, name in enumerate(chosen):
        url = origins[name].resolve().as_uri()
        child = by_url[url]
        assert child.temp_dir.parent.resolve() == root.resolve()
        assert (child.temp_dir / ".git").is_dir()
        mode = case["mode"][i]
        source = case["src"][i]
        # A blank custom source is not issue-key mode. Schedule normalize
        # copies the first repository's source onto it.
        if i > 0 and mode == "custom" and not (source or "").strip():
            top_mode = case["mode"][0]
            inherited = (
                work_branch_for_issue_key(key)
                if top_mode == "issue_key"
                else case["src"][0]
            )
            work = GitManager.resolve_work_branch_name(key, inherited, case["tgt"][i])
        else:
            work = _intended(mode, source, case["tgt"][i], key)
        assert _branch(child.temp_dir) == work
        assert (child.work_branch or "") == work
        assert (child.target_branch or "") == case["tgt"][i]
        if _tip(origins[name], work):
            expect = _tip(origins[name], work)
        else:
            expect = _tip(origins[name], case["tgt"][i])
        assert expect
        assert _head(child.temp_dir) == expect
        seen_sha.append(expect)
        assert f"`{child.temp_dir.name}`" in prompt
        assert f"`{work}` → `{case['tgt'][i]}`" in prompt
    # Two remotes never share a commit, so a clone that used the other
    # repository's tip fails here even when the branch name looks right.
    assert len(seen_sha) == len(set(seen_sha))


def _allow_local_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let file:// remotes pass the host/PAT gates without rewriting them to HTTPS."""
    monkeypatch.setattr(
        GitManager, "_assert_remote_host_allowed", lambda self, url: None
    )
    monkeypatch.setattr(
        GitManager, "_pat_for_remote", lambda self, url="": "local-test-pat"
    )
    # A PAT plus a file:// URL would otherwise become https://file///...
    monkeypatch.setattr(
        GitManager, "_https_url_with_settings_pat", lambda self, url="": None
    )


@pytest.fixture(scope="module")
def origins(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("multi-origins")
    made = {
        name: _make_origin(root, name)
        for name in ("svc-a", "svc-b", "svc-c", "left", "right")
    }
    # left and right are both named api.git so the child folder must not collide.
    for name in ("left", "right"):
        src = made[name]
        dest_parent = root / name
        dest_parent.mkdir()
        dest = dest_parent / "api.git"
        _git(root, "clone", "--bare", str(src), str(dest))
        made[name] = dest
    return made


def test_multi_repo_matrix_checks_out_each_branch_from_its_own_remote(
    origins: Dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clones = tmp_path / "clones"
    clones.mkdir()
    monkeypatch.setattr(settings, "temp_dir_base", str(clones))
    _allow_local_git(monkeypatch)
    assert len(CASES) >= 200
    failures: List[str] = []
    for index, case in enumerate(CASES):
        git: Optional[GitManager] = None
        label = (
            f"#{index} key={case['key']} repos={case['repos']} "
            f"same_leaf={case['same_leaf']} "
            f"modes={case['mode'][: case['repos']]} "
            f"src={case['src'][: case['repos']]} "
            f"tgt={case['tgt'][: case['repos']]}"
        )
        try:
            git = _prepare_workspace(clones=clones, origins=origins, case=case)
            _assert_case(git, origins, case)
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
        finally:
            if git is not None:
                git.cleanup()
    assert not failures, f"{len(failures)} of {len(CASES)} failed\n" + "\n".join(
        failures[:25]
    )


def test_missing_target_on_one_repository_does_not_count_as_success(
    origins: Dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path))
    _allow_local_git(monkeypatch)
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    with pytest.raises((GitCloneError, GitSourceBranchError, Exception)):
        git = GitManager(
            issue_key="KAN-7",
            remote_url=url_a,
            source_branch="main",
            target_branch="main",
            repository_refs=[
                {"url": url_a, "source_branch": "", "target_branch": "main"},
                {
                    "url": url_b,
                    "source_branch": "",
                    "target_branch": "no-such-target",
                },
            ],
        )
        try:
            git.ensure_feature_branch("KAN-7")
        finally:
            git.cleanup()


def _workspace(
    tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    key: str,
    refs: List[Dict[str, str]],
    keep_source: bool = False,
) -> GitManager:
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp))
    _allow_local_git(monkeypatch)
    git = GitManager(
        issue_key=key,
        remote_url=refs[0]["url"],
        source_branch=refs[0]["source_branch"],
        target_branch=refs[0]["target_branch"],
        keep_source_work_branch=keep_source,
        repository_refs=refs,
    )
    git.ensure_feature_branch(key)
    for child in git.repo_checkouts:
        _git(child.temp_dir, "config", "user.email", "dev@example.com")
        _git(child.temp_dir, "config", "user.name", "Dev")
    return git


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"fix: {name}")
    return _head(repo)


def _processor(state_manager, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.jira_client = fake_jira
    proc.reporter.post_progress_update = lambda *args, **kwargs: None
    proc.reporter.post_error = lambda *args, **kwargs: None
    proc.reporter.post_completion = lambda *args, **kwargs: None
    return proc


@pytest.mark.asyncio
async def test_push_sends_each_repository_to_its_own_remote(
    origins, tmp_path, monkeypatch, state_manager, fake_jira
):
    key = "KAN-7"
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    git = _workspace(
        tmp_path / "push",
        monkeypatch,
        key=key,
        refs=[
            {"url": url_a, "source_branch": "", "target_branch": "main"},
            {
                "url": url_b,
                "source_branch": "feature/existing",
                "target_branch": "develop",
            },
        ],
    )
    try:
        root = git.get_working_directory()
        assert root is not None and not (root / ".git").exists()
        assert Path(AgentRunner(working_directory=root).working_directory) == root
        children = {child.remote_url: child for child in git.repo_checkouts}
        child_a = children[url_a]
        child_b = children[url_b]
        assert _branch(child_a.temp_dir) == "feature/KAN-7"
        assert _branch(child_b.temp_dir) == "feature/existing"
        assert _head(child_a.temp_dir) == _tip(origins["svc-a"], "main")
        assert _head(child_b.temp_dir) == _tip(origins["svc-b"], "feature/existing")
        assert _head(child_a.temp_dir) != _head(child_b.temp_dir)

        proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
        state_manager.create_state(key, "multi push", "d")
        state_manager.update_state(key, status=TaskStatus.EXECUTING)
        state = state_manager.get_state(key)
        proc._contexts[key] = {
            "git": git,
            "runner": AgentRunner(working_directory=root),
        }
        proc._snapshot_delivery_baseline(key, git)
        sha_b = _commit(child_b.temp_dir, "web.txt", "web change\n")
        # Drift the second clone off its work branch. Delivery must put it back.
        _git(child_b.temp_dir, "checkout", "develop")
        assert proc._head_moved_this_job(key) is True
        assert proc._assert_multi_repo_delivery(git, [child_a, child_b]) is None
        assert git.push() is False
        assert _tip(origins["svc-b"], "feature/existing") != sha_b

        calls: List[Dict[str, str]] = []

        def _mr(self, title: str, body: str = "", target_branch: Optional[str] = None):
            calls.append(
                {
                    "remote": self.remote_url or "",
                    "branch": self.work_branch or "",
                    "target": target_branch or "",
                    "sha": self.get_last_commit_sha() or "",
                }
            )
            return f"https://gitlab.example/{self.temp_dir.name}/-/merge_requests/1"

        monkeypatch.setattr(GitManager, "create_merge_request", _mr)
        ok = await proc._push_and_create_mr(state, open_mr=True)
        assert ok is True
        assert _tip(origins["svc-a"], "feature/KAN-7") == _tip(origins["svc-a"], "main")
        assert _tip(origins["svc-b"], "feature/existing") == sha_b
        # The api commit must not land on the web remote, or the reverse.
        assert _tip(origins["svc-a"], "feature/existing") != sha_b
        assert len(calls) == 1
        assert calls[0]["remote"] == url_b
        assert calls[0]["branch"] == "feature/existing"
        assert calls[0]["target"] == "develop"
        assert calls[0]["sha"] == sha_b
        saved = state_manager.get_state(key)
        history = list((saved.metadata or {}).get("git_deliveries") or [])
        urls = {row.get("repository_url") for row in history}
        assert url_b in urls
        match = [row for row in history if row.get("repository_url") == url_b]
        assert match[-1]["feature_branch"] == "feature/existing"
        assert match[-1]["commit_sha"] == sha_b
        assert match[-1]["target_branch"] == "develop"
    finally:
        git.cleanup()


@pytest.mark.asyncio
async def test_review_followup_pushes_only_the_matching_repository(
    origins, tmp_path, monkeypatch, state_manager, fake_jira
):
    key = "KAN-42"
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    git = _workspace(
        tmp_path / "follow",
        monkeypatch,
        key=key,
        refs=[
            {"url": url_a, "source_branch": "", "target_branch": "main"},
            {"url": url_b, "source_branch": "", "target_branch": "develop"},
        ],
    )
    try:
        proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
        state_manager.create_state(key, "follow", "d")
        state_manager.update_state(key, status=TaskStatus.EXECUTING)
        state = state_manager.get_state(key)
        proc._contexts[key] = {"git": git, "runner": None}
        children = {child.remote_url: child for child in git.repo_checkouts}
        sha_a = _commit(children[url_a].temp_dir, "a.txt", "api\n")
        sha_b = _commit(children[url_b].temp_dir, "b.txt", "web\n")
        calls: List[str] = []

        def _mr(self, title: str, body: str = "", target_branch: Optional[str] = None):
            calls.append(self.remote_url or "")
            return f"{self.remote_url}/-/merge_requests/9"

        monkeypatch.setattr(GitManager, "create_merge_request", _mr)
        review = f"{url_b}/-/merge_requests/4"
        ok = await proc._push_and_create_mr(
            state, existing_mr_url=review, open_mr=True
        )
        assert ok is True
        # Reuse the review URL. Do not open a second merge request.
        assert calls == []
        assert _tip(origins["svc-a"], "feature/KAN-42") != sha_a
        assert _tip(origins["svc-b"], "feature/KAN-42") == sha_b
        saved = state_manager.get_state(key)
        history = [
            row
            for row in ((saved.metadata or {}).get("git_deliveries") or [])
            if row.get("repository_url") == url_b
        ]
        assert history[-1]["merge_request_url"] == review
        assert history[-1]["commit_sha"] == sha_b
    finally:
        git.cleanup()


@pytest.mark.asyncio
async def test_one_rejected_push_still_pushes_the_other_repository(
    origins, tmp_path, monkeypatch, state_manager, fake_jira
):
    key = "AZ-3"
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    git = _workspace(
        tmp_path / "partial",
        monkeypatch,
        key=key,
        refs=[
            {"url": url_a, "source_branch": "", "target_branch": "main"},
            {"url": url_b, "source_branch": "", "target_branch": "main"},
        ],
    )
    try:
        proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
        state_manager.create_state(key, "partial", "d")
        state_manager.update_state(key, status=TaskStatus.EXECUTING)
        state = state_manager.get_state(key)
        proc._contexts[key] = {"git": git, "runner": None}
        children = {child.remote_url: child for child in git.repo_checkouts}
        _commit(children[url_a].temp_dir, "a.txt", "api\n")
        sha_b = _commit(children[url_b].temp_dir, "b.txt", "web\n")
        # Push rewrites origin from remote_url, so a set-url alone is undone.
        missing = tmp_path / "missing.git"
        children[url_a].remote_url = missing.resolve().as_uri()
        monkeypatch.setattr(
            GitManager,
            "create_merge_request",
            lambda self, title, body="", target_branch=None: "https://example.test/mr/1",
        )
        ok = await proc._push_and_create_mr(state, open_mr=True)
        assert ok is False
        assert _tip(origins["svc-b"], "feature/AZ-3") == sha_b
    finally:
        git.cleanup()


def test_followup_on_feature_branch_does_not_checkout_develop(
    origins, tmp_path, monkeypatch, state_manager, fake_jira
):
    """A ticket whose source is develop still works on feature/KEY.

    The merge-request follow-up names that feature branch. Checkout has to
    stay there, in the same multi-repo folder, including the other repository.
    """
    key = "KAN-7"
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    refs = [
        {"url": url_a, "source_branch": "develop", "target_branch": "main"},
        {"url": url_b, "source_branch": "develop", "target_branch": "main"},
    ]
    monkeypatch.setattr(settings, "temp_dir_base", str(tmp_path / "follow-dev"))
    _allow_local_git(monkeypatch)
    build = GitManager(
        issue_key=key,
        remote_url=url_a,
        source_branch="develop",
        target_branch="main",
        repository_refs=refs,
    )
    try:
        state_manager.create_state(key, "follow develop", "d")
        state_manager.update_state(
            key,
            metadata={"repository_urls": [url_a, url_b], "repository_refs": refs},
        )
        proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
        follow = proc._init_git_manager(
            key,
            state_manager.get_state(key),
            repository_url=url_a,
            source_branch="feature/KAN-7",
            target_branch="main",
            keep_source_work_branch=True,
        )
        assert follow is not None
        try:
            follow.ensure_feature_branch(key)
            by_url = {child.remote_url: child for child in follow.repo_checkouts}
            assert _branch(by_url[url_a].temp_dir) == "feature/KAN-7"
            assert _branch(by_url[url_b].temp_dir) == "feature/KAN-7"
            assert follow.temp_dir is not None and build.temp_dir is not None
            assert follow.temp_dir.name == build.temp_dir.name
            root = follow.get_working_directory()
            assert root is not None and not (root / ".git").exists()
            assert Path(AgentRunner(working_directory=root).working_directory) == root
        finally:
            follow.cleanup()
    finally:
        build.cleanup()


def test_mr_state_webhook_keeps_the_other_repository_delivery(
    tmp_path, monkeypatch, state_manager, fake_jira
):
    """An open-hook that read the job early must not erase the other MR."""
    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    api = "https://gitlab.com/acme/api.git"
    web = "https://gitlab.com/acme/web.git"
    api_mr = "https://gitlab.com/acme/api/-/merge_requests/4"
    web_mr = "https://gitlab.com/acme/web/-/merge_requests/9"
    created = proc.job_store.create_job(
        issue_key="KAN-12",
        summary="multi",
        workflow_type="execution",
        status="completed",
        repository_url=api,
        merge_request_url=api_mr,
        gitlab_project="acme/api",
        gitlab_mr_iid=4,
    )
    assert created is not None
    job_id = created["job_id"]
    both = [
        {
            "repository_url": api,
            "feature_branch": "feature/KAN-12",
            "target_branch": "main",
            "merge_request_url": api_mr,
            "commit_sha": "a" * 40,
        },
        {
            "repository_url": web,
            "feature_branch": "feature/KAN-12",
            "target_branch": "main",
            "merge_request_url": web_mr,
            "commit_sha": "b" * 40,
        },
    ]
    assert proc.job_store.update_job(job_id, deliveries=both) is not None
    stale = dict(proc.job_store.get_job(job_id) or {})
    stale["deliveries"] = [dict(both[0])]
    stale["merge_request_url"] = api_mr
    monkeypatch.setattr(proc.job_store, "list_jobs", lambda *args, **kwargs: [stale])
    proc._record_merge_request_state(
        issue_key="KAN-12",
        mr_url=api_mr,
        project_path="acme/api",
        mr_iid=4,
        state="opened",
    )
    saved = proc.job_store.get_job(job_id) or {}
    urls = {
        row.get("repository_url")
        for row in (saved.get("deliveries") or [])
        if isinstance(row, dict)
    }
    assert web in urls
    assert api in urls


def test_followup_keep_source_checks_out_each_repo_branch(
    origins, tmp_path, monkeypatch
):
    """A real work branch stays. A stored primary base becomes feature/{KEY}."""
    key = "KAN-7"
    url_a = origins["svc-a"].resolve().as_uri()
    url_b = origins["svc-b"].resolve().as_uri()
    git = _workspace(
        tmp_path / "keep",
        monkeypatch,
        key=key,
        keep_source=True,
        refs=[
            {
                "url": url_a,
                "source_branch": "feature/existing",
                "target_branch": "main",
            },
            {"url": url_b, "source_branch": "develop", "target_branch": "main"},
        ],
    )
    try:
        children = {child.remote_url: child for child in git.repo_checkouts}
        assert _branch(children[url_a].temp_dir) == "feature/existing"
        assert _head(children[url_a].temp_dir) == _tip(
            origins["svc-a"], "feature/existing"
        )
        # develop is ticket text. keep_source still cuts feature/KAN-7 from main
        # when that branch is not on the remote yet.
        assert _branch(children[url_b].temp_dir) == "feature/KAN-7"
        assert _head(children[url_b].temp_dir) == _tip(origins["svc-b"], "main")
        root = git.get_working_directory()
        assert root is not None and root.name.startswith("multi_")
        assert not (root / ".git").exists()
    finally:
        git.cleanup()
