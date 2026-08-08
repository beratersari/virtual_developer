"""Exact-scenario tests from the 2026 multi-agent review.

Passing tests lock fixes (or already-correct behaviour).
``xfail(strict=True)`` encodes remaining production bugs: when the bug is
fixed the XPASS forces the marker to be removed.

These are *not* design-choice cases from AGENTS.md (plan_ready wait, fail →
In Progress → To Do requeue, verify=False, dashboard no-auth v1).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.state.models import TaskStatus


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    p._last_jira_status = {}
    p._seen_issues = set()
    return p


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _todo_fields(name="To Do", labels=None, summary="s", description=None):
    fields = {
        "summary": summary,
        "status": {"name": name, "statusCategory": {"key": "new"}},
        "labels": labels or ["bot"],
        "assignee": None,
    }
    if description is not None:
        fields["description"] = description
    return fields


def _http_resp(status=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.text = text or str(json_data)
    r.json.return_value = json_data if json_data is not None else {}
    r.raise_for_status = MagicMock()
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=r
        )
    return r


# ---------------------------------------------------------------------------
# R1 — append_to_description must request real field names (fixed)
# ---------------------------------------------------------------------------


def test_r1_jira_fields_query_does_not_split_string():
    from src.jira.client import _jira_fields_query

    assert _jira_fields_query("description") == "description"
    assert _jira_fields_query(["summary", "description"]) == "summary,description"
    assert _jira_fields_query("") is None
    assert _jira_fields_query(None) is None


def test_r1_append_to_description_requests_description_not_characters():
    """Plan append must GET fields=description (not d,e,s,c,r,i,p,t,i,o,n)."""
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            http.get.return_value = _http_resp(
                200, {"fields": {"description": "{params}\nMode: plan\n{params}"}}
            )
            http.put.return_value = _http_resp(204)
            ok = c.append_to_description("PLAN-1", "h3. AI Agent — Plan")
            assert ok is True
            params = http.get.call_args.kwargs.get("params") or {}
            assert params.get("fields") == "description"
            assert "d,e,s,c" not in str(params.get("fields") or "")
            put_desc = http.put.call_args.kwargs["json"]["fields"]["description"]
            assert "{params}" in put_desc
            assert "Mode: plan" in put_desc
            assert "AI Agent — Plan" in put_desc


def test_r1_append_to_description_fails_closed_when_get_issue_returns_none():
    """If current description cannot be read, do not PUT a plan-only body."""
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            http.get.return_value = _http_resp(404, {"errorMessages": ["not found"]})
            ok = c.append_to_description("PLAN-2", "plan block that must not wipe")
            assert ok is False
            http.put.assert_not_called()


def test_r1_get_issue_string_fields_arg_is_not_character_joined():
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            http.get.return_value = _http_resp(200, {"key": "P-1", "fields": {}})
            c.get_issue("P-1", fields="description")
            params = http.get.call_args.kwargs.get("params") or {}
            assert params.get("fields") == "description"


# ---------------------------------------------------------------------------
# R2 — schedule finish must not overwrite operator cancel (CAS)
# ---------------------------------------------------------------------------


def test_r2_finish_after_cancel_keeps_cancelled(tmp_path):
    from src.scheduler.service import _finish_schedule_dispatch, cancel_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="d",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        issue_key="SCH-1",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    assert store.claim_due(sid) is not None
    cancel_scheduled_job(sid, store=store)
    _finish_schedule_dispatch(store, sid, status="dispatched")
    final = store.get(sid)
    assert final is not None
    assert final["status"] == "cancelled"
    assert final.get("dispatched_at") is None


def test_r2_finish_must_not_overwrite_cancel_during_update(tmp_path):
    from src.scheduler.service import _finish_schedule_dispatch, cancel_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="d",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        issue_key="SCH-2",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    assert store.claim_due(sid) is not None

    real_update = store.update

    def update_after_cancel(schedule_id, **fields):
        # Simulate operator cancel landing between get and write without
        # re-entering this wrapper (cancel_scheduled_job also calls update).
        real_update(schedule_id, status="cancelled", error_message=None)
        return real_update(schedule_id, **fields)

    store.update = update_after_cancel  # type: ignore[method-assign]
    _finish_schedule_dispatch(store, sid, status="dispatched")
    final = store.get(sid)
    assert final is not None
    assert final["status"] == "cancelled", (
        f"late dispatched must not clobber cancel; got {final['status']}"
    )
    assert final.get("dispatched_at") is None


# ---------------------------------------------------------------------------
# R3 — schedule existing To Do + trigger must not be stolen by poller
# ---------------------------------------------------------------------------


def test_r3_schedule_existing_todo_must_not_be_poller_eligible_before_fire(
    poller, tmp_path, monkeypatch
):
    from src.config import settings
    from src.scheduler.service import schedule_existing_issue
    from src.state.schedule_store import ScheduleStore

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}"
    )
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "SCH-9",
        "fields": {
            "summary": "run tomorrow",
            "description": desc,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "issuetype": {"name": "Task"},
            "labels": ["bot"],
        },
    }
    client.transition_to_in_progress.return_value = False
    client.add_labels.return_value = True
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    out = schedule_existing_issue(
        "SCH-9",
        scheduled_at=(datetime.now() + timedelta(days=1)).isoformat(
            timespec="seconds"
        ),
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    poller.schedule_store = store

    issue = {"key": "SCH-9", "fields": _todo_fields(labels=["bot"], summary="run tomorrow")}
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    keys = [i["key"] for i in poller.poll_board()]
    assert "SCH-9" not in keys, (
        "pending schedule must suppress poller intake until scheduled_at"
    )


# ---------------------------------------------------------------------------
# R4 — _ensure_agent_runner must not adopt a released foreign runner
# ---------------------------------------------------------------------------


def test_r4_ensure_agent_runner_must_not_adopt_released_foreign_runner(
    processor, tmp_path
):
    from src.orchestrator.agent_runner import AgentRunner

    dead_dir = tmp_path / "issue_a_clone"
    dead_dir.mkdir()
    leftover = AgentRunner(working_directory=dead_dir)
    processor._contexts["A-1"] = {"git": None, "runner": leftover}
    processor.agent_runner = leftover
    processor._release_context("A-1", success=False)
    assert "A-1" not in processor._contexts

    with patch.object(processor, "_init_git_manager", side_effect=RuntimeError("no git")):
        runner_b = processor._ensure_agent_runner("B-2")
    assert runner_b is not leftover
    assert runner_b.working_directory != leftover.working_directory


# ---------------------------------------------------------------------------
# R5 — /start-work failure must release live context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r5_start_work_command_releases_context_on_failure(
    processor, state_manager
):
    state_manager.create_state("CMD-1", "s", "d")
    state_manager.update_state("CMD-1", status=TaskStatus.PLAN_READY)

    async def boom(state):
        processor._contexts[state.issue_key] = {
            "git": None,
            "runner": MagicMock(),
        }
        raise RuntimeError("workflow crashed after claim")

    with patch.object(processor, "_start_execution_workflow", side_effect=boom):
        await processor._handle_bot_command("CMD-1", "/start-work")

    assert not processor._is_live_processing("CMD-1")
    st = state_manager.get_state("CMD-1")
    assert st is not None
    assert st.status == TaskStatus.ERROR


# ---------------------------------------------------------------------------
# R6 — SimulatedJiraClient must accept real client call shapes
# ---------------------------------------------------------------------------


def test_r6_real_create_issue_accepts_scheduler_kwargs():
    from src.jira.client import JiraClient

    inspect.signature(JiraClient.create_issue).bind(
        None,
        project="P",
        summary="t",
        description="d",
        issue_type="Task",
        labels=["scheduled"],
    )


def test_r6_simulated_create_issue_accepts_scheduler_kwargs():
    from src.jira.simulated_client import SimulatedJiraClient

    inspect.signature(SimulatedJiraClient.create_issue).bind(
        None,
        project="P",
        summary="t",
        description="d",
        issue_type="Task",
        labels=["scheduled"],
    )


def test_r6_simulated_client_exposes_poller_methods():
    from src.jira.simulated_client import SimulatedJiraClient

    required = (
        "get_active_sprint",
        "get_board_issues",
        "get_sprint_issues",
        "transition_to_in_progress",
        "add_labels",
        "append_to_description",
        "update_issue",
    )
    missing = [n for n in required if not callable(getattr(SimulatedJiraClient, n, None))]
    assert missing == [], f"SimulatedJiraClient missing {missing}"


# ---------------------------------------------------------------------------
# R7 — progress parser must not treat prose spaces as a progress bar
# ---------------------------------------------------------------------------


def test_r7_parse_progress_clamps_over_100():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner()
    assert runner._parse_progress("Progress: 150%") == 100
    assert runner._parse_progress("Progress: 75%") == 75


def test_r7_parse_progress_ignores_prose_with_one_block_and_spaces():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner()
    line = "Reading █ file with many spaces in this sentence     "
    assert runner._parse_progress(line) is None


# ---------------------------------------------------------------------------
# R8 — cancel dispatching schedule must abort in-flight process_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r8_cancel_dispatching_schedule_aborts_processor_job(tmp_path):
    import asyncio

    from src.scheduler.service import (
        cancel_scheduled_job,
        dispatch_due_schedules,
        wait_inflight_dispatches,
    )
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="d",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        issue_key="CX-1",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled_called = {"n": 0}

    class Proc:
        async def process_event(self, event):
            started.set()
            await release.wait()
            return {"ok": True, "work_started": True, "skipped": None}

        async def cancel_job(self, issue_key, *, reason=""):
            cancelled_called["n"] += 1
            release.set()
            return {"ok": True, "issue_key": issue_key}

    proc = Proc()
    try:
        jira = MagicMock()
        jira.get_issue.return_value = None
        n = await dispatch_due_schedules(
            processor=proc,  # type: ignore[arg-type]
            store=store,
            jira_client=jira,
        )
        assert n["launched"] >= 1
        await asyncio.wait_for(started.wait(), timeout=2)
        out = cancel_scheduled_job(sid, store=store, processor=proc)
        assert out["ok"] is True
        # Yield once so a correct cancel_job implementation can run
        await asyncio.sleep(0.05)
        assert cancelled_called["n"] >= 1, (
            "cancel_scheduled_job must abort the in-flight processor job"
        )
        final = store.get(sid)
        assert final is not None
        assert final["status"] == "cancelled"
    finally:
        release.set()
        await wait_inflight_dispatches()


# ---------------------------------------------------------------------------
# R9 — add_attachment must not send JSON Content-Type
# ---------------------------------------------------------------------------


def test_r9_add_attachment_does_not_send_json_content_type(tmp_path):
    from src.jira.client import JiraClient

    f = tmp_path / "note.txt"
    f.write_text("hello")
    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            c = JiraClient()
            c.client = http
            http.post.return_value = _http_resp(200, [{"id": "1"}])
            c.add_attachment("P-1", str(f))
            kwargs = http.post.call_args.kwargs
            headers = kwargs.get("headers") or {}
            ct = ""
            for k, v in headers.items():
                if str(k).lower() == "content-type":
                    ct = str(v or "")
            assert kwargs.get("files") is not None
            # Must override the client's default application/json (multipart).
            assert any(str(k).lower() == "content-type" for k in headers), (
                "add_attachment must set Content-Type (or explicitly clear JSON)"
            )
            assert "json" not in ct.lower()


# ---------------------------------------------------------------------------
# R10 — oracle keyword heuristic: "approach" without Mode
# ---------------------------------------------------------------------------


def test_r10_improve_login_approach_is_not_oracle_without_mode():
    from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType

    wt = WorkflowRouter.route_issue("X-1", "Improve login approach", "")
    assert wt != WorkflowType.ORACLE_CONSULT


# ---------------------------------------------------------------------------
# R11 — watchdog must not treat healthy clone as stuck agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r11_watchdog_does_not_abort_live_clone_within_git_budget(
    processor, state_manager
):
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon.processor = processor
    daemon.state_manager = state_manager
    daemon._running = True

    state_manager.create_state("CLN-1", "s", "d")
    started = datetime.now() - timedelta(seconds=100)
    state_manager.update_state(
        "CLN-1",
        status=TaskStatus.EXECUTING,
        started_at=started,
        timeout_seconds=60,
        max_retries=0,
    )
    processor._contexts["CLN-1"] = {"git": MagicMock(), "runner": MagicMock()}

    async def stop_after_one(_seconds):
        daemon._running = False

    with patch.object(daemon, "_abort_stuck_issue") as abort:
        with patch("asyncio.sleep", side_effect=stop_after_one):
            await daemon._monitor_active_issues()
        assert abort.call_count == 0, (
            "Healthy in-clone job must not be watchdog-failed "
            "before git_clone_timeout"
        )
    st = state_manager.get_state("CLN-1")
    assert st is not None
    assert st.status == TaskStatus.EXECUTING


# ---------------------------------------------------------------------------
# R12 — cancel dispatching is allowed by API (UI currently hides it)
# ---------------------------------------------------------------------------


def test_r12_cancel_future_schedule_does_not_cancel_issue_job(tmp_path):
    from src.scheduler.service import cancel_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="d",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at=(datetime.now() + timedelta(hours=2)).isoformat(
            timespec="seconds"
        ),
        issue_key="LIVE-1",
        issue_description="x",
    )
    called = {"n": 0}

    class Proc:
        async def cancel_job(self, issue_key, *, reason=""):
            called["n"] += 1
            return {"ok": True}

    out = cancel_scheduled_job(rec["schedule_id"], store=store, processor=Proc())
    assert out["ok"] is True
    assert called["n"] == 0


def test_r12_cancel_dispatching_schedule_is_allowed_by_store(tmp_path):
    from src.scheduler.service import cancel_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="d",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
        issue_key="CX-2",
        issue_description="x",
    )
    store.claim_due(rec["schedule_id"])
    out = cancel_scheduled_job(rec["schedule_id"], store=store)
    assert out["ok"] is True
    assert store.get(rec["schedule_id"])["status"] == "cancelled"
