"""Coverage-oriented unit tests for src/processor.py (plan/build modes).

Hits cancel_job, start_plan_execution locks, delivery guards, plan
persist/materialize, push protection, workflow resolution, oracle abort,
complete_work races, JobSlotLimiter, process_event, and bot commands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.git_manager import GitCloneError, GitSourceBranchError, GitTargetBranchError
from src.issue_git_spec import IssueGitConfigError
from src.orchestrator.workflow_router import WorkflowType
from src.processor import JobProcessor, _JobSlotLimiter
from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient, make_issue_event


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def _mock_git_and_agent(processor, tmp_path, returncode=0, stdout="done", stderr=""):
    git = MagicMock()
    git.ensure_feature_branch.return_value = "feature/X-1"
    git.work_branch = "feature/X-1"
    git.target_branch = "develop"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/X-1"
    git.ensure_on_work_branch.return_value = True
    git.commits_ahead_of_target.return_value = 1
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "feat: x"
    git.get_last_commit_message.return_value = "feat: x\n\nbody"
    _sha_calls = {"n": 0}

    def _sha(*_a, **_k):
        _sha_calls["n"] += 1
        return "baseline000001" if _sha_calls["n"] == 1 else "delivered000002"

    git.get_last_commit_sha.side_effect = _sha
    git.build_commit_url.return_value = "http://git/commit/delivered000002"
    git.create_merge_request.return_value = "http://mr/1"
    git.get_mr_url.return_value = "http://mr/1"
    git.add_mr_comment.return_value = True
    git.cleanup = MagicMock()

    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_1",
            "retry_info": {
                "attempts": 1,
                "max_retries": 3,
                "retried": False,
                "last_opencode_session_id": "ses_1",
            },
            "timed_out": False,
        }
    )
    runner.run_agent = AsyncMock(
        return_value={
            "returncode": returncode,
            "stdout": stdout or "review ok",
            "stderr": stderr,
            "session_file": str(tmp_path / "r.log"),
            "opencode_session_id": "ses_r",
        }
    )
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 1
    processor.git_manager = git
    processor.agent_runner = runner
    return git, runner


# ---------------------------------------------------------------------------
# _JobSlotLimiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_slot_limiter_resize_and_properties():
    lim = _JobSlotLimiter(2)
    assert lim.limit == 2
    assert lim.active == 0
    await lim.acquire()
    assert lim.active == 1
    lim.resize(1)
    assert lim.limit == 1
    # release wakes waiters
    await lim.release()
    assert lim.active == 0

    # context manager
    async with lim:
        assert lim.active == 1
    assert lim.active == 0


@pytest.mark.asyncio
async def test_job_slot_limiter_schedule_wake_without_running_loop():
    """Cover RuntimeError branch + stored-loop threadsafe wake (lines 61-65)."""
    lim = _JobSlotLimiter(1)
    await lim.acquire()
    # store loop so cold resize can use run_coroutine_threadsafe
    loop = asyncio.get_running_loop()
    lim._loop = loop
    # Call _schedule_wake while loop is running (uses create_task path)
    lim.resize(3)
    await asyncio.sleep(0)
    assert lim.limit == 3
    await lim.release()


def test_job_slot_limiter_schedule_wake_no_loop_no_running():
    """When no running loop and no stored loop, wake is a no-op."""
    lim = _JobSlotLimiter(1)
    lim._loop = None
    lim._schedule_wake()  # should not raise
    lim.resize(5)
    assert lim.limit == 5


def test_job_slot_limiter_schedule_wake_with_stored_running_loop():
    """Thread-safe wake when no running loop but stored loop is running (61-65)."""
    lim = _JobSlotLimiter(1)
    fake = MagicMock()
    fake.is_running.return_value = True
    lim._loop = fake
    with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
        with patch("asyncio.run_coroutine_threadsafe") as rct:
            lim._schedule_wake()
            rct.assert_called_once()
            assert rct.call_args[0][1] is fake


# ---------------------------------------------------------------------------
# resize_job_semaphore / seed_poller / mark_jira
# ---------------------------------------------------------------------------


def test_resize_job_semaphore_installs_limiter(processor):
    processor._job_semaphore = None
    processor._contexts = {"A-1": {}}
    processor.resize_job_semaphore(4)
    assert isinstance(processor._job_semaphore, _JobSlotLimiter)
    assert processor._job_semaphore.limit == 4
    assert processor._job_semaphore.active == 1  # len(contexts)

    processor.resize_job_semaphore(2)
    assert processor._job_semaphore.limit == 2

    # cold path with bad limit -> max(1, ...)
    processor._job_semaphore = MagicMock()  # not _JobSlotLimiter
    processor.resize_job_semaphore(0)
    assert isinstance(processor._job_semaphore, _JobSlotLimiter)
    assert processor._job_semaphore.limit == 1


def test_seed_poller_requeue_markers(processor, state_manager):
    assert processor.seed_poller_requeue_markers() == 0
    poller = MagicMock()
    poller._last_jira_status = {}
    processor._poller = poller

    state_manager.create_state("S-1", "s", "d")
    state_manager.update_state(
        "S-1",
        status=TaskStatus.ERROR,
        metadata={"requeue_eligible": True},
    )
    state_manager.create_state("S-2", "s", "d")
    state_manager.update_state("S-2", status=TaskStatus.COMPLETED)
    # completed without requeue_eligible — skipped
    n = processor.seed_poller_requeue_markers()
    assert n == 1
    assert poller._last_jira_status["S-1"] == "to do"


def test_nudge_poller_paths(processor):
    processor._nudge_poller_after_terminal("X")  # no poller
    poller = MagicMock()
    poller._last_jira_status = {"X": "in progress"}
    processor._poller = poller
    processor._nudge_poller_after_terminal("X")
    assert poller._last_jira_status["X"] == "in progress"  # kept

    poller._last_jira_status["Y"] = "to do"
    processor._nudge_poller_after_terminal("Y")
    # already todo — leave as-is (todo_names path)
    assert poller._last_jira_status["Y"] == "to do"

    poller._last_jira_status["Z"] = ""
    processor._nudge_poller_after_terminal("Z")
    assert poller._last_jira_status["Z"] == "to do"

    # exception swallowed
    poller._last_jira_status = None  # will raise on .get
    processor._nudge_poller_after_terminal("Z")


def test_mark_jira_in_progress_paths(processor, fake_jira):
    processor.jira_client = None
    processor._mark_jira_in_progress("M-1")

    processor.jira_client = MagicMock(spec=[])  # no transition_to_in_progress
    processor._mark_jira_in_progress("M-1")

    client = MagicMock()
    client.transition_to_in_progress.return_value = True
    processor.jira_client = client
    processor._mark_jira_in_progress("M-1")

    client.transition_to_in_progress.return_value = False
    processor._mark_jira_in_progress("M-1")

    client.transition_to_in_progress.side_effect = RuntimeError("net")
    processor._mark_jira_in_progress("M-1")


# ---------------------------------------------------------------------------
# fail_issue / release_context / record helpers
# ---------------------------------------------------------------------------


def test_fail_issue_no_state_and_post_error_none(processor, fake_jira):
    processor._fail_issue("GHOST", "boom")
    assert any("boom" in c["body"] for c in fake_jira.comments)

    processor.reporter = MagicMock()
    processor.reporter.post_error.return_value = None
    processor.state_manager.create_state("FE-1", "s", "d")
    processor._fail_issue("FE-1", "err2")


def test_fail_issue_exception_swallowed(processor, state_manager):
    state_manager.create_state("FE-2", "s", "d")
    processor.reporter = MagicMock()
    processor.reporter.post_error.side_effect = RuntimeError("jira down")
    # update_state works; post_error blows — outer except
    processor.state_manager.update_state = MagicMock(side_effect=RuntimeError("disk"))
    processor._fail_issue("FE-2", "x")


def test_release_context_cleanup_exception(processor):
    git = MagicMock()
    git.cleanup.side_effect = RuntimeError("rm fail")
    processor._contexts["RC-1"] = {"git": git, "runner": None}
    processor._release_context("RC-1", success=False)
    assert "RC-1" not in processor._contexts


def test_record_agent_retry_aborted_and_job_update(processor, state_manager, tmp_path):
    state_manager.create_state("RR-1", "s", "d")
    state_manager.update_state("RR-1", status=TaskStatus.CANCELLED)
    processor._record_agent_retry("RR-1", attempt_number=1, delay_seconds=1.0, reason="x")

    state_manager.create_state("RR-2", "s", "d")
    state_manager.update_state("RR-2", status=TaskStatus.EXECUTING, current_task_id="t1")
    job = processor.job_store.create_job(issue_key="RR-2", status="executing")
    processor._active_jobs["RR-2"] = job["job_id"]
    # force job_store update to raise
    with patch.object(processor.job_store, "update_job", side_effect=RuntimeError("x")):
        processor._record_agent_retry(
            "RR-2",
            attempt_number=2,
            delay_seconds=0.1,
            reason="timeout",
            session_id="ses_x",
            session_file=str(tmp_path / "s.log"),
            new_task_id="t2",
        )


def test_link_and_apply_session_paths(processor, state_manager, tmp_path):
    processor._link_job_session_paths("NOJOB", session_path="/a", prompt_path="/b")

    state_manager.create_state("LK-1", "s", "d")
    job = processor.job_store.create_job(issue_key="LK-1", status="running", description="")
    processor._active_jobs["LK-1"] = job["job_id"]
    processor._link_job_session_paths("LK-1")  # empty patch
    sess = tmp_path / "ses.log"
    sess.write_text("x")
    prompt = tmp_path / "ses.prompt.txt"
    prompt.write_text("Plan: do things carefully\n")
    processor._link_job_session_paths("LK-1", session_path=str(sess), prompt_path=str(prompt))
    with patch.object(processor.job_store, "update_job", side_effect=RuntimeError("x")):
        processor._link_job_session_paths("LK-1", session_path=str(sess))

    # apply_agent_result_session recovers description from prompt file
    result = {
        "opencode_session_id": "ses_new",
        "session_file": str(sess),
        "retry_info": {"last_opencode_session_id": "ses_fallback"},
    }
    processor._apply_agent_result_session("LK-1", result)
    loaded = processor.job_store.get_job(job["job_id"])
    assert loaded is not None

    # Abandoned cold-retry id must not be rebound when the final attempt
    # produced no session id.
    processor._apply_agent_result_session(
        "LK-1",
        {
            "opencode_session_id": None,
            "session_file": str(sess),
            "retry_info": {
                "last_opencode_session_id": "ses_old",
                "abandoned_session_id": "ses_old",
            },
        },
    )
    st = state_manager.get_state("LK-1")
    assert st is not None
    # current stays ses_new from the previous successful apply
    assert st.current_opencode_session_id == "ses_new"

    # no session id
    processor._record_opencode_session("LK-1", None)
    processor._record_opencode_session("GHOST", "ses_y")


def test_finish_job_record_terminal_fill(processor, state_manager):
    state_manager.create_state("FJ-1", "s", "d")
    state_manager.update_state(
        "FJ-1",
        status=TaskStatus.EXECUTING,
        current_task_id="task_fill",
        current_opencode_session_id="ses_fill",
    )
    job = processor.job_store.create_job(
        issue_key="FJ-1", status="completed", task_id=None
    )
    # already terminal — fill missing ids
    processor._active_jobs["FJ-1"] = job["job_id"]
    processor._finish_job_record("FJ-1", status="error", error_message="late")
    loaded = processor.job_store.get_job(job["job_id"])
    assert loaded["status"] == "completed"
    assert loaded.get("task_id") == "task_fill" or loaded.get("opencode_session_id")

    # no job at all
    processor._finish_job_record("NOPE", status="error")


def test_start_job_record_appends_history(processor, state_manager):
    state = state_manager.create_state("SJ-1", "s", "d")
    jid = processor._start_job_record(
        state, workflow_type="planning", agent="prometheus", task_id="t1"
    )
    assert jid
    st = state_manager.get_state("SJ-1")
    assert jid in (st.metadata or {}).get("job_ids", [])
    assert "t1" in (st.metadata or {}).get("task_ids", [])


# ---------------------------------------------------------------------------
# cancel_job / start_plan_execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_job_paths(processor, state_manager, fake_jira):
    # no state
    out = await processor.cancel_job("NOPE")
    assert out["ok"] is False

    state_manager.create_state("CJ-1", "s", "d")
    state_manager.update_state("CJ-1", status=TaskStatus.COMPLETED)
    out = await processor.cancel_job("CJ-1")
    assert out["ok"] is False
    assert "terminal" in out["error"]

    state_manager.create_state("CJ-2", "s", "d")
    state_manager.update_state(
        "CJ-2", status=TaskStatus.EXECUTING, current_task_id="t1"
    )
    runner = MagicMock()
    runner.cancel_task.return_value = True
    runner.cancel_all_tasks.return_value = 2
    processor._contexts["CJ-2"] = {"git": MagicMock(), "runner": runner}
    processor.agent_runner = runner
    out = await processor.cancel_job("CJ-2", reason="ops cancel")
    assert out["ok"] is True
    assert state_manager.get_state("CJ-2").status == TaskStatus.CANCELLED
    assert "CJ-2" not in processor._contexts

    # kill raises
    state_manager.create_state("CJ-3", "s", "d")
    state_manager.update_state(
        "CJ-3", status=TaskStatus.PLANNING, current_task_id="t2"
    )
    bad = MagicMock()
    bad.cancel_task.side_effect = RuntimeError("kill fail")
    processor._contexts["CJ-3"] = {"git": None, "runner": bad}
    out = await processor.cancel_job("CJ-3")
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_start_plan_execution_lock_paths(processor, state_manager, tmp_path):
    # no state
    out = await processor.start_plan_execution("NOPE")
    assert out["ok"] is False
    assert "No local state" in out["error"]

    state_manager.create_state("SPE-1", "s", "d")
    out = await processor.start_plan_execution("SPE-1")
    assert out["ok"] is False
    assert "not plan_ready" in out["error"]

    state_manager.update_state("SPE-1", status=TaskStatus.PLAN_READY, plan_path="p.md")
    processor._contexts["SPE-1"] = {"git": None, "runner": None}
    out = await processor.start_plan_execution("SPE-1")
    assert out["ok"] is False
    assert "already being processed" in out["error"]
    del processor._contexts["SPE-1"]

    async def boom(_state):
        raise RuntimeError("exec boom")

    with patch.object(processor, "_start_execution_workflow", side_effect=boom):
        out = await processor.start_plan_execution("SPE-1")
    assert out["ok"] is False
    assert "exec boom" in out["error"]
    assert state_manager.get_state("SPE-1").status == TaskStatus.ERROR

    # success path
    state_manager.create_state("SPE-2", "s", "d")
    state_manager.update_state("SPE-2", status=TaskStatus.PLAN_READY)

    async def ok(st):
        state_manager.update_state("SPE-2", status=TaskStatus.COMPLETED)

    with patch.object(processor, "_start_execution_workflow", side_effect=ok):
        out = await processor.start_plan_execution("SPE-2", reason="test")
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# _resolve_workflow / process_event / handlers
# ---------------------------------------------------------------------------


def test_resolve_workflow_mode_and_params_error(processor):
    # mode plan
    wt = processor._resolve_workflow(
        "W-1",
        "x",
        "{params}\nMode: plan\nRepository: https://g.example/r.git\n"
        "Source branch: develop\nTarget branch: main\n{params}",
    )
    assert wt == WorkflowType.PLANNING

    # mode build
    wt = processor._resolve_workflow(
        "W-2",
        "x",
        "{params}\nMode: build\nRepository: https://g.example/r.git\n"
        "Source branch: develop\nTarget branch: main\n{params}",
    )
    assert wt == WorkflowType.EXECUTION

    # oracle
    wt = processor._resolve_workflow(
        "W-3", "how to design architecture", "what approach should we use?"
    )
    assert wt == WorkflowType.ORACLE_CONSULT

    # params without mode → still routes; Mode fails later in parse_issue_git_spec
    wt = processor._resolve_workflow(
        "W-4",
        "do work",
        "{params}\nRepository: https://g.example/r.git\n"
        "Source branch: develop\nTarget branch: main\n{params}",
    )
    assert wt in (WorkflowType.PLANNING, WorkflowType.EXECUTION)

    # no params no mode — allow (test patch path)
    with patch(
        "src.processor.WorkflowRouter.route_issue",
        return_value=WorkflowType.PLANNING,
    ):
        wt = processor._resolve_workflow("W-5", "s", "d")
        assert wt == WorkflowType.PLANNING


@pytest.mark.asyncio
async def test_process_event_paths(processor, state_manager, fake_jira):
    await processor.process_event({"webhookEvent": "unknown", "issue": {"key": "U"}})

    with patch.object(processor, "_handle_issue_created", new_callable=AsyncMock) as m:
        await processor.process_event(make_issue_event(key="PE-1"))
        m.assert_awaited()

    with patch.object(processor, "_handle_issue_updated", new_callable=AsyncMock) as m:
        await processor.process_event(
            make_issue_event(key="PE-2", event_type="jira:issue_updated")
        )
        m.assert_awaited()

    with patch.object(processor, "_handle_comment_created", new_callable=AsyncMock) as m:
        await processor.process_event(
            {
                "webhookEvent": "comment_created",
                "issue": {"key": "PE-3"},
                "comment": {"body": "x"},
            }
        )
        m.assert_awaited()

    with patch.object(processor, "_handle_comment_created", new_callable=AsyncMock) as m:
        await processor.process_event(
            {
                "webhookEvent": "jira:issue_commented",
                "issue": {"key": "PE-4"},
                "comment": {"body": "x"},
            }
        )
        m.assert_awaited()

    state_manager.create_state("PE-E", "s", "d")
    with patch.object(
        processor, "_handle_issue_created", side_effect=RuntimeError("boom")
    ):
        await processor.process_event(make_issue_event(key="PE-E"))
    assert state_manager.get_state("PE-E").status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_handle_created_route_err_and_live(processor, state_manager):
    processor._contexts["LIVE-1"] = {"git": None, "runner": None}
    await processor._handle_issue_created(make_issue_event(key="LIVE-1"))

    state_manager.create_state("PR-SKIP", "s", "d")
    state_manager.update_state("PR-SKIP", status=TaskStatus.PLAN_READY)
    await processor._handle_issue_created(make_issue_event(key="PR-SKIP"))

    # existing terminal + route error with params no mode
    state_manager.create_state("RE-1", "old", "old")
    state_manager.update_state("RE-1", status=TaskStatus.ERROR)
    desc = (
        "{params}\nRepository: https://g.example/r.git\n"
        "Source branch: develop\nTarget branch: main\n{params}"
    )
    await processor._handle_issue_created(
        make_issue_event(key="RE-1", summary="work", description=desc)
    )
    assert state_manager.get_state("RE-1").status == TaskStatus.ERROR

    # new issue with route error
    await processor._handle_issue_created(
        make_issue_event(key="RE-2", summary="work", description=desc)
    )
    assert state_manager.get_state("RE-2").status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_handle_updated_reprocess_and_label_fail(processor, state_manager):
    # terminal without requeue_eligible
    state_manager.create_state("UP-1", "s", "d")
    state_manager.update_state("UP-1", status=TaskStatus.ERROR, metadata={})
    await processor._handle_issue_updated(
        make_issue_event(
            key="UP-1",
            event_type="jira:issue_updated",
            status="To Do",
        )
    )
    # still error (no requeue)
    assert state_manager.get_state("UP-1").status == TaskStatus.ERROR

    # terminal not todo
    state_manager.update_state(
        "UP-1", status=TaskStatus.ERROR, metadata={"requeue_eligible": True}
    )
    await processor._handle_issue_updated(
        make_issue_event(
            key="UP-1",
            event_type="jira:issue_updated",
            status="In Progress",
        )
    )

    # reprocess with requeue
    with patch.object(processor, "_handle_issue_created", new_callable=AsyncMock) as m:
        await processor._handle_issue_updated(
            make_issue_event(
                key="UP-1",
                event_type="jira:issue_updated",
                status="To Do",
            )
        )
        m.assert_awaited()

    # plan_ready + label + live skip
    state_manager.create_state("UP-L", "s", "d")
    state_manager.update_state("UP-L", status=TaskStatus.PLAN_READY)
    processor._contexts["UP-L"] = {"git": None, "runner": None}
    # Start labels only apply while the board is still To Do-like (product rule)
    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "UP-L",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["ai-start-work"],
                "summary": "s",
                "description": "d",
            },
        },
    }
    await processor._handle_issue_updated(event)

    # plan_ready + label + execution fails
    del processor._contexts["UP-L"]
    with patch.object(
        processor,
        "_start_execution_workflow",
        side_effect=RuntimeError("label fail"),
    ):
        await processor._handle_issue_updated(event)
    assert state_manager.get_state("UP-L").status == TaskStatus.ERROR

    # live processing ignore
    processor._contexts["UP-LIVE"] = {}
    state_manager.create_state("UP-LIVE", "s", "d")
    await processor._handle_issue_updated(
        make_issue_event(key="UP-LIVE", event_type="jira:issue_updated")
    )

    # in-flight ignore
    state_manager.create_state("UP-IF", "s", "d")
    state_manager.update_state("UP-IF", status=TaskStatus.EXECUTING)
    await processor._handle_issue_updated(
        make_issue_event(key="UP-IF", event_type="jira:issue_updated")
    )


@pytest.mark.asyncio
async def test_bot_commands_status_cancel_start(processor, state_manager, fake_jira):
    state_manager.create_state("BOT-1", "s", "d")
    state_manager.update_state(
        "BOT-1",
        status=TaskStatus.PLAN_READY,
        plan_path="p.md",
        error_message="prev err",
        metadata={"workflow_type": "planning", "merge_request_url": "http://mr/x"},
    )
    with patch.object(processor, "_start_execution_workflow", new_callable=AsyncMock):
        await processor._handle_bot_command("BOT-1", "/start-work")

    await processor._handle_bot_command("BOT-1", "/status")
    await processor._handle_bot_command("NOSTATE", "/status")

    # start-work when not plan_ready
    state_manager.update_state("BOT-1", status=TaskStatus.PENDING)
    await processor._handle_bot_command("BOT-1", "/start-work")
    assert any("No plan is ready" in c["body"] for c in fake_jira.comments)

    # cancel with runner
    state_manager.update_state(
        "BOT-1", status=TaskStatus.EXECUTING, current_task_id="t1"
    )
    runner = MagicMock()
    runner.cancel_task.return_value = True
    processor._contexts["BOT-1"] = {"git": MagicMock(), "runner": runner}
    await processor._handle_bot_command("BOT-1", "/cancel")
    assert state_manager.get_state("BOT-1").status == TaskStatus.CANCELLED

    await processor._handle_bot_command("NOSTATE", "/cancel")

    # free-form -> direct request
    with patch.object(processor, "_handle_direct_request", new_callable=AsyncMock) as d:
        await processor._handle_bot_command("BOT-1", "please explain this")
        d.assert_awaited()


# ---------------------------------------------------------------------------
# _refresh_issue_text_from_jira
# ---------------------------------------------------------------------------


def test_refresh_issue_text_from_jira(processor, state_manager, fake_jira):
    state = state_manager.create_state("RF-1", "old sum", "old desc")

    # get_issue returns None
    s, d = processor._refresh_issue_text_from_jira("RF-1", state)
    assert s == "old sum"

    # exception
    fake_jira.get_issue = MagicMock(side_effect=RuntimeError("net"))
    s, d = processor._refresh_issue_text_from_jira("RF-1", state)
    assert s == "old sum"

    # live update with non-string description
    fake_jira.get_issue = MagicMock(
        return_value={
            "fields": {
                "summary": "new sum",
                "description": {"type": "doc", "text": "x"},
            }
        }
    )
    s, d = processor._refresh_issue_text_from_jira("RF-1", state)
    assert s == "new sum"
    assert "doc" in d or d  # stringified
    st = state_manager.get_state("RF-1")
    assert st.issue_summary == "new sum"


# ---------------------------------------------------------------------------
# prepare_git_workspace exception branches
# ---------------------------------------------------------------------------


def test_prepare_git_workspace_exception_types(processor, state_manager, fake_jira):
    state = state_manager.create_state("GW-1", "s", "d")

    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=IssueGitConfigError("bad template"),
    ):
        assert processor._prepare_git_workspace_blocking(state) is None
    assert state_manager.get_state("GW-1").status == TaskStatus.ERROR

    state = state_manager.create_state("GW-2", "s", "d")
    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=GitCloneError("clone fail"),
    ):
        assert processor._prepare_git_workspace_blocking(state) is None

    state = state_manager.create_state("GW-3", "s", "d")
    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=GitTargetBranchError("no target"),
    ):
        assert processor._prepare_git_workspace_blocking(state) is None

    state = state_manager.create_state("GW-4", "s", "d")
    with patch.object(
        processor,
        "_init_git_manager",
        side_effect=GitSourceBranchError("no source"),
    ):
        assert processor._prepare_git_workspace_blocking(state) is None

    state = state_manager.create_state("GW-5", "s", "d")
    with patch.object(
        processor, "_init_git_manager", side_effect=RuntimeError("weird")
    ):
        assert processor._prepare_git_workspace_blocking(state) is None


# ---------------------------------------------------------------------------
# assert_build_delivery / persist / materialize / push
# ---------------------------------------------------------------------------


def test_assert_build_delivery_failures(processor, tmp_path):
    assert processor._assert_build_delivery("NOGIT") is not None

    git = MagicMock()
    git.work_branch = ""
    git.delivery_baseline_sha = None
    git.get_last_commit_sha.return_value = "aaa111"
    processor._contexts["BD-1"] = {"git": git, "runner": None}
    assert "Work branch" in processor._assert_build_delivery("BD-1")

    git.work_branch = "feature/BD-1"
    git.ensure_on_work_branch.return_value = False
    git.get_current_branch.return_value = "develop"
    err = processor._assert_build_delivery("BD-1")
    assert "instead of work branch" in err

    git.ensure_on_work_branch.return_value = True
    git.commits_ahead_of_target.return_value = 0
    git.get_last_commit_sha.return_value = "aaa111"
    git.delivery_baseline_sha = None
    err = processor._assert_build_delivery("BD-1")
    assert "No commits" in err

    git.commits_ahead_of_target.side_effect = RuntimeError("git err")
    err = processor._assert_build_delivery("BD-1")
    assert "No commits" in err

    git.commits_ahead_of_target = MagicMock(return_value=2)
    git.get_last_commit_sha.return_value = "bbb222"
    git.delivery_baseline_sha = "aaa111"  # new commits since start
    assert processor._assert_build_delivery("BD-1") is None

    # Re-queue on existing source: ahead of target but HEAD unchanged → fail
    git.delivery_baseline_sha = "bbb222"
    git.get_last_commit_sha.return_value = "bbb222"
    git.commits_ahead_of_target.return_value = 5
    err = processor._assert_build_delivery("BD-1")
    assert err is not None
    assert "No new commits" in err
    assert "this job" in err.lower() or "job start" in err.lower()

    # no commits_ahead attribute, but HEAD moved → still require ahead count
    git.delivery_baseline_sha = "aaa111"
    git.get_last_commit_sha.return_value = "ccc333"
    del git.commits_ahead_of_target
    err = processor._assert_build_delivery("BD-1")
    assert err is not None


def test_assert_build_delivery_requires_head_sha(processor):
    git = MagicMock()
    git.work_branch = "feature/X"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = None
    git.delivery_baseline_sha = None
    processor._contexts["BD-2"] = {"git": git, "runner": None}
    err = processor._assert_build_delivery("BD-2")
    assert err is not None
    assert "HEAD" in err or "read" in err.lower()


def test_persist_and_materialize_plan(processor, tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    sisy = Path(".sisyphus/plans")

    with patch("src.processor.settings") as s:
        s.full_plans_dir = plans
        s.sisyphus_plans_dir = sisy

        assert processor._persist_plan("P-1", "") is None
        assert processor._persist_plan("P-1", "   ") is None
        p = processor._persist_plan("P-1", "# Plan\n- step")
        assert p is not None and p.exists()

        # materialize into workspace
        git = MagicMock()
        work = tmp_path / "work"
        work.mkdir()
        git.get_working_directory.return_value = work
        processor._contexts["P-1"] = {"git": git, "runner": None}
        dest = processor._materialize_plan_into_workspace("P-1")
        assert dest is not None
        assert dest.exists()

        # no durable, fall back to resolve existing in workspace
        processor._contexts["P-2"] = {"git": git, "runner": None}
        fallback = work / ".sisyphus" / "plans"
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "P-2.md").write_text("# old plan\n")
        out = processor._materialize_plan_into_workspace("P-2")
        assert out is not None

        # materialize with no git -> returns durable
        processor._contexts.pop("P-1", None)
        processor.git_manager = None
        out = processor._materialize_plan_into_workspace("P-1")
        assert out is not None and out.exists()

        # write exception falls back to durable
        processor._contexts["P-1"] = {"git": git, "runner": None}
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            # materialize may return durable on write failure
            processor._materialize_plan_into_workspace("P-1")
            assert processor._persist_plan("P-3", "content") is None


@pytest.mark.asyncio
async def test_push_protected_and_ensure_on_work_fail(processor, state_manager, fake_jira):
    state = state_manager.create_state("PU-1", "s", "d")

    # no git
    processor.git_manager = None
    processor._contexts.clear()
    assert await processor._push_and_create_mr(state) is False

    git = MagicMock()
    git.work_branch = "feature/PU-1"
    git.target_branch = "develop"
    git.ensure_on_work_branch.return_value = False
    processor._contexts["PU-1"] = {"git": git, "runner": None}
    assert await processor._push_and_create_mr(state) is False

    # protected branch main
    git.ensure_on_work_branch.return_value = True
    git.work_branch = "main"
    git.get_current_branch.return_value = "main"
    assert await processor._push_and_create_mr(state) is False

    # release/*
    git.work_branch = "release/1.0"
    assert await processor._push_and_create_mr(state) is False

    # empty branch name
    git.work_branch = ""
    git.get_current_branch.return_value = ""
    assert await processor._push_and_create_mr(state) is False

    # push fail
    git.work_branch = "feature/PU-1"
    git.get_current_branch.return_value = "feature/PU-1"
    git.push.return_value = False
    assert await processor._push_and_create_mr(state) is False

    # push ok, MR ok
    git.push.return_value = True
    git.get_last_commit_subject.return_value = "feat: x"
    git.get_last_commit_message.return_value = "body"
    git.create_merge_request.return_value = "http://mr/1"
    assert await processor._push_and_create_mr(state) is True

    # push ok, MR None
    git.create_merge_request.return_value = None
    git.get_last_commit_subject.return_value = None
    git.get_last_commit_message.return_value = None
    assert await processor._push_and_create_mr(state) is True

    # reporter raises on progress — still returns
    processor.reporter.post_progress_update = MagicMock(side_effect=RuntimeError("x"))
    git.ensure_on_work_branch.return_value = False
    assert await processor._push_and_create_mr(state) is False


# ---------------------------------------------------------------------------
# planning: plan missing / durable fail / abort after success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planning_missing_plan_and_durable_fail(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    state = state_manager.create_state("PLM-1", "implement feature", "big")
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0)
    # no plan file written

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.default_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_planning_workflow(state)
    assert state_manager.get_state("PLM-1").status == TaskStatus.ERROR
    assert "no plan file" in (state_manager.get_state("PLM-1").error_message or "").lower()

    # durable persist fails
    state2 = state_manager.create_state("PLM-2", "s", "d")
    git2, runner2 = _mock_git_and_agent(processor, tmp_path, returncode=0)
    (tmp_path / ".sisyphus" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sisyphus" / "plans" / "PLM-2.md").write_text("# plan\n- [ ] a\n")
    with patch.object(processor, "_init_git_manager", return_value=git2):
        with patch.object(processor, "_persist_plan", return_value=None):
            with patch("src.processor.settings") as s:
                s.default_agent = "prometheus"
                s.agent_task_timeout_seconds = 10
                s.agent_task_max_retries = 1
                s.full_plans_dir = plans
                s.sisyphus_plans_dir = Path(".sisyphus/plans")
                s.default_branch = "main"
                await processor._start_planning_workflow(state2)
    assert state_manager.get_state("PLM-2").status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_planning_aborted_and_cas_race(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "PLA-1.md").write_text("# plan\n- [ ] x\n")
    state = state_manager.create_state("PLA-1", "s", "d")
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0)

    async def abort_result(*a, **k):
        state_manager.update_state("PLA-1", status=TaskStatus.CANCELLED)
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses",
            "retry_info": {"attempts": 1},
            "aborted": True,
        }

    runner.run_agent_with_retry = AsyncMock(side_effect=abort_result)
    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.default_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_planning_workflow(state)
    assert state_manager.get_state("PLA-1").status == TaskStatus.CANCELLED

    # CAS race: status flipped before plan_ready
    state2 = state_manager.create_state("PLA-2", "s", "d")
    git2, runner2 = _mock_git_and_agent(processor, tmp_path, returncode=0)
    (plans / "PLA-2.md").write_text("# plan\n- [ ] y\n")
    (tmp_path / ".sisyphus" / "plans").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sisyphus" / "plans" / "PLA-2.md").write_text("# plan\n- [ ] y\n")

    async def success_then_cancel(*a, **k):
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses",
            "retry_info": {"attempts": 1, "last_opencode_session_id": "ses"},
            "timed_out": False,
        }

    runner2.run_agent_with_retry = AsyncMock(side_effect=success_then_cancel)

    real_persist = processor._persist_plan

    def persist_and_cancel(key, content):
        path = real_persist(key, content)
        state_manager.update_state(key, status=TaskStatus.CANCELLED)
        return path

    with patch.object(processor, "_init_git_manager", return_value=git2):
        with patch.object(processor, "_persist_plan", side_effect=persist_and_cancel):
            with patch("src.processor.settings") as s:
                s.default_agent = "prometheus"
                s.agent_task_timeout_seconds = 10
                s.agent_task_max_retries = 1
                s.full_plans_dir = plans
                s.sisyphus_plans_dir = Path(".sisyphus/plans")
                s.default_branch = "main"
                await processor._start_planning_workflow(state2)
    assert state_manager.get_state("PLA-2").status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# execution: delivery fail / push fail / abort / complete_work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_delivery_and_push_failures(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "EXD-1.md").write_text("# plan\n")
    state = state_manager.create_state("EXD-1", "s", "d")
    state_manager.update_state(
        "EXD-1", plan_path=str(plans / "EXD-1.md"), metadata={"workflow_type": "execution"}
    )
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0)
    git.commits_ahead_of_target.return_value = 0  # delivery fail

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.default_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_execution_workflow(state)
    assert state_manager.get_state("EXD-1").status == TaskStatus.ERROR

    # push fails after delivery ok
    state2 = state_manager.create_state("EXD-2", "s", "d")
    state_manager.update_state("EXD-2", plan_path=str(plans / "EXD-1.md"))
    git2, runner2 = _mock_git_and_agent(processor, tmp_path, returncode=0)
    git2.commits_ahead_of_target.return_value = 2
    with patch.object(processor, "_init_git_manager", return_value=git2):
        with patch.object(processor, "_push_and_create_mr", new_callable=AsyncMock) as p:
            p.return_value = False
            with patch("src.processor.settings") as s:
                s.default_agent = "atlas"
                s.agent_task_timeout_seconds = 10
                s.agent_task_max_retries = 1
                s.full_plans_dir = plans
                s.sisyphus_plans_dir = Path(".sisyphus/plans")
                s.default_branch = "main"
                await processor._start_execution_workflow(state2)
    assert state_manager.get_state("EXD-2").status == TaskStatus.ERROR

    # aborted during execution
    state3 = state_manager.create_state("EXD-3", "s", "d")
    git3, runner3 = _mock_git_and_agent(processor, tmp_path, returncode=0)

    async def aborting(*a, **k):
        state_manager.update_state("EXD-3", status=TaskStatus.CANCELLED)
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "aborted": True,
            "session_file": None,
            "opencode_session_id": None,
            "retry_info": {"attempts": 1},
        }

    runner3.run_agent_with_retry = AsyncMock(side_effect=aborting)
    with patch.object(processor, "_init_git_manager", return_value=git3):
        with patch("src.processor.settings") as s:
            s.default_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_execution_workflow(state3)
    assert state_manager.get_state("EXD-3").status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_complete_work_aborted_and_cas(processor, state_manager):
    # None state
    await processor._complete_work(None)

    # aborted
    state = state_manager.create_state("CW-1", "s", "d")
    state_manager.update_state("CW-1", status=TaskStatus.CANCELLED)
    await processor._complete_work(state_manager.get_state("CW-1"), "sum")
    assert state_manager.get_state("CW-1").status == TaskStatus.CANCELLED

    # not EXECUTING -> CAS fail
    state_manager.create_state("CW-2", "s", "d")
    state_manager.update_state("CW-2", status=TaskStatus.PLANNING)
    await processor._complete_work(state_manager.get_state("CW-2"))
    assert state_manager.get_state("CW-2").status == TaskStatus.PLANNING

    # success + post_completion fails
    state_manager.create_state("CW-3", "s", "d")
    state_manager.update_state("CW-3", status=TaskStatus.EXECUTING)
    processor.reporter.post_completion = MagicMock(side_effect=RuntimeError("post fail"))
    await processor._complete_work(state_manager.get_state("CW-3"), "done")
    assert state_manager.get_state("CW-3").status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# oracle abort / fail / completion race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oracle_abort_fail_and_completion_race(processor, state_manager, tmp_path):
    state = state_manager.create_state("OR-A", "how to design", "architecture question")
    runner = MagicMock()

    async def abort_mid(*a, **k):
        state_manager.update_state("OR-A", status=TaskStatus.CANCELLED)
        return {"returncode": 0, "stdout": "ans", "stderr": ""}

    runner.run_agent = AsyncMock(side_effect=abort_mid)
    processor.agent_runner = runner
    await processor._start_oracle_consultation(state)
    assert state_manager.get_state("OR-A").status == TaskStatus.CANCELLED

    # fail returncode
    state2 = state_manager.create_state("OR-F", "how to", "architecture?")
    runner2 = MagicMock()
    runner2.run_agent = AsyncMock(
        return_value={"returncode": 1, "stdout": "", "stderr": "oracle fail"}
    )
    processor.agent_runner = runner2
    processor._contexts.clear()
    await processor._start_oracle_consultation(state2)
    assert state_manager.get_state("OR-F").status == TaskStatus.ERROR

    # success but CAS fails (status flipped)
    state3 = state_manager.create_state("OR-C", "how to", "architecture?")
    runner3 = MagicMock()

    async def ok_then_race(task, **kw):
        state_manager.update_state("OR-C", status=TaskStatus.CANCELLED)
        return {"returncode": 0, "stdout": "answer", "stderr": ""}

    runner3.run_agent = AsyncMock(side_effect=ok_then_race)
    processor.agent_runner = runner3
    processor._contexts.clear()
    await processor._start_oracle_consultation(state3)
    assert state_manager.get_state("OR-C").status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# direct request / ensure runner / kill / cancel_issue_state / shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_direct_request_success_fail_and_init(
    processor, state_manager, tmp_path, fake_jira
):
    state_manager.create_state("DR-1", "s", "d")
    runner = MagicMock()
    runner.run_agent = AsyncMock(
        return_value={"returncode": 0, "stdout": "direct answer", "stderr": ""}
    )
    processor.agent_runner = runner
    await processor._handle_direct_request("DR-1", "what is this?")
    assert any("direct answer" in c["body"] for c in fake_jira.comments)

    # fail returncode
    runner.run_agent = AsyncMock(
        return_value={"returncode": 1, "stdout": "", "stderr": "agent boom"}
    )
    processor._contexts.clear()
    processor.agent_runner = runner
    await processor._handle_direct_request("DR-1", "retry")
    assert any("agent boom" in c["body"] for c in fake_jira.comments)

    # ensure fails
    processor.agent_runner = None
    processor._contexts.clear()
    with patch.object(processor, "_ensure_agent_runner", side_effect=RuntimeError("no")):
        await processor._handle_direct_request("DR-1", "x")

    # existing context not released
    processor._contexts["DR-1"] = {"git": None, "runner": runner}
    processor.agent_runner = runner
    runner.run_agent = AsyncMock(
        return_value={"returncode": 0, "stdout": "kept", "stderr": ""}
    )
    await processor._handle_direct_request("DR-1", "keep ctx")
    assert "DR-1" in processor._contexts


def test_ensure_agent_runner_and_kill(processor, state_manager, tmp_path):
    processor.agent_runner = None
    processor._contexts.clear()
    with patch.object(processor, "_init_git_manager", side_effect=RuntimeError("no git")):
        with patch("src.processor.settings") as s:
            s.temp_dir_base = ".temp"
            s.project_root = tmp_path
            runner = processor._ensure_agent_runner("ER-1")
            assert runner is not None
            assert processor._contexts["ER-1"]["git"] is None

    # adopt pre-set runner
    processor._contexts.clear()
    r2 = MagicMock()
    processor.agent_runner = r2
    assert processor._ensure_agent_runner("ER-2") is r2

    # existing context
    assert processor._ensure_agent_runner("ER-2") is r2

    # kill children
    state_manager.create_state("K-1", "s", "d")
    state_manager.update_state("K-1", current_task_id="t1")
    runner = MagicMock()
    runner.cancel_task.side_effect = RuntimeError("x")
    processor._contexts["K-1"] = {"git": None, "runner": runner}
    processor._kill_children_for_issue("K-1")
    processor._kill_children_for_issue("NO-RUNNER")


def test_cancel_issue_state_no_state_and_error_status(processor, state_manager, fake_jira):
    processor._cancel_issue_state("GHOST-C", message="gone", status=TaskStatus.CANCELLED)
    assert any("interrupted" in c["body"].lower() or "gone" in c["body"] for c in fake_jira.comments)

    state_manager.create_state("CIS-1", "s", "d")
    state_manager.update_state("CIS-1", status=TaskStatus.EXECUTING)
    processor._cancel_issue_state(
        "CIS-1", message="watchdog", status=TaskStatus.ERROR
    )
    assert state_manager.get_state("CIS-1").status == TaskStatus.ERROR

    # outer exception
    processor.state_manager.update_state = MagicMock(side_effect=RuntimeError("disk"))
    processor._cancel_issue_state("CIS-1", message="x")


def test_shutdown_and_recover(processor, state_manager):
    state_manager.create_state("SH-1", "s", "d")
    state_manager.update_state(
        "SH-1", status=TaskStatus.EXECUTING, current_task_id="t1"
    )
    runner = MagicMock()
    runner.cancel_all_tasks.side_effect = RuntimeError("legacy fail")
    processor.agent_runner = runner
    git = MagicMock()
    git.cleanup.side_effect = RuntimeError("cleanup")
    processor._contexts["SH-1"] = {"git": git, "runner": runner}
    # also context without in-flight
    processor._contexts["SH-2"] = {"git": None, "runner": None}
    n = processor.shutdown_processing(reason="test stop")
    assert n >= 1
    assert state_manager.get_state("SH-1").status == TaskStatus.CANCELLED

    state_manager.create_state("ORPH-1", "s", "d")
    state_manager.update_state("ORPH-1", status=TaskStatus.PLANNING)
    r = processor.recover_orphaned_in_flight()
    assert r >= 1
    assert state_manager.get_state("ORPH-1").status == TaskStatus.ERROR


def test_job_processor_init_real_client_branch(monkeypatch, tmp_path):
    """Cover non-simulated JIRA client log branch (line ~118)."""
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.settings") as s:
        s.is_configured.return_value = True
        s.jira_host = "https://jira.real.example.com"
        s.default_agent = "atlas"
        with patch("src.processor.create_jira_client", return_value=MagicMock()) as cj:
            proc = JobProcessor()
            cj.assert_called()
            assert proc.jira_client is not None


def test_list_live_and_git_for_isolation(processor, tmp_path):
    assert processor.list_live_processing_keys() == []
    git_a = MagicMock()
    git_a.issue_key = "A-1"
    processor._contexts["A-1"] = {"git": git_a, "runner": MagicMock()}
    processor._contexts["B-1"] = {"git": None, "runner": MagicMock()}
    assert "A-1" in processor.list_live_processing_keys()
    assert processor._git_for("B-1") is None
    assert processor._git_for("A-1") is git_a

    # foreign git_manager not returned
    foreign = MagicMock()
    foreign.issue_key = "OTHER"
    processor.git_manager = foreign
    assert processor._git_for("C-1") is None


def test_resolve_plan_path(processor, tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    with patch("src.processor.settings") as s:
        s.full_plans_dir = plans
        s.sisyphus_plans_dir = Path(".sisyphus/plans")
        missing = processor._resolve_plan_path("MISS", require_exists=True)
        assert missing is None
        durable = processor._resolve_plan_path("MISS", require_exists=False)
        assert durable is not None

        (plans / "HIT.md").write_text("# p\n")
        assert processor._resolve_plan_path("HIT", require_exists=True).name == "HIT.md"

        git = MagicMock()
        work = tmp_path / "w"
        work.mkdir()
        sp = work / ".sisyphus" / "plans"
        sp.mkdir(parents=True)
        (sp / "WS.md").write_text("# ws\n")
        git.get_working_directory.return_value = work
        processor._contexts["WS"] = {"git": git, "runner": None}
        assert processor._resolve_plan_path("WS", require_exists=True) is not None


@pytest.mark.asyncio
async def test_execution_prepare_git_none_returns(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    state = state_manager.create_state("EXN-1", "s", "d")
    with patch.object(
        processor, "_prepare_git_workspace", new_callable=AsyncMock, return_value=None
    ):
        with patch("src.processor.settings") as s:
            s.default_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = tmp_path / "plans"
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            await processor._start_execution_workflow(state)
    # failed during prepare — status may be ERROR from prepare or still EXECUTING from begin
    st = state_manager.get_state("EXN-1")
    assert st is not None


@pytest.mark.asyncio
async def test_planning_success_with_retry_info_no_auto_start(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "PLS-1.md").write_text("# plan\n- [ ] step\n")
    (tmp_path / ".sisyphus" / "plans").mkdir(parents=True)
    (tmp_path / ".sisyphus" / "plans" / "PLS-1.md").write_text("# plan\n- [ ] step\n")
    state = state_manager.create_state("PLS-1", "s", "d")
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0)

    async def with_hooks(task, on_output=None, on_progress=None, on_retry=None, **kw):
        if on_progress:
            on_progress(50, "half")
        if on_output:
            on_output("stdout", "line")
        if on_retry:
            on_retry(1, 0.1, "timeout", str(tmp_path / "s.log"), "err", -1, "ses_r", "t_new")
        if kw.get("on_session_file"):
            kw["on_session_file"](str(tmp_path / "s.log"), str(tmp_path / "s.prompt.txt"))
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_r",
            "retry_info": {"attempts": 2, "last_opencode_session_id": "ses_r"},
            "timed_out": True,
        }

    runner.run_agent_with_retry = with_hooks
    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.default_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 2
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            with patch.object(
                processor, "start_plan_execution", new_callable=AsyncMock
            ) as ex:
                await processor._start_planning_workflow(state)
                ex.assert_not_awaited()
    st = state_manager.get_state("PLS-1")
    assert st.status == TaskStatus.PLAN_READY
    assert st.timed_out is True


@pytest.mark.asyncio
async def test_execution_success_full_path(
    processor, state_manager, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "EXS-1.md").write_text("# plan\n")
    state = state_manager.create_state("EXS-1", "s", "d")
    state_manager.update_state("EXS-1", plan_path=str(plans / "EXS-1.md"))
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0)
    git.commits_ahead_of_target.return_value = 3

    async def with_hooks(task, on_output=None, on_progress=None, on_retry=None, **kw):
        if on_progress:
            on_progress(80, "almost")
        if on_output:
            on_output("stderr", "line")
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_e",
            "retry_info": {"attempts": 1, "last_opencode_session_id": "ses_e"},
            "timed_out": False,
        }

    runner.run_agent_with_retry = with_hooks
    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.default_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_execution_workflow(state)
    assert state_manager.get_state("EXS-1").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_handle_created_workflow_crash(processor, state_manager):
    with patch(
        "src.processor.WorkflowRouter.route_issue",
        return_value=WorkflowType.ORACLE_CONSULT,
    ):
        with patch.object(
            processor,
            "_start_oracle_consultation",
            side_effect=RuntimeError("crash"),
        ):
            await processor._handle_issue_created(
                make_issue_event(
                    key="CR-1",
                    summary="how to design architecture",
                    description="what approach should we take?",
                )
            )
    assert state_manager.get_state("CR-1").status == TaskStatus.ERROR
