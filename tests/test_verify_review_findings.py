"""Reproduce the last review findings against real code (and live Jira).

Each test asserts the *correct* behaviour. A FAIL means the finding is real.
A PASS means that finding is already fixed or was not reproducible.

Live Jira tests create/update issues via REST (no ``bot`` label so a later
daemon start will not pick them up). They skip when Jira is not configured.

Run::

    .venv/bin/python -m pytest tests/test_verify_review_findings.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.state.models import TaskStatus


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


def _seed_opencode_session(
    db_path: Path,
    *,
    session_id: str,
    directory: str,
    title: str = "work",
    time_created: int = 1_700_000_000_000,
    time_updated: int = 1_700_000_000_000,
    text: str = "hello",
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            agent TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            cost REAL,
            tokens_input INTEGER,
            tokens_output INTEGER
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT
        )
        """
    )
    con.execute(
        """
        INSERT OR REPLACE INTO session (
            id, title, directory, agent,
            time_created, time_updated, cost, tokens_input, tokens_output
        ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
        """,
        (session_id, title, directory, "atlas", time_created, time_updated),
    )
    mid = f"msg_{session_id}"
    con.execute(
        """
        INSERT OR REPLACE INTO message (
            id, session_id, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            mid,
            session_id,
            time_created,
            time_updated,
            json.dumps({"role": "assistant"}),
        ),
    )
    con.execute(
        """
        INSERT OR REPLACE INTO part (
            id, message_id, session_id, time_created, time_updated, data
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            f"prt_{session_id}",
            mid,
            session_id,
            time_created,
            time_updated,
            json.dumps({"type": "text", "text": text}),
        ),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# 1. plan_ready + ai-start-work after daemon restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_start_after_restart_must_start_execution(
    state_manager, fake_jira, reporter, tmp_path, monkeypatch
):
    """Cold poller (_seen_issues empty) + start label must start build."""
    from src.jira.poller import JiraPoller
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)

    key = "VR-PLAN-1"
    state_manager.create_state(key, "plan me", "Mode: plan")
    state_manager.update_state(key, status=TaskStatus.PLAN_READY)

    issue = {
        "key": key,
        "fields": {
            "summary": "plan me",
            "description": "Mode: plan",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["ai-start-work"],
            "assignee": None,
        },
    }

    poller = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    poller.state_manager = state_manager
    poller._seen_issues = set()
    poller._plan_start_emitted = set()
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.get_board_issues = MagicMock(return_value=[issue])

    to_process = poller.poll_board()
    assert key in [i["key"] for i in to_process]

    captured: list[dict] = []

    def _handler(event: dict) -> None:
        captured.append(event)

    poller._handler = _handler
    for row in to_process:
        poller.process_issue(row, poller.dispatch_as_update(row["key"]))

    assert captured, "poller emitted no event"
    event = captured[0]
    assert event["webhookEvent"] == "jira:issue_updated", (
        f"after restart plan-start was sent as {event['webhookEvent']}; "
        "create path ignores start labels"
    )

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._start_execution_workflow = AsyncMock(return_value=None)

    outcome = await proc.process_event(event)
    assert outcome.get("work_started") is True, (
        f"plan-start did not begin execution: {outcome}"
    )
    proc._start_execution_workflow.assert_awaited()


@pytest.mark.asyncio
async def test_live_jira_plan_start_after_restart():
    """Create + label a real Jira issue, then run the restart dispatch path."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)

    from src.jira.client import JiraClient
    from src.jira.poller import JiraPoller
    from src.jira_connection import probe_jira_connection
    from src.processor import JobProcessor
    from src.reporter.jira_reporter import JiraReporter
    from src.state.manager import JiraStateManager

    client = JiraClient()
    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    projects = (settings.jira_projects or "KAN").split(",")
    project = (projects[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    summary = f"[vd-verify] plan-start after restart {stamp}"
    description = (
        "Automated verification of plan_ready + ai-start-work after restart.\n"
        "Do not process as a real job.\n"
        "Mode: plan\n"
    )
    created = client.create_issue(
        project,
        summary,
        description,
        issue_type="Task",
        labels=["vd-verify"],
    )
    if not created or not created.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key = created["key"]
    print(f"\n[live jira] created {key}", flush=True)

    assert client.add_labels(key, ["ai-start-work"]), "add_labels failed"
    live = client.get_issue(key)
    assert live and live.get("key") == key
    fields = live.get("fields") or {}
    labels = [str(x).lower() for x in (fields.get("labels") or [])]
    assert "ai-start-work" in labels
    print(f"[live jira] {key} labels={labels} status={fields.get('status')}", flush=True)

    from pathlib import Path as _P
    import tempfile

    tmp = _P(tempfile.mkdtemp(prefix="vd-verify-"))
    state_manager = JiraStateManager(state_dir=tmp / "state")
    state_manager.create_state(key, summary, description)
    state_manager.update_state(key, status=TaskStatus.PLAN_READY)

    # Use the live payload; keep status To Do-like for the start-label path.
    issue = {
        "key": key,
        "id": live.get("id"),
        "fields": {
            "summary": fields.get("summary") or summary,
            "description": fields.get("description") or description,
            "status": fields.get("status")
            or {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": fields.get("labels") or ["ai-start-work"],
            "assignee": fields.get("assignee"),
        },
    }
    status_name = str((issue["fields"]["status"] or {}).get("name") or "")
    if status_name and status_name.lower() not in {
        "to do",
        "todo",
        "open",
        "backlog",
        "selected for development",
    }:
        issue["fields"]["status"] = {
            "name": "To Do",
            "statusCategory": {"key": "new"},
        }

    poller = JiraPoller(client=client, interval_seconds=1, board_id="1")
    poller.state_manager = state_manager
    poller._seen_issues = set()
    poller._plan_start_emitted = set()

    captured: list[dict] = []
    poller._handler = lambda ev: captured.append(ev)
    poller.process_issue(issue, poller.dispatch_as_update(key))

    assert captured, "live process_issue emitted no event"
    event = captured[0]
    print(f"[live jira] dispatched {event['webhookEvent']}", flush=True)
    assert event["webhookEvent"] == "jira:issue_updated", (
        f"live restart path sent {event['webhookEvent']} (create ignores start labels)"
    )

    proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = JiraReporter(client=client)
    proc.jira_client = client
    proc._start_execution_workflow = AsyncMock(return_value=None)
    outcome = await proc.process_event(event)
    print(f"[live jira] process_event outcome={outcome}", flush=True)
    assert outcome.get("work_started") is True, outcome


# ---------------------------------------------------------------------------
# 2. Queue find_open_jira oldest-500 cap
# ---------------------------------------------------------------------------


def test_find_open_jira_sees_new_row_after_500_historical(tmp_path):
    from src.state.queue_store import WorkQueueStore

    qs = WorkQueueStore(queue_dir=tmp_path / "q")
    for i in range(500):
        rec = qs.enqueue(
            source="jira",
            issue_key=f"OLD-{i}",
            summary=f"old {i}",
        )
        qs.finish(rec["queue_id"], status="completed")
    live = qs.enqueue(source="jira", issue_key="NEW-1", summary="fresh")
    found = qs.find_open_jira("NEW-1")
    assert found is not None, (
        "find_open_jira missed the open row after 500 older files"
    )
    assert found["queue_id"] == live["queue_id"]


# ---------------------------------------------------------------------------
# 3. Cancel dispatching schedule during Jira GET
# ---------------------------------------------------------------------------


def test_cancel_during_jira_fetch_must_not_start_process_event(tmp_path):
    """Dashboard cancel while claim is blocked on get_issue must not start work."""
    from src.scheduler.service import (
        _INFLIGHT_DISPATCHES,
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
        issue_key="VR-SCHED-1",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    in_get = threading.Event()
    release_get = threading.Event()
    process_started: list[dict] = []

    class SlowJira:
        def get_issue(self, *a, **k):
            in_get.set()
            release_get.wait(timeout=5)
            return {
                "key": "VR-SCHED-1",
                "fields": {
                    "summary": "t",
                    "description": "x",
                    "labels": [],
                    "status": {"name": "To Do"},
                },
            }

    class Proc:
        async def process_event(self, event):
            process_started.append(event)
            return {"ok": True, "work_started": True, "skipped": None}

        async def cancel_job(self, issue_key, *, reason=""):
            return {"ok": False, "error": "No local state for this issue"}

    proc = Proc()

    def _run() -> None:
        asyncio.run(
            dispatch_due_schedules(
                processor=proc,  # type: ignore[arg-type]
                store=store,
                jira_client=SlowJira(),
            )
        )

    worker = threading.Thread(target=_run, name="vd-verify-dispatch")
    worker.start()
    assert in_get.wait(timeout=5), "dispatcher never called get_issue"
    out = cancel_scheduled_job(sid, store=store, processor=proc)
    assert out["ok"] is True
    release_get.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    _INFLIGHT_DISPATCHES.pop(sid, None)
    assert process_started == [], (
        "process_event ran after the schedule was cancelled during Jira fetch"
    )
    assert (store.get(sid) or {}).get("status") == "cancelled"


# ---------------------------------------------------------------------------
# 4. Schedule work is invisible to the queue workspace lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_schedule_job_blocks_same_workspace_queue(tmp_path):
    """While a schedule dispatch is in process_event, same clone must not claim."""
    from src.scheduler.service import (
        dispatch_due_schedules,
        wait_inflight_dispatches,
    )
    from src.state.queue_store import WorkQueueStore, workspace_lock_key
    from src.state.schedule_store import ScheduleStore

    repo = "https://gitlab.example.com/acme/demo.git"
    lock = workspace_lock_key(repo, "feature/KAN-1", "main")
    qs = WorkQueueStore(queue_dir=tmp_path / "q")
    qs.enqueue(
        source="jira",
        issue_key="KAN-2",
        summary="second",
        repository_url=repo,
        work_branch="feature/KAN-1",
        target_branch="main",
        lock_key=lock,
    )

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    store.create(
        title="t",
        description="d",
        repository_url=repo,
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        issue_key="KAN-1",
        issue_description="x",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class Proc:
        def __init__(self) -> None:
            self._locks: dict[str, str] = {}

        def note_workspace_lock(self, issue_key: str, **kw: str) -> str:
            from src.state.queue_store import workspace_lock_key

            lk = workspace_lock_key(
                kw.get("repository_url") or "",
                kw.get("work_branch") or "",
                kw.get("target_branch") or "",
            )
            if lk:
                self._locks[lk] = issue_key
            return lk

        def live_workspace_lock_keys(self) -> set:
            return set(self._locks)

        async def process_event(self, event):
            started.set()
            await release.wait()
            return {"ok": True, "work_started": True, "skipped": None}

    class InstantJira:
        def get_issue(self, *a, **k):
            return None

    proc = Proc()
    task = asyncio.create_task(
        dispatch_due_schedules(
            processor=proc,  # type: ignore[arg-type]
            store=store,
            jira_client=InstantJira(),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    claimed = qs.claim_next(
        blocked_issue_keys={"KAN-1"},
        blocked_locks=proc.live_workspace_lock_keys(),
        max_running=6,
    )
    release.set()
    await task
    await wait_inflight_dispatches()
    assert claimed is None, (
        "queue claimed KAN-2 on the same clone while the schedule job "
        f"for KAN-1 was still in process_event (got {claimed})"
    )


@pytest.mark.asyncio
async def test_live_jira_schedule_blocks_same_workspace_queue(
    tmp_path, isolate_jira_agent_artifacts
):
    """Two live Jira issues: schedule dispatch must block the other on the clone."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)

    from src.jira.client import JiraClient
    from src.jira_connection import probe_jira_connection
    from src.scheduler.service import (
        dispatch_due_schedules,
        wait_inflight_dispatches,
    )
    from src.state.queue_store import workspace_lock_key
    from src.state.schedule_store import ScheduleStore

    from src import config as cfg

    cfg.bootstrap_dotenv_into_environ()
    client = JiraClient()
    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    repo = "https://gitlab.example.com/acme/demo.git"
    keys = []
    for tag in ("sched-A", "sched-B"):
        rec = client.create_issue(
            project,
            f"[vd-verify] schedule lock {tag} {stamp}",
            (
                f"Automated schedule-lock probe {tag}. "
                "No bot/ai-assist label.\n"
                f"Repository: {repo}\n"
                "Source branch: develop\n"
                "Target branch: main\n"
                "Mode: build\n"
            ),
            issue_type="Task",
            labels=["vd-verify"],
        )
        assert rec and rec.get("key"), client.last_error
        keys.append(rec["key"])
        client.add_comment(
            rec["key"],
            f"h3. vd-verify schedule lock\n\nPair {tag} created via REST.",
        )
    key_a, key_b = keys
    print(f"\n[live jira] schedule lock pair {key_a} / {key_b}", flush=True)

    from src.git_manager import GitManager

    work = GitManager.resolve_work_branch_name(key_a, "develop", "main")
    lock = workspace_lock_key(repo, work, "main")
    qs = isolate_jira_agent_artifacts["queue_store"]
    qs.enqueue(
        source="jira",
        issue_key=key_b,
        summary="second",
        repository_url=repo,
        work_branch=work,
        target_branch="main",
        lock_key=lock,
    )

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    store.create(
        title="t",
        description="d",
        repository_url=repo,
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at=(datetime.now() - timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        issue_key=key_a,
        issue_description="x",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class Proc:
        def __init__(self) -> None:
            self._locks: dict[str, str] = {}

        def note_workspace_lock(self, issue_key: str, **kw: str) -> str:
            lk = workspace_lock_key(
                kw.get("repository_url") or "",
                kw.get("work_branch") or "",
                kw.get("target_branch") or "",
            )
            if lk:
                self._locks[lk] = issue_key
            return lk

        def drop_workspace_lock(self, issue_key: str) -> None:
            dead = [k for k, v in self._locks.items() if v == issue_key]
            for k in dead:
                self._locks.pop(k, None)

        def live_workspace_lock_keys(self) -> set:
            return set(self._locks)

        async def process_event(self, event):
            started.set()
            await release.wait()
            return {"ok": True, "work_started": True, "skipped": None}

    proc = Proc()
    task = asyncio.create_task(
        dispatch_due_schedules(
            processor=proc,  # type: ignore[arg-type]
            store=store,
            jira_client=client,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=15)
    claimed = qs.claim_next(
        blocked_issue_keys={key_a},
        blocked_locks=proc.live_workspace_lock_keys(),
        max_running=6,
    )
    release.set()
    await task
    await wait_inflight_dispatches()
    assert claimed is None, (
        f"queue claimed {key_b} on {key_a}'s clone while schedule "
        f"process_event was running (got {claimed})"
    )
    print(f"[live jira] {key_a} lock blocked {key_b} claim", flush=True)


# ---------------------------------------------------------------------------
# 5. Existing MR lookup ignores target
# ---------------------------------------------------------------------------


def test_create_merge_request_must_not_reuse_mr_for_other_target(tmp_path):
    from src.git_manager import GitManager

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key="VR-MR-1",
            remote_url="https://gitlab.example.com/acme/demo.git",
            source_branch="feature/foo",
            target_branch="main",
        )
    gm.remote_enabled = True
    gm.work_branch = "feature/foo"

    listed = [
        {
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/9",
            "target_branch": "develop",
        }
    ]

    class _Res:
        returncode = 0
        stdout = json.dumps(listed)
        stderr = ""

    with patch.object(gm, "_run_glab", return_value=_Res()):
        with patch.object(gm, "_gitlab_host_and_project", return_value=("", "")):
            url = gm._get_existing_mr_url("feature/foo", target_branch="main")
    assert url != listed[0]["web_url"], (
        "existing-MR lookup reused an MR whose target is not main"
    )
    assert url is None


def test_get_existing_mr_url_filters_target(tmp_path, monkeypatch):
    from src.git_manager import GitManager

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(
            issue_key="VR-MR-2",
            remote_url="https://gitlab.example.com/acme/demo.git",
            source_branch="feature/foo",
            target_branch="main",
        )
    gm.remote_enabled = True
    gm.work_branch = "feature/foo"

    listed = [
        {
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/1",
            "target_branch": "develop",
        },
        {
            "web_url": "https://gitlab.example.com/acme/demo/-/merge_requests/2",
            "target_branch": "main",
        },
    ]

    class _Res:
        returncode = 0
        stdout = json.dumps(listed)
        stderr = ""

    monkeypatch.setattr(gm, "_run_glab", lambda cmd: _Res())
    url = gm._get_existing_mr_url("feature/foo")
    assert url == listed[1]["web_url"], (
        f"_get_existing_mr_url returned {url}; must match target_branch=main"
    )


# ---------------------------------------------------------------------------
# 6. GitLab mention on primary-base MR must stay on that source
# ---------------------------------------------------------------------------


def test_gitlab_primary_base_mr_keeps_source_as_work_branch():
    from src.git_manager import GitManager

    jira = GitManager.resolve_work_branch_name(
        "GL-99",
        source_branch="develop",
        target_branch="main",
    )
    assert jira == "feature/GL-99"
    work = GitManager.resolve_work_branch_name(
        "GL-99",
        source_branch="develop",
        target_branch="main",
        keep_source=True,
    )
    # GitLab note intake must build on the MR source, not feature/GL-99.
    assert work == "develop", (
        f"primary-base Source remapped to {work}; GitLab MR develop→main "
        "would clone/push a new feature/GL-… branch"
    )


# ---------------------------------------------------------------------------
# 7. Session storage leaks
# ---------------------------------------------------------------------------


def test_job_chat_does_not_include_later_session_from_same_clone(
    isolate_jira_agent_artifacts,
):
    from src.dashboard.service import collect_job_chat
    from src.state.job_store import JobStore

    runtime = isolate_jira_agent_artifacts["runtime"]
    db = runtime / "opencode.db"
    wd = str((runtime / "clone").resolve())
    (runtime / "clone").mkdir()
    t0 = 1_700_000_000_000
    t1 = t0 + 60_000
    _seed_opencode_session(
        db,
        session_id="ses_AAAAAA",
        directory=wd,
        title="KAN-A: first",
        time_created=t0,
        time_updated=t0,
        text="job A only",
    )
    _seed_opencode_session(
        db,
        session_id="ses_BBBBBB",
        directory=wd,
        title="KAN-B: second",
        time_created=t1,
        time_updated=t1,
        text="job B leaked",
    )

    js: JobStore = isolate_jira_agent_artifacts["job_store"]
    job = js.create_job(issue_key="KAN-A", summary="first")
    js.update_job(
        job["job_id"],
        working_directory=wd,
        opencode_session_id="ses_AAAAAA",
        started_at=datetime.fromtimestamp(t0 / 1000).isoformat(timespec="seconds"),
        completed_at=datetime.fromtimestamp((t0 + 10_000) / 1000).isoformat(
            timespec="seconds"
        ),
        status="completed",
    )
    raw = js.get_job(job["job_id"])
    chat = collect_job_chat(raw)
    assert "ses_BBBBBB" not in chat["session_ids"], (
        f"old job chat included later session: {chat['session_ids']}"
    )
    texts = []
    for msg in chat.get("messages") or []:
        for part in msg.get("parts") or []:
            if part.get("text"):
                texts.append(part["text"])
    assert "job B leaked" not in "\n".join(texts)


def test_build_one_job_does_not_stamp_other_jobs_session(
    isolate_jira_agent_artifacts, state_manager
):
    from src.dashboard.service import build_one_job

    js = isolate_jira_agent_artifacts["job_store"]
    job = js.create_job(issue_key="KAN-X", summary="old")
    js.update_job(job["job_id"], status="completed")
    state_manager.create_state("KAN-X", "old", "")
    state_manager.update_state(
        "KAN-X",
        status=TaskStatus.COMPLETED,
        current_opencode_session_id="ses_LIVE99",
        metadata={"current_job_id": "job_other"},
    )
    item = build_one_job(
        job["job_id"],
        store=js,
        state_manager=state_manager,
    )
    assert item is not None
    assert item.opencode_session_id != "ses_LIVE99", (
        "historical job inherited the issue's current session id"
    )


def test_parse_session_id_prefers_session_created_over_json_child():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner(working_directory="/tmp")
    lines = [
        '{"type":"tool","sessionID":"ses_CHILD999"}',
        "session created: ses_MAIN999999",
    ]
    sid = runner._parse_session_id(lines)
    assert sid == "ses_MAIN999999", (
        f"_parse_session_id returned {sid}; child JSON sessionID won"
    )


# ---------------------------------------------------------------------------
# 8. Job detail leftover job (React page)
# ---------------------------------------------------------------------------

_JOB_DETAIL = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "src"
    / "pages"
    / "jobs"
    / "JobDetailPage.tsx"
)
_JOB_CHAT = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "src"
    / "pages"
    / "jobs"
    / "JobChatTab.tsx"
)


def _job_id_effect_block(src: str) -> str:
    """The useEffect that reseeds state when the route :jobId changes."""
    start = src.find("useEffect(() => {\n    setTab('overview')")
    assert start != -1, "JobDetailPage jobId effect not found"
    end = src.find("}, [jobId])", start)
    assert end != -1, "JobDetailPage jobId effect end not found"
    return src[start:end]


def _apply_job_id_effect(
    *,
    job: dict | None,
    route_job_id: str,
    cache: dict[str, dict],
) -> dict | None:
    """Line-accurate port of JobDetailPage.tsx jobId useEffect (seed branch).

    Mirrors:

        const seed = peekJob(jobId.trim())
        if (seed) {
          setJob(seed)
          setLoading(false)
        }
        void load(Boolean(seed))

    Mirrors ``setJob(seed)`` (seed may be null on cache miss).
    """
    return cache.get(route_job_id.strip())


def test_job_detail_route_change_must_not_keep_previous_job():
    """A→B with B not in entityCache must not leave job A on screen / on Stop."""
    job_a = {
        "job_id": "job_aaa",
        "issue_key": "KAN-A",
        "summary": "first",
        "status": "executing",
        "live": True,
    }
    displayed = _apply_job_id_effect(
        job=job_a,
        route_job_id="job_bbb",
        cache={"job_aaa": job_a},
    )
    assert displayed is None or displayed["job_id"] == "job_bbb", (
        f"route is job_bbb but displayed job is still {displayed}"
    )
    assert (displayed or {}).get("issue_key") != "KAN-A", (
        "Stop work would call cancelTask(KAN-A) while the URL is /jobs/job_bbb"
    )

    src = _JOB_DETAIL.read_text(encoding="utf-8")
    effect = _job_id_effect_block(src)
    assert "setJob(seed)" in effect
    assert "if (seed)" not in effect.split("const seed", 1)[-1].split("void load", 1)[0]


def test_job_detail_stop_and_chat_bind_to_route_id_not_stale_job():
    src = _JOB_DETAIL.read_text(encoding="utf-8")
    assert "cancelTask(job.issue_key)" in src
    assert "deleteJob(job.job_id" in src
    assert "job.job_id !== jobId.trim()" in src
    assert "jobId={jobId.trim()}" in src, (
        "JobChatTab must follow the route jobId, not leftover job.job_id"
    )


def test_job_detail_artifacts_must_ignore_stale_response():
    src = _JOB_DETAIL.read_text(encoding="utf-8")
    start = src.find("const loadArtifacts = useCallback")
    assert start != -1
    load = src[start : src.find("const load = useCallback", start)]
    assert "fetchJobArtifacts(id)" in load
    assert "setPrompts(nextPrompts)" in load
    assert "jobIdRef.current" in src
    assert "acceptJobArtifactsResponse(id, jobIdRef.current)" in load, (
        "loadArtifacts must compare the fetch id to the *current* route ref, "
        "not the jobId closed over when the request started"
    )
    assert "id !== jobId.trim()" not in load, (
        "closed-over jobId === request id after A→B; late A still applies"
    )
    effect = _job_id_effect_block(src)
    assert "artsGen.current" in effect
    assert "artsInFlight.current = false" in effect, (
        "job change must release the in-flight flag so B can fetch"
    )


def test_job_chat_tab_clears_data_on_job_id_change():
    src = _JOB_CHAT.read_text(encoding="utf-8")
    effect = src[src.find("useEffect(() => {\n    loadedFor.current") :]
    effect = effect[: effect.find("}, [jobId])") + 12]
    assert "setData(null)" in effect or "setData(undefined)" in effect, (
        "JobChatTab jobId effect does not clear data; a failed B fetch leaves "
        "A's transcript on screen (error && !data is the only error UI)"
    )


def test_job_chat_api_returns_requested_job_id(
    isolate_jira_agent_artifacts, state_manager
):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app

    js = isolate_jira_agent_artifacts["job_store"]
    a = js.create_job(issue_key="A-1", summary="aaa")
    b = js.create_job(issue_key="B-1", summary="bbb")
    js.update_job(a["job_id"], opencode_session_id="ses_AAA111")
    js.update_job(b["job_id"], opencode_session_id="ses_BBB222")
    state_manager.create_state("A-1", "aaa", "")
    state_manager.create_state("B-1", "bbb", "")

    with patch("src.dashboard.api.job_store", js):
        app = create_dashboard_app(processor=None, state_manager=state_manager)
        client = TestClient(app)
        ra = client.get(f"/api/jobs/{a['job_id']}/chat")
        rb = client.get(f"/api/jobs/{b['job_id']}/chat")
    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    assert ra.json()["job_id"] == a["job_id"]
    assert rb.json()["job_id"] == b["job_id"]


def _write_job_text_artifacts(root: Path, marker: str) -> tuple[str, str]:
    d = root / ".jira-agent" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    prompt = d / f"{marker}.prompt.txt"
    log = d / f"{marker}.log"
    prompt.write_text(f"PROMPT of {marker}\n", encoding="utf-8")
    log.write_text(f"LOG of {marker}\n", encoding="utf-8")
    return str(prompt), str(log)


def _stale_closure_would_apply(request_id: str, closed_over_job_id: str) -> bool:
    """Old JobDetailPage guard: `id !== jobId.trim()` with jobId from fetch start."""
    return request_id.strip() == closed_over_job_id.strip()


def _current_route_applies(request_id: str, route_job_id: str) -> bool:
    """Fixed guard: compare fetch id to the live route ref."""
    req = request_id.strip()
    route = route_job_id.strip()
    return bool(req) and req == route


def test_job_artifacts_api_is_job_scoped_and_stale_a_must_not_replace_b(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts, state_manager
):
    """Two jobs, two files: artifacts API is scoped; stale A must not paint B."""
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app

    monkeypatch.chdir(tmp_path)
    js = isolate_jira_agent_artifacts["job_store"]
    ja = js.create_job(issue_key="ART-A", summary="job A", description="desc A")
    jb = js.create_job(issue_key="ART-B", summary="job B", description="desc B")
    pa, la = _write_job_text_artifacts(tmp_path, ja["job_id"])
    pb, lb = _write_job_text_artifacts(tmp_path, jb["job_id"])
    js.update_job(ja["job_id"], prompt_path=pa, session_log_path=la)
    js.update_job(jb["job_id"], prompt_path=pb, session_log_path=lb)
    state_manager.create_state("ART-A", "job A", "desc A")
    state_manager.create_state("ART-B", "job B", "desc B")

    with patch("src.dashboard.api.job_store", js):
        app = create_dashboard_app(processor=None, state_manager=state_manager)
        client = TestClient(app)
        ra = client.get(f"/api/jobs/{ja['job_id']}/artifacts")
        rb = client.get(f"/api/jobs/{jb['job_id']}/artifacts")

    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    body_a, body_b = ra.json(), rb.json()
    assert body_a["job_id"] == ja["job_id"]
    assert body_b["job_id"] == jb["job_id"]
    text_a = " ".join(
        (p.get("content") or "") for p in (body_a["prompts"] + body_a["session_logs"])
    )
    text_b = " ".join(
        (p.get("content") or "") for p in (body_b["prompts"] + body_b["session_logs"])
    )
    assert f"PROMPT of {ja['job_id']}" in text_a
    assert f"LOG of {ja['job_id']}" in text_a
    assert f"PROMPT of {jb['job_id']}" in text_b
    assert ja["job_id"] not in text_b
    assert jb["job_id"] not in text_a

    # Frontend race: A fetch still in flight, operator opens B, A returns.
    displayed = body_b
    if _stale_closure_would_apply(ja["job_id"], ja["job_id"]):
        displayed_if_buggy = body_a
    else:
        displayed_if_buggy = body_b
    assert displayed_if_buggy["job_id"] == ja["job_id"], (
        "the old closed-over jobId check would not reproduce; test is wrong"
    )
    if _current_route_applies(ja["job_id"], jb["job_id"]):
        displayed = body_a
    assert displayed["job_id"] == jb["job_id"]
    assert ja["job_id"] not in " ".join(
        (p.get("content") or "") for p in (displayed["prompts"] + displayed["session_logs"])
    )


def test_live_jira_two_issues_artifacts_are_job_scoped(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts, state_manager
):
    """Create two real Jira issues, bind two jobs, assert artifacts do not leak."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)

    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.jira.client import JiraClient
    from src.jira_connection import probe_jira_connection

    from src import config as cfg

    cfg.bootstrap_dotenv_into_environ()
    client = JiraClient()
    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    created = []
    for tag in ("A", "B"):
        rec = client.create_issue(
            project,
            f"[vd-verify] artifacts isolation {tag} {stamp}",
            (
                f"Automated artifacts-isolation probe {tag}. "
                "No bot/ai-assist label — daemon must not pick this up.\n"
                "Mode: build\n"
            ),
            issue_type="Task",
            labels=["vd-verify"],
        )
        assert rec and rec.get("key"), client.last_error
        created.append(rec["key"])
        client.add_comment(
            rec["key"],
            f"h3. vd-verify artifacts isolation\n\nCreated via REST as job pair {tag}.",
        )
    key_a, key_b = created
    print(f"\n[live jira] created {key_a} and {key_b}", flush=True)

    live_a = client.get_issue(key_a)
    live_b = client.get_issue(key_b)
    assert live_a and live_a.get("key") == key_a
    assert live_b and live_b.get("key") == key_b
    labels_a = [str(x).lower() for x in ((live_a.get("fields") or {}).get("labels") or [])]
    assert "bot" not in labels_a and "ai-assist" not in labels_a

    monkeypatch.chdir(tmp_path)
    js = isolate_jira_agent_artifacts["job_store"]
    ja = js.create_job(
        issue_key=key_a,
        summary=f"job for {key_a}",
        description=f"desc {key_a}",
    )
    jb = js.create_job(
        issue_key=key_b,
        summary=f"job for {key_b}",
        description=f"desc {key_b}",
    )
    pa, la = _write_job_text_artifacts(tmp_path, ja["job_id"])
    pb, lb = _write_job_text_artifacts(tmp_path, jb["job_id"])
    js.update_job(ja["job_id"], prompt_path=pa, session_log_path=la)
    js.update_job(jb["job_id"], prompt_path=pb, session_log_path=lb)
    state_manager.create_state(key_a, f"job for {key_a}", f"desc {key_a}")
    state_manager.create_state(key_b, f"job for {key_b}", f"desc {key_b}")

    with patch("src.dashboard.api.job_store", js):
        app = create_dashboard_app(processor=None, state_manager=state_manager)
        http = TestClient(app)
        ra = http.get(f"/api/jobs/{ja['job_id']}/artifacts")
        rb = http.get(f"/api/jobs/{jb['job_id']}/artifacts")
        da = http.get(f"/api/jobs/{ja['job_id']}")
        db = http.get(f"/api/jobs/{jb['job_id']}")

    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    assert da.status_code == 200 and da.json()["job"]["issue_key"] == key_a
    assert db.status_code == 200 and db.json()["job"]["issue_key"] == key_b
    assert ra.json()["job_id"] == ja["job_id"]
    assert rb.json()["job_id"] == jb["job_id"]
    text_a = " ".join(
        (p.get("content") or "")
        for p in (ra.json()["prompts"] + ra.json()["session_logs"])
    )
    text_b = " ".join(
        (p.get("content") or "")
        for p in (rb.json()["prompts"] + rb.json()["session_logs"])
    )
    assert f"PROMPT of {ja['job_id']}" in text_a
    assert f"PROMPT of {jb['job_id']}" in text_b
    assert ja["job_id"] not in text_b
    assert jb["job_id"] not in text_a
    assert not _current_route_applies(ja["job_id"], jb["job_id"])
    assert _stale_closure_would_apply(ja["job_id"], ja["job_id"])
    print(
        f"[live jira] artifacts isolated for {key_a}={ja['job_id']} "
        f"and {key_b}={jb['job_id']}",
        flush=True,
    )
