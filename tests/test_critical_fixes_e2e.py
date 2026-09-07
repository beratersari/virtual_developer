"""End-to-end verification of the four critical fixes.

These drive ServeOrchestrator over a real httpx client, the GitLab work
queue + processor, and the planning workflow — not isolated helpers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.git_manager import GitManager
from src.gitlab.webhook import decide_gitlab_note_webhook
from src.opencode_serve import OpenCodeServeClient, ServeOrchestrator
from src.processor import JobProcessor
from src.state.models import TaskStatus
from src.state.queue_store import workspace_lock_key
from tests.test_opencode_serve_e2e import FakeServeBackend


# ---------------------------------------------------------------------------
# In-process OpenCode serve (httpx MockTransport) — real HTTP client
# ---------------------------------------------------------------------------


class _ServeHttpApp:
    """Enough of opencode serve for ServeOrchestrator, with wrap/500 knobs."""

    def __init__(self, backend: FakeServeBackend, *, wrap: bool = False, fail_lists: bool = False):
        self.backend = backend
        self.wrap = wrap
        self.fail_lists = fail_lists

    def _pack(self, payload: Any) -> Any:
        if self.wrap and isinstance(payload, list):
            return {"data": payload}
        return payload

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()

        if method == "GET" and path.endswith("/global/health"):
            return httpx.Response(200, json=await self.backend.health())

        if method == "POST" and path.rstrip("/").endswith("/session"):
            body = json.loads(request.content.decode() or "{}")
            sess = await self.backend.create_session(body.get("title") or "t")
            return httpx.Response(200, json=sess)

        if method == "GET" and path.rstrip("/").endswith("/session/status"):
            return httpx.Response(200, json=await self.backend.session_status())

        parts = [p for p in path.split("/") if p]
        # .../session/{id}/message|todo|abort
        if "session" in parts:
            i = parts.index("session")
            if i + 1 < len(parts):
                sid = parts[i + 1]
                tail = parts[i + 2] if i + 2 < len(parts) else ""
                if method == "POST" and tail == "message":
                    body = json.loads(request.content.decode() or "{}")
                    text = ""
                    for p in body.get("parts") or []:
                        if isinstance(p, dict) and p.get("text"):
                            text = str(p["text"])
                            break
                    msg = await self.backend.send_message(sid, text)
                    return httpx.Response(200, json=msg)
                if method == "GET" and tail == "message":
                    if self.fail_lists:
                        return httpx.Response(500, json={"error": "list failed"})
                    msgs = await self.backend.list_all_messages(sid)
                    return httpx.Response(200, json=self._pack(msgs))
                if method == "GET" and tail == "todo":
                    if self.fail_lists:
                        return httpx.Response(500, json={"error": "todos failed"})
                    todos = await self.backend.list_todos(sid)
                    return httpx.Response(200, json=self._pack(todos))
                if method == "POST" and tail == "abort":
                    await self.backend.abort(sid)
                    return httpx.Response(204)

        return httpx.Response(404, json={"error": path})


def _serve_client(backend: FakeServeBackend, **app_kw) -> OpenCodeServeClient:
    app = _ServeHttpApp(backend, **app_kw)
    transport = httpx.MockTransport(app.handle)
    http = httpx.AsyncClient(
        base_url="http://opencode.test/",
        transport=transport,
        verify=False,
        timeout=30.0,
    )
    return OpenCodeServeClient(
        "http://opencode.test",
        timeout_seconds=30.0,
        client=http,
    )


@pytest.mark.asyncio
async def test_e2e_wrapped_message_list_does_not_false_complete():
    """GET /message returns {data: [...]} — must still see compact-then-stop."""
    backend = FakeServeBackend(required_compacts=8)
    backend.auto_complete_on_idle = False
    client = _serve_client(backend, wrap=True)
    try:
        orch = ServeOrchestrator(
            client=client,
            compact_wait_seconds=0.35,
            compact_poll_seconds=0.05,
            compact_settle_seconds=0.08,
        )
        result = await orch.run(prompt="long job", title="KAN-WRAP: compact")
    finally:
        await client.aclose()

    assert result.returncode != 0, result.stderr
    assert result.continue_count == 0
    assert backend.message_calls == 1
    blob = " ".join(backend.prompts)
    assert "Finish remaining todos" not in blob
    reasons = " ".join(str(r) for r in (result.incomplete_reasons or []))
    assert (
        "compact" in reasons.lower()
        or "todo" in reasons.lower()
        or result.timed_out
    )


@pytest.mark.asyncio
async def test_e2e_list_500_does_not_false_complete():
    """Message/todo list HTTP 500 must not be treated as a finished job."""
    backend = FakeServeBackend(required_compacts=3)
    backend.auto_complete_on_idle = False
    client = _serve_client(backend, fail_lists=True)
    try:
        orch = ServeOrchestrator(
            client=client,
            compact_wait_seconds=0.35,
            compact_poll_seconds=0.05,
            compact_settle_seconds=0.08,
        )
        result = await orch.run(prompt="long job", title="KAN-500: compact")
    finally:
        await client.aclose()

    assert result.returncode != 0, result.stderr
    reasons = " ".join(str(r) for r in (result.incomplete_reasons or []))
    assert (
        "snapshot unavailable" in reasons
        or "compact" in reasons.lower()
        or result.timed_out
    )


@pytest.mark.asyncio
async def test_e2e_auto_resume_still_succeeds_over_http():
    """Happy path: wrap + auto-resume still completes (unwrap must work)."""
    backend = FakeServeBackend(required_compacts=2)
    backend.auto_complete_on_idle = True
    client = _serve_client(backend, wrap=True)
    try:
        orch = ServeOrchestrator(
            client=client,
            compact_wait_seconds=1.0,
            compact_poll_seconds=0.05,
            compact_settle_seconds=0.08,
        )
        result = await orch.run(prompt="finish the work", title="KAN-OK")
    finally:
        await client.aclose()

    assert result.returncode == 0, result.stderr
    assert result.incomplete is False
    assert backend.message_calls == 1


# ---------------------------------------------------------------------------
# GitLab queue: lock identity + defer must not drop the note
# ---------------------------------------------------------------------------


def _gitlab_note(
    *,
    note_id: int,
    iid: int,
    source: str,
    target: str,
    prompt: str = "please look",
):
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
            "iid": iid,
            "title": f"MR {iid}",
            "source_branch": source,
            "target_branch": target,
            "web_url": f"https://gitlab.example.com/acme/demo/-/merge_requests/{iid}",
        },
        "repository": {"url": "https://gitlab.example.com/acme/demo.git"},
    }
    d = decide_gitlab_note_webhook(
        payload,
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert d.event, d.reason
    return d.event


@pytest.mark.asyncio
async def test_e2e_gitlab_primary_source_mrs_share_queue_lock(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    """Two develop→main MR comments must serialize on the same clone lock."""
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]

    started = asyncio.Event()
    gate = asyncio.Event()
    ran: List[str] = []

    async def slow(event):
        ran.append(event.note_id)
        started.set()
        await gate.wait()
        return True

    proc._run_gitlab_mr_comment = slow  # type: ignore[method-assign]

    e1 = _gitlab_note(note_id=11, iid=10, source="develop", target="main", prompt="one")
    e2 = _gitlab_note(note_id=12, iid=11, source="develop", target="main", prompt="two")
    assert e1.issue_key != e2.issue_key

    clone_work = GitManager.resolve_work_branch_name(
        e1.issue_key, "develop", "main", keep_source=True
    )
    assert clone_work == "develop"
    expected_lock = workspace_lock_key(
        e1.repository_url, clone_work, "main"
    )

    t1 = asyncio.create_task(proc.enqueue_gitlab_note(e1))
    await asyncio.wait_for(started.wait(), timeout=3)
    r2 = await proc.enqueue_gitlab_note(e2)
    rec1 = proc.queue_store.find_note("11")
    rec2 = proc.queue_store.find_note("12")
    assert rec1 and rec2
    assert rec1["lock_key"] == rec2["lock_key"] == expected_lock
    assert r2["status"] == "queued"
    assert rec2["status"] == "queued"
    assert rec1["work_branch"] == "develop"
    gate.set()
    await t1


@pytest.mark.asyncio
async def test_e2e_gitlab_deferred_note_is_not_marked_completed(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    """In-flight defer must requeue and later actually start the same note."""
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]

    started: List[str] = []
    live_hits = {"n": 0}

    def is_live(_key: str) -> bool:
        # First accept attempt is in-flight → defer. Redispatch must start.
        live_hits["n"] += 1
        return live_hits["n"] == 1

    proc._is_live_processing = is_live  # type: ignore[method-assign]

    async def fake_start(state, event):
        started.append(event.note_id)

    proc._start_gitlab_mr_workflow = fake_start  # type: ignore[method-assign]

    event = _gitlab_note(
        note_id=99, iid=22, source="feature/x", target="develop", prompt="retry me"
    )
    enq = await proc.enqueue_gitlab_note(event)
    qid = enq["queue_id"]
    await asyncio.sleep(0.15)
    rec = proc.queue_store.get(qid)
    assert rec is not None
    assert rec["status"] == "completed", rec
    assert started == ["99"], (
        "deferred note was dropped (seen-before-accept) or never started"
    )
    assert live_hits["n"] >= 2
    assert "99" in proc._gitlab_seen_notes


# ---------------------------------------------------------------------------
# Planning workflow: must not adopt another issue's markdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_plan_workflow_rejects_foreign_markdown(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts, state_manager, reporter
):
    """Shared clone has KAN-1.md only. KAN-2 plan must ERROR, not plan_ready."""
    monkeypatch.chdir(tmp_path)
    plans_host = tmp_path / "durable-plans"
    plans_host.mkdir()
    work = tmp_path / "shared-clone"
    sis = work / ".sisyphus" / "plans"
    sis.mkdir(parents=True)
    (sis / "KAN-1.md").write_text("# plan for KAN-1\nDo the other ticket.\n", encoding="utf-8")

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira

    git = MagicMock()
    git.get_working_directory.return_value = str(work)
    git.work_branch = "feature/shared-review"
    git.target_branch = "develop"
    git.ensure_feature_branch.return_value = "feature/shared-review"
    git.ensure_on_work_branch.return_value = True

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "planned",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_plan",
            "retry_info": {"attempts": 1, "max_retries": 0, "retried": False},
            "timed_out": False,
        }
    )

    async def fake_prepare(state):
        proc._contexts[state.issue_key] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    proc._prepare_git_workspace = fake_prepare  # type: ignore[method-assign]
    proc._mark_jira_in_progress = lambda key: None  # type: ignore[method-assign]

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/acme/demo.git\n"
        "Source branch: feature/shared-review\n"
        "Target branch: develop\n"
        "Mode: plan\n"
        "{params}\n"
    )
    state = state_manager.create_state("KAN-2", "plan the other thing", desc)

    with patch("src.processor.settings") as s:
        s.full_plans_dir = plans_host
        s.sisyphus_plans_dir = Path(".sisyphus/plans")
        s.default_agent = "atlas"
        s.agent_task_timeout_seconds = 30
        s.agent_task_max_retries = 0
        s.agent_task_max_incomplete_retries = 0
        await proc._start_planning_workflow(state)

    live = state_manager.get_state("KAN-2")
    assert live is not None
    assert live.status == TaskStatus.ERROR, live.status
    assert live.status != TaskStatus.PLAN_READY
    err = (live.error_message or "").lower()
    assert "no plan file" in err
    # Must not have copied KAN-1.md into KAN-2.md
    assert not (sis / "KAN-2.md").exists()
    assert not (plans_host / "KAN-2.md").exists()
