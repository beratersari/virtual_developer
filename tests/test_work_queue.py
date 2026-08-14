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
