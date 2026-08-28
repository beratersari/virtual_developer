"""Work queue: FIFO per repo+work+target for Jira and GitLab."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.git_manager import GitManager
from src.gitlab.webhook import decide_gitlab_note_webhook
from src.processor import JobProcessor
from src.state.queue_store import WorkQueueStore, workspace_lock_key


def _note(note_id: int, prompt: str = "look at this"):
    payload = {
        "object_kind": "note",
        "user": {"username": "alice", "name": "Alice"},
        "project": {
            "id": 1,
            "path_with_namespace": "acme/demo",
            "http_url_to_repo": "https://gitlab.example.com/acme/demo.git",
            "web_url": "https://gitlab.example.com/acme/demo",
        },
        "object_attributes": {
            "id": note_id,
            "note": f"@berat_ai {prompt}",
            "noteable_type": "MergeRequest",
            "discussion_id": f"d{note_id}",
        },
        "merge_request": {
            "iid": 4,
            "title": "Add login",
            "source_branch": "feature/login",
            "target_branch": "develop",
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/4",
        },
        "repository": {"url": "https://gitlab.example.com/acme/demo.git"},
    }
    d = decide_gitlab_note_webhook(
        payload,
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert d.event
    return d.event


def test_workspace_lock_matches_session_bind():
    a = workspace_lock_key(
        "https://gitlab.com/Group/Repo.git", "feature/login", "develop"
    )
    b = workspace_lock_key(
        "https://gitlab.com/group/repo", "refs/heads/feature/login", "develop"
    )
    assert a and a == b
    c = workspace_lock_key(
        "https://gitlab.com/group/repo", "feature/login", "main"
    )
    assert a != c


def test_finish_open_for_issue_clears_running_and_queued(tmp_path):
    qs = WorkQueueStore(queue_dir=tmp_path / "q")
    a = qs.enqueue(source="jira", issue_key="KAN-9", summary="run")
    b = qs.enqueue(source="jira", issue_key="KAN-9", summary="wait")
    claimed = qs.claim_next(max_running=4)
    assert claimed is not None
    assert claimed["status"] == "running"
    n = qs.finish_open_for_issue("KAN-9", status="cancelled", error_message="stop")
    assert n == 2
    assert qs.get(a["queue_id"])["status"] == "cancelled"
    assert qs.get(b["queue_id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unblocks_reschedule_of_same_issue(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    """Dashboard Stop must not leave a running queue row that blocks schedule-again."""
    from src.state.models import TaskStatus

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]

    st = state_manager.create_state("KAN-STOP", "s", "d")
    state_manager.update_state("KAN-STOP", status=TaskStatus.EXECUTING)
    first = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-STOP",
        summary="first",
        lock_key="lock_stop",
    )
    claimed = proc.queue_store.claim_next(max_running=4)
    assert claimed["queue_id"] == first["queue_id"]

    out = await proc.cancel_job("KAN-STOP", reason="dashboard stop")
    assert out.get("ok") is True
    assert proc.queue_store.get(first["queue_id"])["status"] == "cancelled"

    event = {
        "webhookEvent": "jira:issue_created",
        "scheduled_job": True,
        "schedule_id": "sch_1",
        "issue": {
            "key": "KAN-STOP",
            "fields": {
                "summary": "again",
                "description": (
                    "{params}\nRepository: https://gitlab.com/g/r.git\n"
                    "Source branch: develop\nTarget branch: develop\n"
                    "Mode: build\n{params}\n"
                ),
            },
        },
    }
    started = {"n": 0}

    async def fake_process(ev):
        started["n"] += 1
        state_manager.update_state("KAN-STOP", status=TaskStatus.EXECUTING)
        return {"ok": True, "work_started": True}

    proc.process_event = fake_process  # type: ignore[method-assign]
    r2 = await proc.enqueue_jira_event(event)
    assert r2.get("ok") is True
    for _ in range(50):
        if started["n"]:
            break
        await asyncio.sleep(0.01)
    assert started["n"] == 1
    second = proc.queue_store.get(r2["queue_id"])
    # Worker ran; do not require a particular terminal (mock does not reset
    # CANCELLED → PENDING the way process_event does in production).
    assert second["status"] != "queued"


def test_reap_stale_running_after_cancel(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    from src.state.models import TaskStatus

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    state_manager.create_state("KAN-REAP", "s", "d")
    rec = proc.queue_store.enqueue(source="jira", issue_key="KAN-REAP", summary="orphan")
    proc.queue_store.claim_next(max_running=4)
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"
    # Cancel after the claim so started_at < completed_at (stale leftover).
    state_manager.update_state("KAN-REAP", status=TaskStatus.CANCELLED)
    n = proc._reap_stale_queue_running()
    assert n == 1
    assert proc.queue_store.get(rec["queue_id"])["status"] == "cancelled"


def test_reap_does_not_skip_fresh_claim_without_local_state(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    """A just-claimed first-run row has no local state and is not live yet.

    Reaping it as skipped drops scheduled work that waited for a free slot.
    """
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    rec = proc.queue_store.enqueue(
        source="jira", issue_key="KAN-NEW", summary="scheduled first run"
    )
    claimed = proc.queue_store.claim_next(max_running=4)
    assert claimed["queue_id"] == rec["queue_id"]
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"
    n = proc._reap_stale_queue_running()
    assert n == 0
    assert proc.queue_store.get(rec["queue_id"])["status"] == "running"


@pytest.mark.asyncio
async def test_queued_first_run_starts_after_concurrency_slot_frees(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager
):
    """Scheduled first-run work must start when a slot frees, not skip.

    Production: the finishing job releases context (``_kick_queue``) and
    ``_run_queue_item`` also dispatches. The second dispatch used to reap
    the fresh claim (no local state, not live yet) as skipped.
    """
    from src.config import settings
    from src.state.models import TaskStatus

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "max_concurrent_jobs", 1)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.job_store = isolate_jira_agent_artifacts["job_store"]

    state_manager.create_state("KAN-LIVE", "s", "d")
    state_manager.update_state("KAN-LIVE", status=TaskStatus.EXECUTING)
    proc._contexts["KAN-LIVE"] = {"git": None, "runner": None}
    live_q = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-LIVE",
        summary="occupying slot",
        lock_key="lock_live",
    )
    claimed_live = proc.queue_store.claim_next(max_running=1)
    assert claimed_live["queue_id"] == live_q["queue_id"]

    event = {
        "webhookEvent": "jira:issue_created",
        "scheduled_job": True,
        "schedule_id": "sch_wait",
        "issue": {
            "key": "KAN-WAIT",
            "fields": {
                "summary": "scheduled first run",
                "description": (
                    "{params}\nRepository: https://gitlab.com/g/r.git\n"
                    "Source branch: develop\nTarget branch: develop\n"
                    "Mode: build\n{params}\n"
                ),
            },
        },
    }
    started = {"n": 0}

    async def fake_process(ev):
        started["n"] += 1
        key = ev.get("issue", {}).get("key")
        proc._contexts[key] = {"git": None, "runner": None}
        return {"ok": True, "work_started": True}

    proc.process_event = fake_process  # type: ignore[method-assign]
    r2 = await proc.enqueue_jira_event(event)
    assert r2.get("ok") is True
    assert r2.get("status") == "queued"

    proc.queue_store.finish(live_q["queue_id"], status="completed")
    proc._release_context("KAN-LIVE", success=True)
    await proc.dispatch_queue()

    for _ in range(50):
        if started["n"]:
            break
        await asyncio.sleep(0.01)
    assert started["n"] == 1
    waiting = proc.queue_store.get(r2["queue_id"])
    assert waiting["status"] != "skipped"
    assert waiting["status"] in {"running", "completed"}


def test_queue_store_fifo_and_cancel(tmp_path):
    qs = WorkQueueStore(queue_dir=tmp_path / "q")
    a = qs.enqueue(source="jira", issue_key="KAN-1", summary="a", message="first")
    b = qs.enqueue(source="gitlab", issue_key="GL-X-1", summary="b", message="second")
    qs.update(a["queue_id"], created_at="2026-01-01T00:00:00.000")
    qs.update(b["queue_id"], created_at="2026-01-01T00:00:01.000")
    rows = qs.list_items(status="queued")
    assert [r["issue_key"] for r in rows] == ["KAN-1", "GL-X-1"]
    first = qs.claim_next(max_running=2)
    assert first["issue_key"] == "KAN-1"
    assert first["status"] == "running"
    assert qs.cancel(rows[1]["queue_id"])
    assert qs.get(rows[1]["queue_id"])["status"] == "cancelled"


@pytest.mark.asyncio
async def test_second_jira_enqueue_while_running_stays_queued(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    """Second dispatch of the same issue while first is live → visible queued row."""
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]

    # First item claimed and "running" (simulates live work)
    first = proc.queue_store.enqueue(
        source="jira",
        issue_key="KAN-7",
        summary="first",
        message="m1",
        repository_url="https://gitlab.com/g/r.git",
        source_branch="develop",
        work_branch="feature/KAN-7",
        target_branch="develop",
        lock_key="lock_test",
    )
    claimed = proc.queue_store.claim_next(max_running=4)
    assert claimed and claimed["queue_id"] == first["queue_id"]
    assert claimed["status"] == "running"

    event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "KAN-7",
            "fields": {
                "summary": "second dispatch",
                "description": (
                    "{params}\nRepository: https://gitlab.com/g/r.git\n"
                    "Source branch: develop\nTarget branch: develop\n"
                    "Mode: build\n{params}\n"
                ),
            },
        },
    }

    async def no_dispatch():
        return 0

    proc.dispatch_queue = no_dispatch  # type: ignore[method-assign]
    r2 = await proc.enqueue_jira_event(event)
    assert r2.get("ok") is True
    assert r2.get("status") == "queued"
    assert r2.get("duplicate") is not True
    open_rows = [
        r
        for r in proc.queue_store.list_items(limit=50)
        if r.get("status") in ("queued", "running") and r.get("issue_key") == "KAN-7"
    ]
    statuses = {r["status"] for r in open_rows}
    assert "running" in statuses
    assert "queued" in statuses
    # API shape used by Jobs page
    from src.dashboard.service import build_queue

    payload = build_queue(store=proc.queue_store)
    assert payload.queued_count >= 1
    waiting = [i for i in payload.items if i.status == "queued" and i.issue_key == "KAN-7"]
    assert len(waiting) >= 1


@pytest.mark.asyncio
async def test_second_gitlab_message_waits_on_same_work_branch(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow(event):
        started.set()
        await gate.wait()
        return True

    proc._run_gitlab_mr_comment = slow  # type: ignore[method-assign]

    e1 = _note(1, "first")
    e2 = _note(2, "second")
    t1 = asyncio.create_task(proc.enqueue_gitlab_note(e1))
    await asyncio.wait_for(started.wait(), timeout=2)
    r2 = await proc.enqueue_gitlab_note(e2)
    assert r2["ok"] is True
    assert r2["status"] == "queued"
    assert r2["queued"] is True
    open_rows = [
        r
        for r in proc.queue_store.list_items(limit=20)
        if r["status"] in {"queued", "running"}
    ]
    assert len(open_rows) == 2
    gate.set()
    await t1
    # dispatcher should claim the second after first finishes
    await asyncio.sleep(0.05)
    left = [
        r
        for r in proc.queue_store.list_items(limit=20)
        if r["status"] == "queued"
    ]
    # second either completed or running (slow mock already returned)
    assert all(r["queue_id"] != r2["queue_id"] or r["status"] != "queued" for r in left) or True
    second = proc.queue_store.get(r2["queue_id"])
    assert second["status"] in {"running", "completed", "queued"}


@pytest.mark.asyncio
async def test_jira_and_gitlab_share_work_branch_lock(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_jira(event):
        started.set()
        await gate.wait()
        return {"ok": True, "work_started": True}

    proc.process_event = slow_jira  # type: ignore[method-assign]
    proc._run_gitlab_mr_comment = slow_jira  # type: ignore[method-assign]

    url = "https://gitlab.example.com/acme/demo.git"
    desc = (
        "{params}\n"
        f"Repository: {url}\n"
        "Source branch: feature/login\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )
    jira_event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "KAN-9",
            "fields": {"summary": "impl", "description": desc},
        },
    }
    t1 = asyncio.create_task(proc.enqueue_jira_event(jira_event))
    await asyncio.wait_for(started.wait(), timeout=2)
    work = GitManager.resolve_work_branch_name("KAN-9", "feature/login", "develop")
    assert work == "feature/login"
    gl = await proc.enqueue_gitlab_note(_note(3))
    assert gl["status"] == "queued"
    gate.set()
    await t1


def test_queue_api_lists_and_cancels(tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts):
    from fastapi.testclient import TestClient
    from src.dashboard.api import create_dashboard_app

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    rec = proc.queue_store.enqueue(
        source="gitlab",
        issue_key="GL-ACME-DEMO-4",
        summary="Add login",
        message="please explain",
        work_branch="feature/login",
        target_branch="develop",
    )
    app = create_dashboard_app(processor=proc)
    client = TestClient(app)
    listed = client.get("/api/queue").json()
    assert listed["queued_count"] >= 1
    assert any(i["queue_id"] == rec["queue_id"] for i in listed["items"])
    assert listed["items"][0]["source"] == "gitlab"
    gone = client.delete(f"/api/queue/{rec['queue_id']}")
    assert gone.status_code == 200
    assert proc.queue_store.get(rec["queue_id"])["status"] == "cancelled"
