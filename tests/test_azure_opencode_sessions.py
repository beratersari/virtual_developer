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


def test_multi_repo_session_is_not_the_single_repo_session(tmp_path, monkeypatch):
    """Same first repo and branch must not share a chat across layouts."""
    from src.state.session_bind_store import (
        SessionBindStore,
        multi_session_scope,
    )

    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr(
        "src.state.session_bind_store.session_bind_store", store, raising=False
    )
    repo = "https://gitlab.example/group/x.git"
    other = "https://gitlab.example/group/y.git"
    scope = multi_session_scope([repo, other])
    multi = store.upsert(
        repository_url=repo,
        branch="feature/KAN-517",
        target_branch="main",
        session_id="ses_multi",
        issue_key="KAN-517",
        working_directory=str(tmp_path / "multi_abc"),
        kind="build",
        backend="opencode",
        scope=scope,
    )
    single = store.upsert(
        repository_url=repo,
        branch="feature/KAN-517",
        target_branch="main",
        session_id="ses_single",
        issue_key="KAN-900",
        working_directory=str(tmp_path / "x"),
        kind="build",
        backend="opencode",
    )
    assert multi["bind_id"] != single["bind_id"]
    assert store.get(
        repo, "feature/KAN-517", "main", kind="build", backend="opencode"
    )["session_id"] == "ses_single"
    assert store.get(
        repo,
        "feature/KAN-517",
        "main",
        kind="build",
        backend="opencode",
        scope=scope,
    )["session_id"] == "ses_multi"


def test_legacy_multi_folder_bind_is_not_returned_to_a_single_repo_job(tmp_path):
    """An older multi_* row on the single-repo key is moved, not resumed."""
    from src.state.session_bind_store import SessionBindStore

    store = SessionBindStore(binds_dir=tmp_path / "binds")
    repo = "https://gitlab.example/group/x.git"
    saved = store.upsert(
        repository_url=repo,
        branch="feature/KAN-517",
        target_branch="main",
        session_id="ses_oldmulti",
        issue_key="KAN-517",
        working_directory=str(tmp_path / "multi_legacy"),
        kind="build",
        backend="opencode",
    )
    assert store.get(
        repo, "feature/KAN-517", "main", kind="build", backend="opencode"
    ) is None
    moved = store.get(
        repo,
        "feature/KAN-517",
        "main",
        kind="build",
        backend="opencode",
        scope="multi",
    )
    assert moved is not None
    assert moved["session_id"] == "ses_oldmulti"
    assert moved["bind_id"] != saved["bind_id"]


def test_resume_keeps_claude_codex_and_opencode_apart(processor, tmp_path, monkeypatch):
    """Each worker, and a multi-repo OpenCode chat, resumes only its own id."""
    from src.state.models import TaskStatus
    from src.state.session_bind_store import SessionBindStore, multi_session_scope

    store = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr(
        "src.state.session_bind_store.session_bind_store", store
    )
    repo = "https://gitlab.example/group/x.git"
    other = "https://gitlab.example/group/y.git"
    third = "https://gitlab.example/group/z.git"
    scope_xy = multi_session_scope([repo, other])
    scope_xz = multi_session_scope([repo, third])
    codex = "11111111-1111-1111-1111-111111111111"
    claude = "22222222-2222-2222-2222-222222222222"
    common = dict(
        repository_url=repo,
        branch="feature/KAN-517",
        target_branch="main",
        kind="build",
    )
    store.upsert(**common, session_id="ses_single", issue_key="KAN-900", backend="opencode", working_directory=str(tmp_path / "x"))
    store.upsert(**common, session_id=codex, issue_key="KAN-901", backend="codex", working_directory=str(tmp_path / "x"))
    store.upsert(**common, session_id=claude, issue_key="KAN-902", backend="claude", working_directory=str(tmp_path / "x"))
    store.upsert(**common, session_id="ses_multi", issue_key="KAN-517", backend="opencode", scope=scope_xy, working_directory=str(tmp_path / "multi_xy"))
    store.upsert(**common, session_id="ses_other_set", issue_key="KAN-518", backend="opencode", scope=scope_xz, working_directory=str(tmp_path / "multi_xz"))
    plan = dict(common)
    plan["kind"] = "plan"
    store.upsert(**plan, session_id="ses_plan", issue_key="KAN-900", backend="opencode", working_directory=str(tmp_path / "x"))

    class One:
        remote_url = repo
        work_branch = "feature/KAN-517"
        target_branch = "main"
        repo_checkouts = []

    class Multi:
        remote_url = repo
        work_branch = "feature/KAN-517"
        target_branch = "main"

        def __init__(self, urls):
            self.repo_checkouts = [OneRepo(url) for url in urls]

    class OneRepo:
        def __init__(self, url):
            self.remote_url = url

    sm = processor.state_manager
    for key in ("KAN-900", "KAN-901", "KAN-902", "KAN-517", "KAN-518"):
        sm.create_state(key, "build", "d")
        sm.update_state(key, status=TaskStatus.EXECUTING, metadata={"workflow_type": "execution"})
    sm.update_state(
        "KAN-900",
        current_opencode_session_id="ses_multi",
        metadata={
            "workflow_type": "execution",
            "last_opencode_session_id": "ses_multi",
            "opencode_session_ids": ["ses_multi"],
        },
    )

    single, _, _ = processor._resume_session_candidates("KAN-900", One(), backend="opencode")
    codex_ids, _, _ = processor._resume_session_candidates("KAN-901", One(), backend="codex")
    claude_ids, _, _ = processor._resume_session_candidates("KAN-902", One(), backend="claude")
    multi_ids, _, _ = processor._resume_session_candidates("KAN-517", Multi([repo, other]), backend="opencode")
    other_ids, _, _ = processor._resume_session_candidates("KAN-518", Multi([repo, third]), backend="opencode")
    sm.update_state("KAN-900", status=TaskStatus.PLANNING, metadata={"workflow_type": "planning"})
    plan_ids, _, _ = processor._resume_session_candidates("KAN-900", One(), backend="opencode")

    assert single == ["ses_single"]
    assert codex_ids == [codex]
    assert claude_ids == [claude]
    assert multi_ids == ["ses_multi"]
    assert other_ids == ["ses_other_set"]
    assert plan_ids == ["ses_plan"]


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
