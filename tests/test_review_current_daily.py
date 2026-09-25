"""Daily-usage proofs for the last 15 commits and the uncommitted Claude parser.

Each test asserts the operator-safe outcome. A failure is a real bug.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus

PLAN_ID = "99999999-8888-7777-6666-555555555555"
REPO = "https://gitlab.example.com/acme/app.git"
BRANCH = "feature/login"
TARGET = "develop"


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


def _ready(sm, issue: str, kind: str, *, session_id: str = ""):
    sm.create_state(issue, "daily review", "Mode: build")
    meta = {
        "workflow_type": kind,
        "repository_url": REPO,
        "feature_branch": BRANCH,
        "target_branch": TARGET,
    }
    if session_id:
        meta["opencode_session_ids"] = [session_id]
        meta["last_opencode_session_id"] = session_id
    sm.update_state(
        issue,
        status=TaskStatus.EXECUTING if kind != "plan" else TaskStatus.PLANNING,
        current_opencode_session_id=session_id or None,
        metadata=meta,
    )


def test_build_finds_plan_saved_with_a_backend(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """A plan bind written with backend=claude is the plan a later build implements."""
    proc, _sm, binds = _proc(tmp_path, monkeypatch, isolate_jira_agent_artifacts)
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.setattr("src.paths.plans_dir", lambda: plans)
    (plans / "KAN-PLAN.md").write_text("# plan\n", encoding="utf-8")
    binds.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=PLAN_ID,
        issue_key="KAN-PLAN",
        kind="plan",
        backend="claude",
    )
    _ready(proc.state_manager, "KAN-BUILD", "build")
    found = proc._resolve_plan_for_build("KAN-BUILD", _git(tmp_path))
    assert found == str(plans / "KAN-PLAN.md")
