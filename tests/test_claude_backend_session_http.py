"""Cross-backend session binds, exercised through the dashboard HTTP API.

These lock what a real GET/DELETE plus the job resume path do today.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.backends.base import is_claude_session_id, is_codex_thread_id
from src.backends.claude import DEFAULT_CLAUDE_RESUME_PROMPT
from src.backends.codex import DEFAULT_CODEX_RESUME_PROMPT
from src.dashboard.api import create_dashboard_app
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import bind_id_for

REPO = "https://gitlab.example.com/acme/app.git"
BRANCH = "feature/login"
TARGET = "develop"
CODEX_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
OPENCODE_ID = "ses_build_oc"
PLAN_ID = "99999999-8888-7777-6666-555555555555"


def test_uuid_matches_both_codex_and_claude():
    assert is_codex_thread_id(CODEX_ID) is True
    assert is_claude_session_id(CODEX_ID) is True
    assert is_claude_session_id(CLAUDE_ID) is True
    assert is_codex_thread_id(CLAUDE_ID) is True
    assert is_claude_session_id(OPENCODE_ID) is False
    assert is_codex_thread_id(OPENCODE_ID) is False


def _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts):
    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc.job_store = isolate_jira_agent_artifacts["job_store"]
    return proc, sm, binds


def _git(tmp_path):
    git = MagicMock()
    git.remote_url = REPO
    git.work_branch = BRANCH
    git.source_branch = BRANCH
    git.target_branch = TARGET
    git.get_working_directory.return_value = tmp_path
    return git


def _ready(sm, issue: str, kind: str):
    sm.create_state(issue, "session check", "Mode: build")
    sm.update_state(
        issue,
        status=TaskStatus.EXECUTING if kind != "plan" else TaskStatus.PLANNING,
        metadata={
            "workflow_type": kind,
            "repository_url": REPO,
            "feature_branch": BRANCH,
            "target_branch": TARGET,
        },
    )


def _attach(proc, sm, issue: str, backend: str, git, *, kind: str = "build"):
    _ready(sm, issue, kind)
    task = AgentTask(
        description="check",
        prompt="FULL BUILD KIT",
        agent="derman-build",
        issue_key=issue,
        backend=backend,
    )
    chosen = proc._attach_bound_opencode_session(issue, task, git)
    return chosen, task


def test_http_one_build_slot_replaces_opencode_with_claude_and_reset_clears_it(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(state_manager=sm)
    client = TestClient(app)

    first = binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=OPENCODE_ID,
        issue_key="KAN-1",
        kind="build",
        backend="opencode",
    )
    second = binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CLAUDE_ID,
        issue_key="KAN-1",
        kind="build",
        backend="claude",
    )
    assert first["bind_id"] != second["bind_id"]
    assert second["bind_id"] == bind_id_for(
        REPO, BRANCH, TARGET, kind="build", backend="claude"
    )

    listed = client.get("/api/opencode-sessions")
    assert listed.status_code == 200
    rows = [row for row in listed.json()["sessions"] if row["kind"] == "build"]
    by_session = {row["session_id"]: row["backend"] for row in rows}
    assert by_session == {OPENCODE_ID: "opencode", CLAUDE_ID: "claude"}

    reset = client.delete(f"/api/opencode-sessions/{second['bind_id']}")
    assert reset.status_code == 200
    assert reset.json()["ok"] is True
    again = client.get("/api/opencode-sessions")
    left = {row.get("session_id") for row in again.json()["sessions"]}
    assert CLAUDE_ID not in left
    assert OPENCODE_ID in left


def test_claude_resumes_codex_uuid_with_codex_prompt(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CODEX_ID,
        issue_key="KAN-9",
        kind="build",
        backend="codex",
    )
    chosen, task = _attach(proc, sm, "KAN-9", "claude", _git(tmp_path))
    assert chosen is None
    assert task.prompt == "FULL BUILD KIT"
    assert task.prompt != DEFAULT_CODEX_RESUME_PROMPT


def test_codex_resumes_claude_uuid(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CLAUDE_ID,
        issue_key="KAN-2",
        kind="build",
        backend="claude",
    )
    chosen, task = _attach(proc, sm, "KAN-2", "codex", _git(tmp_path))
    assert chosen is None
    assert task.prompt == "FULL BUILD KIT"


def test_claude_resume_of_its_own_id_still_uses_codex_prompt(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CLAUDE_ID,
        issue_key="KAN-3",
        kind="build",
        backend="claude",
    )
    chosen, task = _attach(proc, sm, "KAN-3", "claude", _git(tmp_path))
    assert chosen == CLAUDE_ID
    assert task.prompt == DEFAULT_CLAUDE_RESUME_PROMPT


def test_claude_resume_keeps_the_first_prompt_in_history(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """A later run sends the short continue line, but the first prompt stays visible."""
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    jobs = isolate_jira_agent_artifacts["job_store"]
    first_prompt = tmp_path / "first.prompt.txt"
    first_log = tmp_path / "first.log"
    first_prompt.write_text("FULL BUILD KIT\nfix the login form\n", encoding="utf-8")
    first_log.write_text('{"type":"result","is_error":true,"result":"server error"}\n', encoding="utf-8")
    sm.create_state("KAN-3", "session check", "Mode: build")
    failed = jobs.create_job(issue_key="KAN-3", summary="first", status="error", backend="claude")
    jobs.update_job(
        failed["job_id"],
        opencode_session_id=CLAUDE_ID,
        session_log_path=str(first_log),
        session_log_paths=[str(first_log)],
        prompt_path=str(first_prompt),
        prompt_paths=[str(first_prompt)],
    )
    live = jobs.create_job(issue_key="KAN-3", summary="retry", status="executing", backend="claude")
    proc._active_jobs["KAN-3"] = live["job_id"]
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CLAUDE_ID,
        issue_key="KAN-3",
        kind="build",
        backend="claude",
    )
    _ready(sm, "KAN-3", "build")
    task = AgentTask(
        description="check",
        prompt="FULL BUILD KIT\nfix the login form\n",
        agent="derman-build",
        issue_key="KAN-3",
        backend="claude",
    )
    chosen = proc._attach_bound_opencode_session("KAN-3", task, _git(tmp_path))
    assert chosen == CLAUDE_ID
    assert task.prompt == DEFAULT_CLAUDE_RESUME_PROMPT
    saved = jobs.get_job(live["job_id"])
    assert saved is not None
    assert str(first_prompt) in (saved.get("prompt_paths") or [])
    assert str(first_log) in (saved.get("session_log_paths") or [])


def test_opencode_does_not_resume_a_claude_uuid(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=CLAUDE_ID,
        issue_key="KAN-4",
        kind="build",
        backend="claude",
    )
    chosen, task = _attach(proc, sm, "KAN-4", "opencode", _git(tmp_path))
    assert chosen is None
    assert task.session_id is None
    assert task.prompt == "FULL BUILD KIT"


def test_build_does_not_resume_plan_claude_session(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    proc, sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=PLAN_ID,
        issue_key="KAN-5",
        kind="plan",
        backend="claude",
    )
    chosen, task = _attach(proc, sm, "KAN-5", "claude", _git(tmp_path), kind="build")
    assert chosen is None
    assert task.prompt == "FULL BUILD KIT"
