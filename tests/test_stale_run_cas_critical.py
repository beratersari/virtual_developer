"""Notes + guards for CAS / URL persistence that look like bugs but are not.

Generation-aware complete belongs on ``_complete_work`` only (0.9.14).
GitLab/Azure in-lock stamps, issue-keyed ``_fail_issue``, and storing
inbound repo URLs as received are intentional. See comments on those
call sites.

``_ensure_job_for_failure`` runs *before* fail CAS on purpose so a
template/Mode failure still gets a Jobs-tab row.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from src.processor import JobProcessor
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _executing(state_manager, key: str, *, job_id: str, task_id: str):
    state_manager.create_state(key, "s", "d")
    return state_manager.update_state(
        key,
        status=TaskStatus.EXECUTING,
        current_task_id=task_id,
        metadata={"current_job_id": job_id},
    )


def test_complete_work_does_not_complete_a_newer_job(processor, state_manager):
    """0.9.14: Jira ``_complete_work`` is generation-aware."""
    stale = _executing(state_manager, "CAS-NEW-1", job_id="job-old", task_id="task-old")
    state_manager.update_state(
        "CAS-NEW-1",
        current_task_id="task-new",
        metadata={"current_job_id": "job-new"},
    )
    import asyncio

    asyncio.run(processor._complete_work(stale, "late success"))
    live = state_manager.get_state("CAS-NEW-1")
    assert live.status == TaskStatus.EXECUTING
    assert live.current_task_id == "task-new"
    assert (live.metadata or {}).get("current_job_id") == "job-new"


def test_gitlab_and_azure_complete_stamps_omit_generation_ids_intentionally():
    """In-lock GitLab/Azure success must not grow expected_job_id.

    The per-issue lock is still held; cancel already wrote CANCELLED which
    these stamps reject. Do not "fix" this to match ``_complete_work``.
    """
    from src.processor import JobProcessor

    gitlab = inspect.getsource(JobProcessor._start_gitlab_mr_workflow)
    azure = inspect.getsource(JobProcessor._start_azure_pr_workflow)
    assert "expected_job_id=" not in gitlab
    assert "expected_job_id=" not in azure
    assert "expected_statuses={TaskStatus.EXECUTING}" in gitlab
    assert "expected_statuses={TaskStatus.EXECUTING}" in azure


def test_fail_issue_is_issue_keyed_not_generation_keyed(
    processor, state_manager, fake_jira
):
    """Watchdog / Stop: fail the live ticket, not a remembered job id."""
    _executing(state_manager, "CAS-FAIL-2", job_id="job-live", task_id="task-live")
    processor._fail_issue("CAS-FAIL-2", "stuck watchdog")
    live = state_manager.get_state("CAS-FAIL-2")
    assert live.status == TaskStatus.ERROR


def test_fail_issue_still_creates_dashboard_job_on_validation_path(
    processor, state_manager, fake_jira
):
    """Intentional: Mode/template fail never called begin, but Jobs must show a row."""
    state_manager.create_state("CAS-JOB-1", "s", "d")
    state_manager.update_state("CAS-JOB-1", status=TaskStatus.PENDING)
    processor._fail_issue("CAS-JOB-1", "missing Mode")
    live = state_manager.get_state("CAS-JOB-1")
    assert live.status == TaskStatus.ERROR
    rows = processor.job_store.list_jobs(issue_key="CAS-JOB-1", limit=5)
    assert rows, "template/Mode fail must still create a Jobs-tab row"


def test_queue_persists_inbound_repository_url_as_received(tmp_path):
    """Intentional: queue stores the URL as received; clone strips userinfo."""
    store = WorkQueueStore(queue_dir=tmp_path / "q")
    raw = "https://oauth2:super-secret-pat@git.example.com/g/r.git"
    rec = store.enqueue(
        source="gitlab",
        issue_key="KAN-9",
        repository_url=raw,
        source_branch="feature/x",
        work_branch="feature/x",
        target_branch="develop",
    )
    saved = store.get(rec["queue_id"])
    assert saved["repository_url"] == raw


def test_issue_params_keeps_repository_url_text():
    """Intentional: {params} Repository is stored as written; clone strips."""
    from src.issue_git_spec import parse_issue_git_spec

    spec, err = parse_issue_git_spec(
        "KAN-1",
        "{params}\n"
        "Repository: https://user:leaked-token@git.example.com/acme/app.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}",
    )
    assert err is None
    assert spec is not None
    assert spec.repository_url == (
        "https://user:leaked-token@git.example.com/acme/app.git"
    )
