"""Hermetic e2e: GitLab/Azure review from webhook to overview + inline findings."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.azure.webhook import decide_azure_comment_webhook
from src.gitlab.webhook import decide_gitlab_note_webhook, decide_gitlab_mr_webhook
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_azure_webhook import _pr_comment_payload
from tests.test_gitlab_webhook import _mr_lifecycle_payload, _mr_payload

_FINDINGS = """### Özet
1 Critical.

```opencoderman-findings
{
  "findings": [
    {
      "path": "src/buf.cpp",
      "start_line": 2,
      "end_line": 2,
      "side": "new",
      "severity": "critical",
      "title": "overflow",
      "body": "unbounded copy"
    }
  ]
}
```
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _seed_clone(root: Path) -> Path:
    clone = root / "clone"
    clone.mkdir()
    _git(clone, "init")
    _git(clone, "config", "user.email", "e2e@example.com")
    _git(clone, "config", "user.name", "E2E")
    _git(clone, "checkout", "-B", "develop")
    (clone / "src").mkdir()
    (clone / "src" / "buf.cpp").write_text("int n = 0;\nreturn n;\n", encoding="utf-8")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "chore: seed")
    _git(clone, "checkout", "-B", "feature/login")
    (clone / "src" / "buf.cpp").write_text(
        "int n = 0;\nstrcpy(dest, src);\nreturn n;\n", encoding="utf-8"
    )
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "feat: add copy")
    return clone


def _git_mock(clone: Path) -> MagicMock:
    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/demo.git"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = "abc123deadbeef"
    git.commits_ahead_of_target.return_value = 0
    git.push.return_value = True
    return git


def _runner(tmp_path: Path, stdout: str = _FINDINGS) -> MagicMock:
    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": stdout,
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_rev_e2e",
        }
    )
    return runner


@pytest.mark.asyncio
async def test_e2e_gitlab_review_posts_overview_and_inline_finding(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    clone = _seed_clone(tmp_path)
    git = _git_mock(clone)
    runner = _runner(tmp_path)
    notes: list[dict] = []
    discussions: list[dict] = []

    def fake_note(self, **kwargs):
        notes.append(kwargs)
        return {"id": 99, "discussion_id": "dn1"}

    def fake_discussion(self, **kwargs):
        discussions.append(kwargs)
        return {"id": "d-find"}

    decision = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai /review the auth change"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert decision.accepted is True
    assert decision.event.command == "review"

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    def fake_init(*_a, **_k):
        proc._contexts[decision.event.issue_key] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch.object(proc, "_push_and_create_mr", new_callable=AsyncMock) as push_mr, patch(
        "src.gitlab.client.GitlabClient.post_mr_note", fake_note
    ), patch(
        "src.gitlab.client.GitlabClient.post_mr_discussion", fake_discussion
    ), patch(
        "src.gitlab.client.GitlabClient.get_mr_diff_refs",
        return_value=("", "", ""),
    ), patch(
        "src.gitlab.client.GitlabClient.list_mr_discussions", return_value=[]
    ):
        await proc.handle_gitlab_mr_comment(decision.event)

    st = sm.get_state(decision.event.issue_key)
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert (st.metadata or {}).get("workflow_type") == "gitlab-review"
    assert (st.metadata or {}).get("review_findings_posted") == 1
    push_mr.assert_not_awaited()
    git.push.assert_not_called()

    task = runner.run_agent_with_retry.await_args.args[0]
    assert task.agent == "derman-reviewer"
    assert "derman-reviewer" in task.prompt
    assert "## Review delivery" in task.prompt
    assert "full review" in task.prompt.lower()

    assert notes, "overview note missing"
    overview = notes[0].get("body") or ""
    assert overview.startswith("**Yaver ")
    assert "— Review**" in overview
    assert "opencoderman-findings" not in overview
    assert "Özet" in overview

    assert len(discussions) == 1
    finding = discussions[0]
    assert "yaver-finding" in (finding.get("body") or "")
    assert finding["position"]["new_path"] == "src/buf.cpp"
    assert finding["position"]["new_line"] == 2

    jobs = isolate_jira_agent_artifacts["job_store"].list_jobs(
        issue_key=decision.event.issue_key
    )
    assert jobs
    assert jobs[0].get("workflow_type") == "gitlab-review"


@pytest.mark.asyncio
async def test_e2e_azure_ask_replies_without_finding_threads(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    clone = _seed_clone(tmp_path)
    git = _git_mock(clone)
    git.remote_url = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
    runner = _runner(tmp_path)
    posted: list[dict] = []

    def fake_post(self, **kwargs):
        posted.append(kwargs)
        return {"id": 101}

    decision = decide_azure_comment_webhook(
        _pr_comment_payload(note="@yaver /ask why is strcpy used?"),
        headers={},
        secret="",
        bot_mentions=["@yaver"],
    )
    assert decision.accepted is True
    assert decision.event.command == "ask"

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    def fake_init(*_a, **_k):
        proc._contexts[decision.event.issue_key] = {"git": git, "runner": runner}
        return git

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch.object(proc, "_push_and_create_mr", new_callable=AsyncMock) as push_mr, patch(
        "src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post
    ), patch(
        "src.azure.client.AzureDevOpsClient.post_pr_file_thread",
        return_value={"id": 9},
    ) as file_thread:
        await proc.handle_azure_pr_comment(decision.event)

    st = sm.get_state(decision.event.issue_key)
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert (st.metadata or {}).get("workflow_type") == "azure-review"
    push_mr.assert_not_awaited()
    file_thread.assert_not_called()
    assert posted
    body = posted[0].get("body") or ""
    assert "— Review**" in body
    assert "opencoderman-findings" not in body
    task = runner.run_agent_with_retry.await_args.args[0]
    assert task.agent == "derman-reviewer"
    assert "follow-up" in task.prompt.lower()


def test_e2e_gitlab_assign_starts_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(action="update", state="opened")
    payload["changes"] = {
        "reviewers": {
            "previous": [],
            "current": [{"id": 9, "username": "berat_ai"}],
        }
    }
    d = decide_gitlab_mr_webhook(
        payload,
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.event is not None
    assert d.event.start_review is True


def test_e2e_http_review_and_ask_enqueue(fake_jira, monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": "KAN-12"}
    )
    proc.enqueue_azure_comment = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": "KAN-12"}
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        gl = client.post(
            "/yaver/webhook/gitlab",
            json=_mr_payload(note="@berat_ai /review the diff"),
            headers={"X-Gitlab-Token": "tok", "X-Gitlab-Event": "Note Hook"},
        )
        az = client.post(
            "/yaver/webhook/azure",
            json=_pr_comment_payload(note="@yaver /ask what is this?"),
        )
    assert gl.status_code == 200 and gl.json().get("ok") is True
    assert az.status_code == 200 and az.json().get("ok") is True
    proc.enqueue_gitlab_note.assert_called_once()
    proc.enqueue_azure_comment.assert_called_once()
