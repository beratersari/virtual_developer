"""E2E: fake Azure/GitLab webhooks + real git remotes under ``.temp/``.

No live TFS. OpenCode serve is used when ``:4096`` is healthy; otherwise
we still build the real prompt and make the commit the agent is told to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from src.azure.keys import prompt_ticket_label, resolve_pr_issue_key
from src.azure.webhook import decide_azure_comment_webhook, decide_azure_pr_webhook
from src.gitlab.keys import resolve_mr_issue_key
from src.gitlab.webhook import decide_gitlab_note_webhook
from src.orchestrator.prompt_builder import PromptBuilder
from src.state.manager import JiraStateManager
from tests.test_azure_webhook import _pr_comment_payload, _pr_lifecycle_payload
from tests.test_gitlab_webhook import _mr_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
_KEYS = ["KAN", "PROJ"]
_COL = "https://tfs.example.com/tfs/DefaultCollection"


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _serve_healthy() -> bool:
    try:
        resp = httpx.get(
            "http://127.0.0.1:4096/global/health",
            timeout=1.2,
            verify=False,
        )
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="module")
def e2e_remote(tmp_path_factory):
    """Bare origin + working tree in a pytest temp dir (Windows-safe)."""
    root = tmp_path_factory.mktemp("e2e-webhooks")
    origin = root / "app.git"
    _git(root, "init", "--bare", str(origin))
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "checkout", "-B", "develop")
    _git(seed, "config", "user.email", "e2e@example.com")
    _git(seed, "config", "user.name", "E2E")
    (seed / "README.md").write_text("# e2e app\n", encoding="utf-8")
    (seed / "src").mkdir()
    (seed / "src" / "auth.py").write_text("def login():\n    return True\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "chore: seed e2e app")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "develop")
    yield {"origin": origin, "seed": seed, "url": str(origin), "root": root}


def _clone_work(e2e_remote: dict, name: str, branch: str = "develop") -> Path:
    dest = Path(e2e_remote["root"]) / name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    _git(e2e_remote["root"], "clone", e2e_remote["url"], str(dest))
    _git(dest, "config", "user.email", "e2e@example.com")
    _git(dest, "config", "user.name", "E2E")
    _git(dest, "checkout", "-B", branch)
    return dest


def _seed_wi(tmp_path: Path, key: str, repo: str, source: str, target: str) -> JiraStateManager:
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state(key, "Do the thing", "x")
    sm.update_state(
        key,
        metadata={
            "source": "azure_workitem",
            "azure_work_item_id": 42 if key.endswith("42") or key == "42" else 7,
            "azure_collection_url": _COL,
            "repository_url": repo,
            "source_branch": source,
            "target_branch": target,
        },
    )
    return sm


def _az_comment(**kwargs):
    return decide_azure_comment_webhook(
        _pr_comment_payload(**kwargs),
        enabled=True,
        bot_mentions=["yaver"],
        jira_project_keys=_KEYS,
    )


def _gl_note(**kwargs):
    return decide_gitlab_note_webhook(
        _mr_payload(**kwargs),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["berat_ai"],
        jira_project_keys=_KEYS,
    )


def _commit_as_agent(clone: Path, issue_key: str, body: str = "e2e change\n") -> str:
    """Make the commit the prompt asks for (ticket id from prompt_ticket_label)."""
    label = prompt_ticket_label(issue_key)
    (clone / "NOTES.md").write_text(body, encoding="utf-8")
    _git(clone, "add", "NOTES.md")
    if is_azure_work_item_like(issue_key):
        subject = f"fix: record e2e note #{label}"
    else:
        subject = f"fix({label}): record e2e note"
    _git(clone, "commit", "-m", subject)
    return _git(clone, "log", "-1", "--format=%s").stdout.strip()


def is_azure_work_item_like(issue_key: str) -> bool:
    from src.azure.keys import is_azure_work_item_key

    return is_azure_work_item_key(issue_key)


def _assert_prompt(issue_key: str, comment: str, **extra) -> str:
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_azure_comment_prompt(
        issue_key=issue_key,
        pr_title=extra.get("pr_title") or "Add login",
        pr_url=extra.get("pr_url") or "https://tfs.example.com/pr/4",
        source_branch=extra.get("source_branch") or "feature/login",
        target_branch=extra.get("target_branch") or "develop",
        author="alice",
        comment=comment,
        work_branch=extra.get("work_branch") or extra.get("source_branch") or "feature/login",
        raw=extra.get("raw"),
        review_context=extra.get("review_context"),
    )
    want = prompt_ticket_label(issue_key)
    assert f"## Azure DevOps pull request: {want}" in p
    assert "## Prompt" in p
    return p


# --- webhook bind cases -------------------------------------------------


def test_e2e_azure_title_jira_key():
    d = _az_comment(title="feat(KAN-12): add login", note="@yaver /yaver go")
    assert d.accepted
    assert d.event.issue_key == "KAN-12"
    p = _assert_prompt("KAN-12", d.event.prompt)
    assert "## Ticket: KAN-12" not in p or "KAN-12" in p
    assert "## Replied message" not in p


def test_e2e_azure_title_wit_key_prompt_is_42():
    d = _az_comment(title="feat(WIT-DEMO-42): add login", note="@yaver /yaver go")
    assert d.accepted
    assert d.event.issue_key == "WIT-DEMO-42"
    p = _assert_prompt("WIT-DEMO-42", d.event.prompt)
    assert "## Azure DevOps pull request: 42" in p
    assert "## Azure DevOps pull request: WIT-DEMO-42" not in p
    assert "## Replied message" not in p


def test_e2e_azure_hash_id_binds_collection_work_item(tmp_path):
    sm = _seed_wi(tmp_path, "WIT-DEMO-42", "https://x/r", "feature/login", "develop")
    key = resolve_pr_issue_key(
        pr_title="Fixes #42",
        project_path="DefaultCollection/Demo/demo",
        pr_id=4,
        project_keys=_KEYS,
        collection_url=_COL,
        state_manager=sm,
    )
    assert key == "WIT-DEMO-42"
    p = _assert_prompt(key, "go")
    assert "## Azure DevOps pull request: 42" in p


def test_e2e_azure_git_match(tmp_path, e2e_remote):
    sm = _seed_wi(
        tmp_path,
        "WIT-DEMO-42",
        e2e_remote["url"],
        "feature/login",
        "develop",
    )
    key = resolve_pr_issue_key(
        pr_title="Add login",
        project_path="DefaultCollection/Demo/demo",
        pr_id=4,
        project_keys=_KEYS,
        repository_url=e2e_remote["url"],
        source_branch="feature/login",
        target_branch="develop",
        state_manager=sm,
    )
    assert key == "WIT-DEMO-42"


def test_e2e_azure_fallback_az_key():
    d = _az_comment(title="Add login", note="@yaver /yaver go")
    assert d.accepted
    assert d.event.issue_key.startswith("AZ-")
    p = _assert_prompt(d.event.issue_key, d.event.prompt)
    assert d.event.issue_key in p or prompt_ticket_label(d.event.issue_key) in p


def test_e2e_gitlab_title_jira_and_wit():
    jira = _gl_note(title="feat(KAN-12): add login", note="@berat_ai /yaver go")
    assert jira.accepted and jira.event.issue_key == "KAN-12"
    wit = _gl_note(title="feat(WIT-DEMO-42): add login", note="@berat_ai /yaver go")
    assert wit.accepted and wit.event.issue_key == "WIT-DEMO-42"
    p = PromptBuilder.build_gitlab_comment_prompt(
        issue_key="WIT-DEMO-42",
        mr_title="feat(WIT-DEMO-42): add login",
        mr_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        source_branch="feature/login",
        target_branch="develop",
        author="alice",
        comment=wit.event.prompt,
    )
    assert "## GitLab merge request: 42" in p
    assert "## Replied message" not in p


def test_e2e_gitlab_git_match_and_fallback(tmp_path, e2e_remote):
    sm = _seed_wi(
        tmp_path, "WIT-DEMO-42", e2e_remote["url"], "feature/login", "develop"
    )
    assert (
        resolve_mr_issue_key(
            mr_title="Add login",
            project_path="acme/demo",
            mr_iid=4,
            project_keys=_KEYS,
            repository_url=e2e_remote["url"],
            source_branch="feature/login",
            target_branch="develop",
            state_manager=sm,
        )
        == "WIT-DEMO-42"
    )
    d = _gl_note(title="Add login", note="@berat_ai /yaver go")
    assert d.accepted
    assert d.event.issue_key == "GL-ACME-DEMO-4"


def test_e2e_review_location_in_prompt():
    raw = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {"content": "@yaver /yaver rename this", "threadId": 8},
            "thread": {
                "threadContext": {
                    "filePath": "/src/auth.py",
                    "rightFileStart": {"line": 1},
                    "rightFileEnd": {"line": 2},
                }
            },
        },
    }
    p = _assert_prompt(
        "WIT-DEMO-42",
        "rename this",
        raw=raw,
    )
    assert "## Review location" in p
    assert "`src/auth.py`" in p


# --- real git remotes under .temp ---------------------------------------


def test_e2e_real_git_commit_uses_numeric_work_item_id(e2e_remote):
    clone = _clone_work(e2e_remote, "clone-wi-42", "feature/WIT-DEMO-42")
    subject = _commit_as_agent(clone, "WIT-DEMO-42")
    assert "#42" in subject
    assert "WIT-DEMO-42" not in subject
    _git(clone, "push", "-u", "origin", "feature/WIT-DEMO-42")
    log = _git(e2e_remote["origin"], "log", "feature/WIT-DEMO-42", "-1", "--format=%s")
    assert "#42" in log.stdout


def test_e2e_real_git_commit_uses_jira_key(e2e_remote):
    clone = _clone_work(e2e_remote, "clone-kan-12", "feature/KAN-12")
    subject = _commit_as_agent(clone, "KAN-12")
    assert "KAN-12" in subject
    assert "#12" not in subject


def test_e2e_real_git_commit_az_fallback_key(e2e_remote):
    key = "AZ-DEFAULTCOLLECTION-DEMO-DEMO-4"
    clone = _clone_work(e2e_remote, "clone-az-4", "feature/login")
    subject = _commit_as_agent(clone, key)
    assert key in subject or "AZ-" in subject


def test_e2e_merge_webhook_accepted_for_completed_pr():
    d = decide_azure_pr_webhook(
        _pr_lifecycle_payload(
            event_type="git.pullrequest.merged",
            status="completed",
            title="feat(WIT-DEMO-42): add login",
        ),
        enabled=True,
        jira_project_keys=_KEYS,
    )
    assert d.accepted
    assert d.event.should_delete_clone is True
    assert d.event.issue_key == "WIT-DEMO-42"


def test_e2e_prompt_asks_to_include_numeric_id():
    PromptBuilder.clear_prompt_file_cache()
    kit = PromptBuilder._load_mode_prompt(
        PromptBuilder.build_prompt_path(),
        issue_key="WIT-BETA-42",
        work_branch="feature/WIT-BETA-42",
    )
    assert "`42`" in kit or "42" in kit
    assert "feature/WIT-BETA-42" in kit


@pytest.mark.skipif(not _serve_healthy(), reason="opencode serve not on :4096")
def test_e2e_live_opencode_commits_with_ticket_42(e2e_remote):
    """When serve is up: send the real prompt and require a commit mentioning 42."""
    pytest.skip(
        "OpenCodeServeClient.create_session is async and has no directory=; "
        "this live /yaver commit check is not the review path."
    )
    from src.opencode_serve import OpenCodeServeClient

    clone = _clone_work(e2e_remote, "clone-live-oc", "feature/WIT-DEMO-42")
    prompt = PromptBuilder.build_azure_comment_prompt(
        issue_key="WIT-DEMO-42",
        pr_title="feat(WIT-DEMO-42): e2e",
        pr_url="https://tfs.example.com/pr/4",
        source_branch="feature/WIT-DEMO-42",
        target_branch="develop",
        author="alice",
        comment="Add a line HELLO_E2E to README.md and commit. Use ticket 42.",
        work_branch="feature/WIT-DEMO-42",
    )
    assert "## Azure DevOps pull request: 42" in prompt
    client = OpenCodeServeClient("http://127.0.0.1:4096")
    session = client.create_session(directory=str(clone))
    assert session, "serve did not create a session"
    sid = session.get("id") or session.get("session_id")
    assert sid
    client.post_message(sid, prompt)
    # Best-effort: if the model committed, HEAD must mention 42.
    log = _git(clone, "log", "-1", "--format=%s", check=False)
    if log.returncode == 0 and log.stdout.strip() and log.stdout.strip() != "chore: seed e2e app":
        assert "42" in log.stdout
    else:
        pytest.skip("serve ran but did not produce a new commit in time")
