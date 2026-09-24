"""Integration proofs for the six daily-usage defects found in review.

Each test drives the production function (processor workflow, dashboard
route, GitManager folder naming, or Windows clone delete) and asserts the
behavior that is wrong today. A fix should turn the matching assertion
over to the safe outcome and update the docstring.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore


QUESTION = "Which database should I use — Postgres or SQLite?"


def _processor(state_manager, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.processor.create_jira_client", lambda *a, **k: fake_jira)
    proc = JobProcessor()
    proc.state_manager = state_manager
    from src.reporter.jira_reporter import JiraReporter

    proc.reporter = JiraReporter(client=fake_jira)
    proc.jira_client = fake_jira
    return proc


def _git(tmp_path: Path, *, mr_url):
    git = MagicMock()
    git.work_branch = "feature/KAN-1"
    git.target_branch = "develop"
    git.source_branch = "feature/shared"
    git.remote_url = "https://gitlab.example.com/acme/app.git"
    git.ensure_feature_branch.return_value = "feature/KAN-1"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/KAN-1"
    git.ensure_on_work_branch.return_value = True
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.head_is_on_remote.return_value = True
    git.last_push_error = ""
    git.last_mr_error = "403 insufficient_scope"
    git.get_last_commit_subject.return_value = "feat: login"
    git.get_last_commit_message.return_value = "feat: login"
    git.build_commit_url.return_value = "https://gitlab.example.com/acme/app/-/commit/abc"
    git.create_merge_request.return_value = mr_url
    git.get_mr_url.return_value = mr_url
    calls = {"n": 0}

    def _sha(*_a, **_k):
        calls["n"] += 1
        return "baseline000001" if calls["n"] == 1 else "delivered000002"

    git.get_last_commit_sha.side_effect = _sha
    return git


def _bind_git(proc, git, runner):
    def _init(issue_key, state=None, **_kwargs):
        proc._contexts[issue_key] = {"git": git, "runner": runner}
        return git

    proc._init_git_manager = _init


def _runner(payload: dict):
    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(return_value=payload)
    runner.run_agent = AsyncMock(return_value=payload)
    return runner


@pytest.mark.asyncio
async def test_finding_push_without_mr_is_marked_delivered(
    state_manager, fake_jira, tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Build pushes, MR create returns nothing, job still Completes as delivered."""
    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    git = _git(tmp_path, mr_url=None)
    runner = _runner(
        {
            "returncode": 0,
            "stdout": "implemented login",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_1",
            "timed_out": False,
            "incomplete": False,
            "backend": "codex",
        }
    )
    _bind_git(proc, git, runner)
    state = state_manager.create_state(
        "KAN-1",
        "login",
        "Mode: build\nRepository: https://gitlab.example.com/acme/app.git\n"
        "Source branch: feature/KAN-1\nTarget branch: develop\n",
    )
    await proc._start_execution_workflow(state)

    st = state_manager.get_state("KAN-1")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert (st.metadata or {}).get("delivery_status") == "delivered"
    assert not (st.metadata or {}).get("merge_request_url")
    bodies = "\n".join(c["body"] for c in fake_jira.comments)
    assert "could not be created" in bodies
    assert "Tamamlanma" in bodies
    assert "Bu kayıt işlenirken bir hata oluştu" not in bodies
    git.push.assert_called()
    git.create_merge_request.assert_called()


def _comment_runner():
    return _runner(
        {
            "returncode": 0,
            "stdout": "pushed the fix",
            "stderr": "",
            "session_file": "s.log",
            "opencode_session_id": "ses_1",
            "timed_out": False,
            "incomplete": False,
            "backend": "codex",
        }
    )


@pytest.mark.asyncio
async def test_finding_gitlab_reply_failure_still_completes(
    state_manager, fake_jira, tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """@bot /yaver pushes, the MR note POST fails, local state is still COMPLETED."""
    from src.gitlab.webhook import GitlabMrNoteEvent

    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    git = _git(tmp_path, mr_url="https://gitlab.example.com/acme/app/-/merge_requests/4")
    _bind_git(proc, git, _comment_runner())

    class _Gitlab:
        def __init__(self, host: str = ""):
            self.host = host
            self.notes = 0

        def find_discussion_id_for_note(self, **_kwargs):
            return ""

        def post_mr_note(self, **_kwargs):
            self.notes += 1
            return None

    monkeypatch.setattr("src.gitlab.client.GitlabClient", _Gitlab)
    event = GitlabMrNoteEvent(
        issue_key="KAN-1",
        note_id="55",
        note_body="@bot /yaver fix login",
        prompt="fix login",
        author_username="alice",
        author_name="Alice",
        project_id=9,
        project_path="acme/app",
        repository_url="https://gitlab.example.com/acme/app.git",
        host="gitlab.example.com",
        mr_iid=4,
        mr_title="feat(KAN-1): login",
        mr_description="",
        source_branch="feature/KAN-1",
        target_branch="develop",
        mr_url="https://gitlab.example.com/acme/app/-/merge_requests/4",
        discussion_id="disc-55",
    )
    await proc._run_gitlab_mr_comment(event)
    st = state_manager.get_state("KAN-1")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert git.push.called


@pytest.mark.asyncio
async def test_finding_azure_reply_failure_still_completes(
    state_manager, fake_jira, tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Azure PR comment job completes when the thread reply POST returns nothing."""
    from src.azure.webhook import AzurePrCommentEvent

    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    git = _git(tmp_path, mr_url="https://tfs.example.com/pr/4")
    _bind_git(proc, git, _comment_runner())

    class _Azure:
        def __init__(self, host: str = "", collection_url: str = ""):
            self.calls = 0

        def find_thread_id_for_comment(self, **_kwargs):
            return ""

        def post_pr_comment(self, **_kwargs):
            self.calls += 1
            return None

    monkeypatch.setattr("src.azure.client.AzureDevOpsClient", _Azure)
    event = AzurePrCommentEvent(
        issue_key="KAN-1",
        comment_id="3",
        comment_body="@bot /yaver fix login",
        prompt="fix login",
        author_username="alice",
        author_name="Alice",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project="Demo",
        repository_id="app",
        repository_name="app",
        project_path="Demo/app",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app",
        host="tfs.example.com",
        pr_id=4,
        pr_title="feat(KAN-1): login",
        pr_description="",
        source_branch="feature/KAN-1",
        target_branch="develop",
        pr_url="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app/pullrequest/4",
        thread_id="8",
    )
    await proc._run_azure_pr_comment(event)
    st = state_manager.get_state("KAN-1")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert git.push.called


@pytest.mark.asyncio
async def test_finding_codex_question_becomes_plan_ready(
    state_manager, fake_jira, tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Codex exit 0 with a clarifying question is accepted as a finished plan."""
    from src.backends.base import AgentRunRequest
    from src.backends.codex import CodexBackend

    line = (
        '{"type":"item.completed","item":{"type":"agent_message","text":"'
        + QUESTION
        + '"}}\n'
    )

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_data(line.encode())
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    async def _exec(*_a, **_k):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(
        "src.backends.codex._codex_home_for",
        lambda _cwd: tmp_path / "codex-home",
    )
    codex = await CodexBackend().run(
        AgentRunRequest(
            prompt="plan the login work",
            working_directory=tmp_path,
            timeout_seconds=5,
        )
    )
    assert codex.returncode == 0
    assert codex.incomplete is False
    assert QUESTION in (codex.stdout or "")

    plan = tmp_path / ".sisyphus" / "plans" / "KAN-1.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(QUESTION + "\n", encoding="utf-8")

    proc = _processor(state_manager, fake_jira, tmp_path, monkeypatch)
    git = _git(tmp_path, mr_url=None)
    runner = _runner(
        {
            "returncode": codex.returncode,
            "stdout": codex.stdout,
            "stderr": codex.stderr,
            "incomplete": codex.incomplete,
            "incomplete_reasons": list(codex.incomplete_reasons or []),
            "backend": "codex",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": codex.session_id,
            "timed_out": False,
        }
    )
    _bind_git(proc, git, runner)
    state = state_manager.create_state("KAN-1", "plan login", "Mode: plan")
    await proc._start_planning_workflow(state)
    st = state_manager.get_state("KAN-1")
    assert st is not None
    assert st.status == TaskStatus.PLAN_READY
    bodies = "\n".join(c["body"] for c in fake_jira.comments)
    assert "Ajan netleştirme sormak için durdu" not in bodies


def _schedule_row(store: ScheduleStore, issue_key: str) -> dict:
    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    return store.create(
        title="run now",
        description="do the work",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="build",
        scheduled_at=when,
        issue_key=issue_key,
        issue_description="x",
    )


def _assert_run_now_looks_successful(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["dispatched"] is False
    assert body.get("dispatch_error")


def test_finding_run_now_returns_ok_when_dispatch_fails(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Run now on every schedule create route is HTTP 200 when dispatch cannot start."""
    store = isolate_jira_agent_artifacts["schedule_store"]
    monkeypatch.setattr("src.dashboard.api.schedule_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)
    when = (datetime.now() + timedelta(minutes=5)).isoformat(timespec="seconds")

    def _created(issue_key: str):
        rec = _schedule_row(store, issue_key)
        return {"ok": True, "schedule": rec, "issue_key": issue_key, "message": "saved"}

    monkeypatch.setattr(
        "src.dashboard.api.create_scheduled_job",
        lambda **_kw: _created("KAN-NEW"),
    )
    created = client.post(
        "/api/schedules",
        json={
            "title": "login",
            "description": "add login",
            "repository_url": "https://gitlab.example.com/acme/app.git",
            "source_branch": "feature/x",
            "target_branch": "develop",
            "mode": "build",
            "scheduled_at": when,
            "dispatch_now": True,
        },
    )
    _assert_run_now_looks_successful(created)

    monkeypatch.setattr(
        "src.dashboard.api.schedule_existing_issue",
        lambda *_a, **_kw: _created("KAN-OLD"),
    )
    existing = client.post(
        "/api/schedules/from-issue",
        json={"issue_key": "KAN-OLD", "scheduled_at": when, "dispatch_now": True},
    )
    _assert_run_now_looks_successful(existing)

    monkeypatch.setattr(
        "src.dashboard.api.schedule_mr_followup",
        lambda **_kw: _created("GL-ACME-APP-4"),
    )
    mr = client.post(
        "/api/schedules/mr",
        json={
            "repository_url": "https://gitlab.example.com/acme/app.git",
            "mr_iid": 4,
            "prompt": "fix the test",
            "scheduled_at": when,
            "dispatch_now": True,
        },
    )
    _assert_run_now_looks_successful(mr)

    monkeypatch.setattr(
        "src.dashboard.api.schedule_pr_followup",
        lambda **_kw: _created("AZ-DEMO-APP-4"),
    )
    pr = client.post(
        "/api/schedules/pr",
        json={
            "repository_url": "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app",
            "pr_id": 4,
            "prompt": "fix the test",
            "scheduled_at": when,
            "dispatch_now": True,
        },
    )
    _assert_run_now_looks_successful(pr)


def test_finding_same_source_and_target_split_clone_but_share_session(
    tmp_path, monkeypatch
):
    """Two tickets on one source+target get different folders and one session bind."""
    from src.git_manager import GitManager
    from src.state.session_bind_store import bind_id_for

    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / "t")
    repo = "https://gitlab.example.com/acme/app.git"

    def _folder(issue_key: str) -> Path:
        git = GitManager.__new__(GitManager)
        git.issue_key = issue_key
        git.remote_url = repo
        git.remote_name = "app"
        git.source_branch = "feature/shared"
        git.target_branch = "develop"
        git.work_branch = "feature/shared"
        git.temp_dir = None
        return git._create_temp_directory()

    first = _folder("KAN-1")
    second = _folder("KAN-2")
    assert first != second
    assert first.name != second.name
    kind = "build"
    assert bind_id_for(repo, "feature/shared", "develop", "KAN-1", kind) == bind_id_for(
        repo, "feature/shared", "develop", "KAN-2", kind
    )


@pytest.mark.skipif(os.name != "nt", reason="junction follow is a Windows rd /s behavior")
def test_finding_py310_junction_delete_wipes_durable_plans(tmp_path, monkeypatch):
    """Without Path.is_junction, clone delete follows .yaver-plans into every plan."""
    from src.temp_fs import force_rmtree_progress

    plans = tmp_path / "yaver" / "plans"
    plans.mkdir(parents=True)
    (plans / "KAN-1.md").write_text("# one\n", encoding="utf-8")
    (plans / "KAN-2.md").write_text("# two\n", encoding="utf-8")
    clone = tmp_path / "t" / "app_clone"
    clone.mkdir(parents=True)
    (clone / "README.md").write_text("work\n", encoding="utf-8")
    link = clone / ".yaver-plans"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(plans)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    def _symlink_only(path: Path) -> bool:
        try:
            return bool(path.is_symlink())
        except OSError:
            return False

    monkeypatch.setattr("src.temp_fs._is_dir_link", _symlink_only)
    force_rmtree_progress(clone)
    assert not (plans / "KAN-1.md").is_file()
    assert not (plans / "KAN-2.md").is_file()
