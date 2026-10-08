"""Agent summary is posted when a merge request or pull request is opened."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus


MR_URL = "https://gitlab.example.com/acme/app/-/merge_requests/12"
PR_URL = "https://tfs.example.com/tfs/Col/Proj/_git/Repo/pullrequest/44"
SUMMARY = "Fixed the login timeout after the token refresh."


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _ready_git(url: str = MR_URL) -> MagicMock:
    git = MagicMock()
    git.repo_checkouts = []
    git.work_branch = "feature/KAN-1"
    git.target_branch = "main"
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.ensure_on_work_branch.return_value = True
    git.get_current_branch.return_value = "feature/KAN-1"
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.head_is_on_remote.return_value = True
    git.get_last_commit_subject.return_value = "fix(auth): refresh the login token"
    git.get_last_commit_message.return_value = "fix(auth): refresh the login token"
    git.get_last_commit_sha.return_value = "abc123def456"
    git.build_commit_url.return_value = "https://gitlab.example.com/acme/app/-/commit/abc123def456"
    git.create_merge_request.return_value = url
    return git


def _bind(processor, state, git: MagicMock) -> None:
    processor._contexts[state.issue_key] = {"git": git, "runner": None}


@pytest.mark.asyncio
async def test_jira_job_puts_summary_on_the_opened_mr(processor, state_manager):
    """A Jira issue that opens an MR posts the agent summary on that MR."""
    state = state_manager.create_state("KAN-1", "login", "d")
    git = _ready_git()
    _bind(processor, state, git)
    notes: list[dict] = []

    def _note(**kwargs):
        notes.append(kwargs)
        return {"id": 7}

    raw = f"[serve] poll 3 idle\n{SUMMARY}\n"
    with patch("src.gitlab.client.GitlabClient.post_mr_note", side_effect=_note):
        ok = await processor._push_and_create_mr(state, agent_summary=raw)

    assert ok is True
    body = git.create_merge_request.call_args.kwargs["body"]
    assert SUMMARY in body
    assert "[serve]" not in body
    assert len(notes) == 1
    assert notes[0]["project"] == "acme/app"
    assert notes[0]["mr_iid"] == 12
    assert SUMMARY in notes[0]["body"]
    assert "[serve]" not in notes[0]["body"]


@pytest.mark.asyncio
async def test_jira_reused_mr_still_posts_the_summary(processor, state_manager):
    """A second Jira run reuses the open MR and still posts this run's summary."""
    state = state_manager.create_state("KAN-2", "login", "d")
    git = _ready_git()
    _bind(processor, state, git)
    notes: list[dict] = []

    def _note(**kwargs):
        notes.append(kwargs)
        return {"id": 8}

    with patch("src.gitlab.client.GitlabClient.post_mr_note", side_effect=_note):
        ok = await processor._push_and_create_mr(
            state,
            existing_mr_url=MR_URL,
            agent_summary=SUMMARY,
        )

    assert ok is True
    git.create_merge_request.assert_not_called()
    assert len(notes) == 1
    assert SUMMARY in notes[0]["body"]
    assert notes[0]["mr_iid"] == 12


@pytest.mark.asyncio
async def test_jira_opened_azure_pr_posts_the_summary(processor, state_manager):
    state = state_manager.create_state("KAN-3", "login", "d")
    git = _ready_git(PR_URL)
    _bind(processor, state, git)
    comments: list[dict] = []

    def _comment(**kwargs):
        comments.append(kwargs)
        return {"id": 9}

    with patch("src.azure.client.AzureDevOpsClient.post_pr_comment", side_effect=_comment):
        ok = await processor._push_and_create_mr(state, agent_summary=SUMMARY)

    assert ok is True
    assert SUMMARY in git.create_merge_request.call_args.kwargs["body"]
    assert len(comments) == 1
    assert comments[0]["project"] == "Proj"
    assert comments[0]["repository"] == "Repo"
    assert comments[0]["pr_id"] == 44
    assert SUMMARY in comments[0]["body"]


@pytest.mark.asyncio
async def test_zero_ahead_does_not_open_an_mr_or_post_a_summary(
    processor, state_manager
):
    state = state_manager.create_state("KAN-4", "login", "d")
    git = _ready_git()
    git.commits_ahead_of_target.return_value = 0
    _bind(processor, state, git)

    with patch("src.gitlab.client.GitlabClient.post_mr_note") as note:
        ok = await processor._push_and_create_mr(state, agent_summary=SUMMARY)

    assert ok is True
    git.create_merge_request.assert_not_called()
    note.assert_not_called()


@pytest.mark.asyncio
async def test_empty_summary_keeps_the_commit_message(processor, state_manager):
    state = state_manager.create_state("KAN-5", "login", "d")
    git = _ready_git()
    _bind(processor, state, git)

    with patch("src.gitlab.client.GitlabClient.post_mr_note") as note:
        ok = await processor._push_and_create_mr(state, agent_summary="[serve] idle\n")

    assert ok is True
    body = git.create_merge_request.call_args.kwargs["body"]
    assert body == "fix(auth): refresh the login token"
    note.assert_not_called()


@pytest.mark.asyncio
async def test_gitlab_comment_job_does_not_post_a_second_summary_note(
    processor, state_manager
):
    """MR comment jobs already reply after push. Do not add the same note twice."""
    state = state_manager.create_state("KAN-6", "login", "d")
    state_manager.update_state(
        "KAN-6",
        metadata={"source": "gitlab", "workflow_type": "gitlab_mr"},
    )
    state = state_manager.get_state("KAN-6")
    git = _ready_git()
    _bind(processor, state, git)

    with patch("src.gitlab.client.GitlabClient.post_mr_note") as note:
        ok = await processor._push_and_create_mr(state, agent_summary=SUMMARY)

    assert ok is True
    assert SUMMARY in git.create_merge_request.call_args.kwargs["body"]
    note.assert_not_called()


@pytest.mark.asyncio
async def test_note_failure_still_records_the_opened_mr(processor, state_manager):
    state = state_manager.create_state("KAN-8", "login", "d")
    git = _ready_git()
    _bind(processor, state, git)

    with patch(
        "src.gitlab.client.GitlabClient.post_mr_note",
        side_effect=RuntimeError("gitlab down"),
    ):
        ok = await processor._push_and_create_mr(state, agent_summary=SUMMARY)

    assert ok is True
    recorded = (state_manager.get_state("KAN-8").metadata or {}).get("merge_request_url")
    assert recorded == MR_URL


@pytest.mark.asyncio
async def test_multi_repo_jira_job_posts_the_summary_on_each_mr(
    processor, state_manager
):
    state = state_manager.create_state("KAN-9", "login", "d")
    parent = MagicMock()
    children = []
    for name, iid in (("orders-api", 11), ("orders-web", 8)):
        child = _ready_git(
            f"https://gitlab.example.com/acme/{name}/-/merge_requests/{iid}"
        )
        child.work_branch = "feature/KAN-9"
        child.get_current_branch.return_value = "feature/KAN-9"
        child.remote_url = f"https://gitlab.example.com/acme/{name}.git"
        child.repo_checkouts = []
        children.append(child)
    parent.repo_checkouts = children
    _bind(processor, state, parent)
    notes: list[dict] = []

    def _note(**kwargs):
        notes.append(kwargs)
        return {"id": 1}

    with patch("src.gitlab.client.GitlabClient.post_mr_note", side_effect=_note):
        ok = await processor._push_and_create_mr(state, agent_summary=SUMMARY)

    assert ok is True
    assert len(notes) == 2
    posted = {row["mr_iid"] for row in notes}
    assert posted == {11, 8}
    for child in children:
        assert SUMMARY in child.create_merge_request.call_args.kwargs["body"]
    for row in notes:
        assert SUMMARY in row["body"]


def _mock_git_and_agent(processor, tmp_path, returncode=0, stdout="done", stderr=""):
    git = MagicMock()
    git.repo_checkouts = []
    git.ensure_feature_branch.return_value = "feature/X-1"
    git.work_branch = "feature/X-1"
    git.target_branch = "develop"
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/X-1"
    git.ensure_on_work_branch.return_value = True
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "feat: x"
    git.get_last_commit_message.return_value = "feat: x\n\nbody"
    calls = {"n": 0}

    def _sha(*_a, **_k):
        calls["n"] += 1
        return "baseline000001" if calls["n"] == 1 else "delivered000002"

    git.get_last_commit_sha.side_effect = _sha
    git.build_commit_url.return_value = "http://git/commit/delivered000002"
    git.create_merge_request.return_value = MR_URL
    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_1",
            "retry_info": {"attempts": 1, "max_retries": 3, "retried": False},
            "timed_out": False,
        }
    )
    processor.git_manager = git
    processor.agent_runner = runner
    processor._contexts["X"] = {"git": git, "runner": runner}
    return git, runner


def _settings(s, tmp_path: Path) -> None:
    s.default_agent = "atlas"
    s.agent_task_timeout_seconds = 10
    s.agent_task_max_retries = 1
    s.default_branch = "develop"
    s.full_plans_dir = tmp_path / "plans"
    s.sisyphus_plans_dir = Path(".sisyphus/plans")
    s.default_model = ""
    s.default_review_model = ""


@pytest.mark.asyncio
async def test_jira_execution_posts_stdout_when_the_mr_opens(
    processor, state_manager, tmp_path
):
    state = state_manager.create_state("KAN-10", "login", "d")
    git, _runner = _mock_git_and_agent(
        processor, tmp_path, returncode=0, stdout=SUMMARY
    )
    processor._contexts[state.issue_key] = {
        "git": git,
        "runner": processor.agent_runner,
    }
    notes: list[dict] = []

    def _note(**kwargs):
        notes.append(kwargs)
        return {"id": 3}

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as settings:
            _settings(settings, tmp_path)
            with patch("src.gitlab.client.GitlabClient.post_mr_note", side_effect=_note):
                await processor._start_execution_workflow(state)

    st = state_manager.get_state("KAN-10")
    assert st.status == TaskStatus.COMPLETED
    assert SUMMARY in git.create_merge_request.call_args.kwargs["body"]
    assert notes and SUMMARY in notes[0]["body"]


@pytest.mark.asyncio
async def test_jira_error_with_new_commits_still_posts_the_summary(
    processor, state_manager, tmp_path
):
    state = state_manager.create_state("KAN-11", "login", "d")
    git, _runner = _mock_git_and_agent(
        processor,
        tmp_path,
        returncode=2,
        stdout=SUMMARY,
        stderr="incomplete",
    )
    processor._contexts[state.issue_key] = {
        "git": git,
        "runner": processor.agent_runner,
    }
    notes: list[dict] = []

    def _note(**kwargs):
        notes.append(kwargs)
        return {"id": 4}

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as settings:
            _settings(settings, tmp_path)
            with patch("src.gitlab.client.GitlabClient.post_mr_note", side_effect=_note):
                await processor._start_execution_workflow(state)

    assert SUMMARY in git.create_merge_request.call_args.kwargs["body"]
    assert notes and SUMMARY in notes[0]["body"]
