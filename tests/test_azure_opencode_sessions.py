"""Azure work-item / PR OpenCode session binds — same rules as Jira + GitLab."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.git_manager import GitManager
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import (
    SessionBindStore,
    bind_id_for,
    normalize_session_kind,
)
from src.jira.simulated_client import SimulatedJiraClient
from src.reporter.jira_reporter import JiraReporter


_REPO = "https://tfs.example.com/tfs/ColB/Beta/_git/app.git"
_WORK = "feature/WIT-BETA-42"
_TGT = "develop"


@pytest.fixture
def processor(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sim = SimulatedJiraClient(base_url="http://127.0.0.1:1")
    with patch("src.processor.create_jira_client", return_value=sim):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = sim
    proc.reporter = JiraReporter(client=sim)
    return proc


def test_azure_pr_workflow_type_is_build_kind():
    assert normalize_session_kind("azure_pr") == "build"
    assert normalize_session_kind("execution") == "build"
    assert normalize_session_kind("planning") == "plan"
    assert normalize_session_kind("azure_workitem") == ""
    assert normalize_session_kind("review") == "review"


def test_same_repo_work_target_same_bind_id_across_issue_keys():
    """Kind-specific binds ignore issue key — same as Jira + GitLab."""
    a = bind_id_for(_REPO, _WORK, _TGT, issue_key="WIT-BETA-42", kind="build")
    b = bind_id_for(_REPO, _WORK, _TGT, issue_key="KAN-12", kind="build")
    c = bind_id_for(_REPO, _WORK, _TGT, issue_key="AZ-COLB-BETA-APP-9", kind="azure_pr")
    assert a == b == c


def test_plan_and_build_use_different_binds():
    plan = bind_id_for(_REPO, _WORK, _TGT, kind="plan")
    build = bind_id_for(_REPO, _WORK, _TGT, kind="build")
    assert plan != build


def test_primary_base_work_item_isolates_feature_branch():
    name = GitManager.resolve_work_branch_name(
        "WIT-BETA-42", "develop", "develop", keep_source=False
    )
    assert name == "feature/WIT-BETA-42"


def test_processor_kind_for_work_item_plan_and_build(processor):
    sm = processor.state_manager
    sm.create_state("WIT-BETA-42", "Do", "x")
    sm.update_state(
        "WIT-BETA-42",
        status=TaskStatus.PLANNING,
        metadata={"source": "azure_workitem", "workflow_type": "planning"},
    )
    assert processor._session_kind_for_issue("WIT-BETA-42") == "plan"
    sm.update_state(
        "WIT-BETA-42",
        status=TaskStatus.EXECUTING,
        metadata={"source": "azure_workitem", "workflow_type": "execution"},
    )
    assert processor._session_kind_for_issue("WIT-BETA-42") == "build"
    sm.create_state("WIT-BETA-99", "PR follow-up", "x")
    sm.update_state(
        "WIT-BETA-99",
        status=TaskStatus.EXECUTING,
        metadata={"source": "azure", "workflow_type": "azure_pr"},
    )
    assert processor._session_kind_for_issue("WIT-BETA-99") == "build"


def test_bind_key_uses_feature_branch_not_develop_source(processor):
    sm = processor.state_manager
    sm.create_state("WIT-BETA-42", "Do", "x")
    sm.update_state(
        "WIT-BETA-42",
        metadata={
            "source": "azure_workitem",
            "repository_url": _REPO,
            "source_branch": "develop",
            "target_branch": "develop",
            "feature_branch": _WORK,
        },
    )
    repo, branch, target = processor._session_bind_key("WIT-BETA-42")
    assert repo
    assert branch == _WORK
    assert target == "develop"


def test_work_item_then_pr_on_same_branch_resumes_session(tmp_path, processor):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url=_REPO,
        branch=_WORK,
        target_branch=_TGT,
        issue_key="WIT-BETA-42",
        session_id="ses_workitem_1",
        working_directory=str(tmp_path / "clone"),
        kind="build",
    )
    hit = store.get(_REPO, _WORK, _TGT, kind="build")
    assert hit is not None
    assert hit["session_id"] == "ses_workitem_1"
    # PR comment job on the same work branch (typical after work-item clone)
    again = store.get(_REPO, _WORK, _TGT, kind="azure_pr")
    assert again is not None
    assert again["session_id"] == "ses_workitem_1"


def test_pr_on_other_branch_does_not_steal_work_item_session(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url=_REPO,
        branch=_WORK,
        target_branch=_TGT,
        issue_key="WIT-BETA-42",
        session_id="ses_workitem_1",
        kind="build",
    )
    other = store.get(_REPO, "feature/login", _TGT, kind="build")
    assert other is None


def test_jira_and_gitlab_same_rule_still_holds(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url="https://gitlab.example.com/acme/demo.git",
        branch="feature/KAN-12",
        target_branch="develop",
        issue_key="KAN-12",
        session_id="ses_jira_1",
        kind="build",
    )
    gl = store.get(
        "https://gitlab.example.com/acme/demo.git",
        "feature/KAN-12",
        "develop",
        kind="build",
    )
    assert gl is not None
    assert gl["session_id"] == "ses_jira_1"
