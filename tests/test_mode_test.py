"""Mode: test routes to derman-test and a session map of its own."""

from pathlib import Path
from typing import Any, Dict

from src.config import settings
from src.orchestrator.prompt_builder import PromptBuilder
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import (
    SessionBindStore,
    bind_compatible_with_kind,
    bind_id_for,
    normalize_session_kind,
    other_session_kinds,
)


def test_test_prompt_requires_agents_md_and_unit_tests_only():
    PromptBuilder.clear_prompt_file_cache()
    text = PromptBuilder.build_test_prompt(
        "KAN-9",
        "cover login",
        "{params}\nMode: test\n{params}",
        work_branch="feature/KAN-9",
    )
    assert "derman-test" in text
    assert "AGENTS.md" in text
    assert "unit tests only" in text.lower() or "Write **unit tests only**" in text
    assert "line" in text.lower() and "branch" in text.lower()
    assert "condition" in text.lower()
    assert "hacky" in text.lower()
    assert "expect_call" in text
    assert "exact" in text.lower()
    assert "not-called" in text or "assert_not_called" in text
    assert "boundaries" in text.lower()
    assert "feature/KAN-9" in text
    assert "KAN-9" in text


def test_session_kind_testing_is_separate_from_build():
    assert normalize_session_kind("testing") == "test"
    assert normalize_session_kind("derman-test") == "test"
    assert normalize_session_kind("execution") == "build"
    assert normalize_session_kind("planning") == "plan"
    assert normalize_session_kind("review") == "review"
    assert normalize_session_kind("code-reviewer") == "review"
    assert normalize_session_kind("derman-reviewer") == "review"
    assert set(other_session_kinds("test")) == {"plan", "build", "review"}
    assert set(other_session_kinds("build")) == {"plan", "test", "review"}
    assert set(other_session_kinds("plan")) == {"build", "test", "review"}
    assert set(other_session_kinds("review")) == {"plan", "build", "test"}
    assert bind_compatible_with_kind({"kind": "test"}, "build") is False
    assert bind_compatible_with_kind({"kind": "build"}, "test") is False
    assert bind_compatible_with_kind({"kind": "plan"}, "build") is False
    assert bind_compatible_with_kind({"kind": ""}, "build") is True
    assert bind_compatible_with_kind({"kind": "test"}, "test") is True


def test_plan_build_test_bind_ids_are_three_maps():
    repo = "https://gitlab.example.com/acme/app.git"
    work = "feature/KAN-12"
    target = "develop"
    ids = {
        bind_id_for(repo, work, target, kind="plan"),
        bind_id_for(repo, work, target, kind="build"),
        bind_id_for(repo, work, target, kind="test"),
    }
    assert len(ids) == 3


REPO = "https://gitlab.example.com/acme/app.git"
SOURCE = "develop"
TARGET = "develop"
WORK = "feature/KAN-12"


def _processor(tmp_path: Path, isolate: Dict[str, Any], monkeypatch) -> JobProcessor:
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    return proc


class _Git:
    remote_url = REPO
    work_branch = WORK
    target_branch = TARGET


def _seed_completed_run(
    proc: JobProcessor,
    store: SessionBindStore,
    *,
    workflow_type: str,
    session_id: str,
    issue: str = "KAN-12",
) -> None:
    proc.state_manager.create_state(issue, "cover login", "{params}\nMode: build\n{params}")
    proc.state_manager.update_state(
        issue,
        status=TaskStatus.COMPLETED,
        current_opencode_session_id=None,
        metadata={
            "workflow_type": workflow_type,
            "repository_url": REPO,
            "source_branch": SOURCE,
            "target_branch": TARGET,
            "feature_branch": WORK,
            "last_opencode_session_id": session_id,
            "opencode_session_ids": [session_id],
        },
    )
    stored = store.upsert(
        repository_url=REPO,
        branch=WORK,
        target_branch=TARGET,
        session_id=session_id,
        issue_key=issue,
        kind=workflow_type,
    )
    assert stored is not None
    assert stored.get("session_id") == session_id


def _requeue_as(proc: JobProcessor, *, workflow_type: str, issue: str = "KAN-12") -> None:
    proc._reset_for_reprocess(issue)
    proc.state_manager.update_state(
        issue,
        status=TaskStatus.EXECUTING,
        current_opencode_session_id=None,
        metadata={
            "workflow_type": workflow_type,
            "repository_url": REPO,
            "source_branch": SOURCE,
            "target_branch": TARGET,
            "feature_branch": WORK,
        },
    )


def test_mode_test_does_not_resume_prior_build_session(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    proc = _processor(tmp_path, isolate, monkeypatch)
    _seed_completed_run(proc, store, workflow_type="execution", session_id="ses_build_kan12")
    _requeue_as(proc, workflow_type="testing")

    assert proc._session_kind_for_issue("KAN-12") == "test"
    sids, _forgotten, _wd = proc._resume_session_candidates("KAN-12", _Git())
    assert "ses_build_kan12" not in sids


def test_mode_build_does_not_resume_prior_test_session(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    proc = _processor(tmp_path, isolate, monkeypatch)
    _seed_completed_run(proc, store, workflow_type="testing", session_id="ses_test_kan12")
    _requeue_as(proc, workflow_type="execution")

    assert proc._session_kind_for_issue("KAN-12") == "build"
    sids, _forgotten, _wd = proc._resume_session_candidates("KAN-12", _Git())
    assert "ses_test_kan12" not in sids


def test_mode_test_resumes_prior_test_session(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    proc = _processor(tmp_path, isolate, monkeypatch)
    _seed_completed_run(proc, store, workflow_type="testing", session_id="ses_test_kan12")
    store.upsert(
        repository_url=REPO,
        branch=WORK,
        target_branch=TARGET,
        session_id="ses_build_other",
        issue_key="KAN-12",
        kind="build",
    )
    _requeue_as(proc, workflow_type="testing")

    sids, _forgotten, _wd = proc._resume_session_candidates("KAN-12", _Git())
    assert "ses_test_kan12" in sids
    assert "ses_build_other" not in sids


def test_one_issue_keeps_three_kind_maps(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    proc = _processor(tmp_path, isolate, monkeypatch)
    issue = "KAN-12"
    proc.state_manager.create_state(issue, "work", "{params}\nMode: plan\n{params}")
    for wf, sid in (
        ("planning", "ses_plan_kan12"),
        ("execution", "ses_build_kan12"),
        ("testing", "ses_test_kan12"),
    ):
        store.upsert(
            repository_url=REPO,
            branch=WORK,
            target_branch=TARGET,
            session_id=sid,
            issue_key=issue,
            kind=wf,
        )

    proc.state_manager.update_state(
        issue,
        status=TaskStatus.EXECUTING,
        metadata={
            "workflow_type": "testing",
            "repository_url": REPO,
            "source_branch": SOURCE,
            "target_branch": TARGET,
            "feature_branch": WORK,
            "last_opencode_session_id": "ses_build_kan12",
            "opencode_session_ids": [
                "ses_plan_kan12",
                "ses_build_kan12",
                "ses_test_kan12",
            ],
        },
    )
    test_sids, _, _ = proc._resume_session_candidates(issue, _Git())
    assert test_sids == ["ses_test_kan12"]

    proc.state_manager.update_state(issue, metadata={"workflow_type": "execution"})
    build_sids, _, _ = proc._resume_session_candidates(issue, _Git())
    assert "ses_build_kan12" in build_sids
    assert "ses_plan_kan12" not in build_sids
    assert "ses_test_kan12" not in build_sids

    proc.state_manager.update_state(
        issue, status=TaskStatus.PLANNING, metadata={"workflow_type": "planning"}
    )
    plan_sids, _, _ = proc._resume_session_candidates(issue, _Git())
    assert plan_sids == ["ses_plan_kan12"]
