"""Prove Stop + queued GitLab follow-up cannot be stamped by the old worker.

Production GitLab/Azure jobs run from ``_run_queue_item`` (webhook enqueue),
not ``handle_gitlab_mr_comment``. The handle path takes ``_get_issue_lock``;
the queue path must too. Dashboard Stop CAS-writes CANCELLED, releases the
clone, and ``_kick_queue``s. A leftover ``/yaver`` must not begin EXECUTING
until the cancelled worker has left the issue — otherwise the old GitLab
success CAS (``expected_statuses={EXECUTING}``, no job id) completes the
new run and ``_release_context`` deletes the new clone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from src.gitlab.webhook import GitlabMrNoteEvent
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor, _JobSlotLimiter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


class _DummyGit:
    def __init__(self, issue_key: str, temp_dir: Path) -> None:
        self.issue_key = issue_key
        self.temp_dir = temp_dir
        self.cleaned: list[Optional[bool]] = []

    def cleanup(self, success: Optional[bool] = None) -> None:
        self.cleaned.append(success)

    def get_working_directory(self) -> Path:
        return self.temp_dir


def _note(issue: str, note_id: str) -> GitlabMrNoteEvent:
    return GitlabMrNoteEvent(
        issue_key=issue,
        note_id=note_id,
        note_body=f"@bot /yaver {note_id}",
        prompt=note_id,
        author_username="alice",
        author_name="Alice",
        project_id=1,
        project_path="acme/demo",
        repository_url="https://gitlab.example.com/acme/demo.git",
        host="gitlab.example.com",
        mr_iid=4,
        mr_title=f"feat({issue}): work",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url="https://gitlab.example.com/acme/demo/-/merge_requests/4",
        discussion_id=f"disc-{note_id}",
    )


def _processor(tmp_path: Path, isolate: Dict[str, Any], monkeypatch) -> JobProcessor:
    from src.config import settings
    from src.reporter.jira_reporter import JiraReporter
    from tests.conftest import FakeJiraClient

    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    monkeypatch.setattr(settings, "jira_enabled", False)
    fake = FakeJiraClient()
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    proc.jira_client = fake
    proc.reporter = JiraReporter(client=fake)
    proc._job_semaphore = _JobSlotLimiter(4)
    return proc


@pytest.mark.asyncio
async def test_stop_queued_gitlab_followup_waits_for_old_queue_worker(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Dashboard Stop must not let a leftover /yaver run beside the old worker.

    First queue item holds inside ``_start_gitlab_mr_workflow`` (agent still
    running). Cancel keeps the second note queued and kicks dispatch. The
    follow-up must not call begin/CAS until the first worker returns —
    the same serialization ``handle_gitlab_mr_comment`` gets from the
    per-issue lock.
    """
    isolate = isolate_jira_agent_artifacts
    proc = _processor(tmp_path, isolate, monkeypatch)
    issue = "GL-STALE-Q"
    old_git = _DummyGit(issue, tmp_path / "old-clone")
    new_git = _DummyGit(issue, tmp_path / "new-clone")
    (tmp_path / "old-clone").mkdir()
    (tmp_path / "new-clone").mkdir()

    first_holding = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def fake_start(state, event):
        git = old_git if event.note_id == "n1" else new_git
        job_id = proc._begin_workflow_run(
            state,
            status=TaskStatus.EXECUTING,
            task=AgentTask(
                description=f"note {event.note_id}",
                prompt=event.prompt,
                agent="derman-build",
                issue_key=issue,
            ),
            workflow_type="gitlab_mr",
            agent="derman-build",
            job_status="executing",
            allow_from_failed=True,
        )
        proc._contexts[issue] = {"git": git, "runner": None}
        order.append(f"start:{event.note_id}")
        if event.note_id == "n1":
            first_holding.set()
            await first_release.wait()
            order.append("old-exit")
            return job_id
        second_started.set()
        order.append("second-running")
        return job_id

    proc._start_gitlab_mr_workflow = fake_start  # type: ignore[method-assign]

    first = proc.queue_store.enqueue(
        source="gitlab",
        issue_key=issue,
        summary="first /yaver",
        payload=_note(issue, "n1").to_dict(),
    )
    leftover = proc.queue_store.enqueue(
        source="gitlab",
        issue_key=issue,
        summary="second /yaver",
        payload=_note(issue, "n2").to_dict(),
    )

    first_task = asyncio.create_task(
        proc._run_queue_item(proc.queue_store.get(first["queue_id"]))
    )
    await asyncio.wait_for(first_holding.wait(), timeout=2.0)
    proc.queue_store.update(first["queue_id"], status="running")
    live = proc.state_manager.get_state(issue)
    assert live is not None
    assert live.status == TaskStatus.EXECUTING
    old_job = (live.metadata or {}).get("current_job_id")
    assert old_job

    cancelled = await proc.cancel_job(issue, reason="operator stop")
    assert cancelled.get("ok") is True
    assert proc.state_manager.get_state(issue).status == TaskStatus.CANCELLED
    leftover_row = proc.queue_store.get(leftover["queue_id"])
    assert leftover_row is not None
    assert leftover_row.get("status") == "queued"

    await asyncio.sleep(0)
    await proc.dispatch_queue()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not second_started.is_set(), (
        f"Queued GitLab follow-up began while the cancelled worker was still "
        f"inside _start_gitlab_mr_workflow (order={order}). "
        "_run_queue_item must take _get_issue_lock like handle_gitlab_mr_comment."
    )
    assert proc.state_manager.get_state(issue).status == TaskStatus.CANCELLED, (
        "Follow-up reset CANCELLED before the old worker exited"
    )

    first_release.set()
    await asyncio.wait_for(first_task, timeout=2.0)
    await asyncio.sleep(0)
    await proc.dispatch_queue()
    try:
        await asyncio.wait_for(second_started.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        leftover_now = proc.queue_store.get(leftover["queue_id"])
        pytest.fail(
            "Follow-up /yaver never started after the old worker exited "
            f"(leftover={leftover_now} order={order})"
        )

    live = proc.state_manager.get_state(issue)
    assert live is not None
    assert live.status == TaskStatus.EXECUTING
    new_job = (live.metadata or {}).get("current_job_id")
    assert new_job and new_job != old_job
    assert new_git.cleaned == []
    assert proc._contexts.get(issue, {}).get("git") is new_git
    assert order[:3] == ["start:n1", "old-exit", "start:n2"], order


def test_run_queue_item_takes_issue_lock_for_gitlab_and_azure():
    """Webhook jobs run from the queue, not handle_*; they need the same lock."""
    import inspect

    src = inspect.getsource(JobProcessor._run_queue_item)
    gitlab = src.split('source == "gitlab"', 1)[1].split("elif source ==", 1)[0]
    azure = src.split('source == "azure"', 1)[1].split("else:", 1)[0]
    assert "async with self._get_issue_lock(event.issue_key)" in gitlab
    assert "async with self._get_issue_lock(event.issue_key)" in azure
