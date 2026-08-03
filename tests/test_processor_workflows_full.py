"""Exhaustive branch coverage for JobProcessor workflows."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.workflow_router import WorkflowType
from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient, make_issue_event


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
    processor.git_manager = git
    processor.agent_runner = runner
    return git, runner


@pytest.mark.asyncio
async def test_process_unknown_event(processor):
    await processor.process_event({"webhookEvent": "unknown", "issue": {"key": "X"}})


@pytest.mark.asyncio
async def test_process_event_unhandled_sets_error(processor, state_manager, fake_jira):
    # State must exist for update_state path; also covers bare-comment fallback
    state_manager.create_state("UE-1", "s", "d")
    with patch.object(
        processor,
        "_handle_issue_created",
        side_effect=RuntimeError("boom"),
    ):
        await processor.process_event(make_issue_event(key="UE-1"))
    loaded = state_manager.get_state("UE-1")
    assert loaded is not None
    assert loaded.status == TaskStatus.ERROR
    assert any("boom" in c["body"] for c in fake_jira.comments)


@pytest.mark.asyncio
async def test_handle_created_skips_in_flight(processor, state_manager):
    state_manager.create_state("IF-1", "s", "d")
    state_manager.update_state("IF-1", status=TaskStatus.EXECUTING)
    with patch.object(processor, "_start_execution_workflow", new_callable=AsyncMock) as m:
        await processor._handle_issue_created(make_issue_event(key="IF-1"))
        m.assert_not_called()


@pytest.mark.asyncio
async def test_handle_created_non_string_description(processor, state_manager, tmp_path):
    _mock_git_and_agent(processor, tmp_path)
    event = make_issue_event(key="NS-1", description={"type": "doc"})  # type: ignore
    # description may be dict in event — we coerce
    event["issue"]["fields"]["description"] = {"type": "doc"}
    with patch("src.processor.WorkflowRouter.route_issue", return_value=WorkflowType.EXECUTION):
        with patch.object(processor, "_init_git_manager", return_value=processor.git_manager):
            await processor._handle_issue_created(event)
    assert state_manager.get_state("NS-1") is not None


@pytest.mark.asyncio
async def test_handle_created_existing_terminal_reset(processor, state_manager, tmp_path):
    state_manager.create_state("TR-1", "old", "old")
    state_manager.update_state("TR-1", status=TaskStatus.ERROR)
    _mock_git_and_agent(processor, tmp_path)
    with patch("src.processor.WorkflowRouter.route_issue", return_value=WorkflowType.EXECUTION):
        with patch.object(processor, "_init_git_manager", return_value=processor.git_manager):
            with patch.object(processor, "_start_execution_workflow", new_callable=AsyncMock) as m:
                await processor._handle_issue_created(
                    make_issue_event(key="TR-1", summary="new sum", description="new desc")
                )
                m.assert_awaited()


@pytest.mark.asyncio
async def test_handle_updated_no_key(processor):
    await processor._handle_issue_updated({"issue": {}})


@pytest.mark.asyncio
async def test_handle_updated_no_state_creates(processor, tmp_path):
    _mock_git_and_agent(processor, tmp_path)
    with patch.object(processor, "_handle_issue_created", new_callable=AsyncMock) as m:
        await processor._handle_issue_updated(make_issue_event(key="NEW-1", event_type="jira:issue_updated"))
        m.assert_awaited()


@pytest.mark.asyncio
async def test_handle_updated_pending_todo_starts(processor, state_manager):
    state_manager.create_state("PD-1", "s", "d")
    with patch.object(processor, "_handle_issue_created", new_callable=AsyncMock) as m:
        await processor._handle_issue_updated(
            make_issue_event(key="PD-1", event_type="jira:issue_updated", status="To Do")
        )
        m.assert_awaited()


@pytest.mark.asyncio
async def test_comment_paths(processor, state_manager, tmp_path, fake_jira):
    # empty
    await processor._handle_comment_created({})
    # dict body
    with patch.object(processor, "_handle_bot_command", new_callable=AsyncMock) as m:
        with patch(
            "src.processor.WorkflowRouter.extract_mention_command",
            return_value="/status",
        ):
            await processor._handle_comment_created(
                {
                    "issue": {"key": "C-1"},
                    "comment": {"body": {"type": "doc"}},
                }
            )
            m.assert_awaited()
    # no command
    with patch("src.processor.WorkflowRouter.extract_mention_command", return_value=None):
        await processor._handle_comment_created(
            {"issue": {"key": "C-1"}, "comment": {"body": "hi"}}
        )


@pytest.mark.asyncio
async def test_bot_commands(processor, state_manager, tmp_path, fake_jira):
    state_manager.create_state("BC-1", "s", "d")
    state_manager.update_state("BC-1", status=TaskStatus.PLAN_READY, plan_path="p.md")
    with patch.object(processor, "_start_execution_workflow", new_callable=AsyncMock) as m:
        await processor._handle_bot_command("BC-1", "/start-work")
        m.assert_awaited()

    # force not plan ready so a second /start-work does not run real execution
    state_manager.update_state("BC-1", status=TaskStatus.PENDING)
    await processor._handle_bot_command("BC-1", "/start-work")

    await processor._handle_bot_command("BC-1", "/status")
    await processor._handle_bot_command("NOPE", "/status")

    state_manager.update_state("BC-1", status=TaskStatus.EXECUTING, current_task_id="t1")
    runner = MagicMock()
    runner.cancel_task.return_value = True
    processor.agent_runner = runner
    await processor._handle_bot_command("BC-1", "/cancel")
    await processor._handle_bot_command("NOPE", "/cancel")

    with patch.object(processor, "_handle_direct_request", new_callable=AsyncMock) as d:
        await processor._handle_bot_command("BC-1", "please explain")
        d.assert_awaited()

    # start-work crash
    state_manager.update_state("BC-1", status=TaskStatus.PLAN_READY)
    with patch.object(
        processor,
        "_start_execution_workflow",
        side_effect=RuntimeError("exec fail"),
    ):
        await processor._handle_bot_command("BC-1", "/start-work")
    assert state_manager.get_state("BC-1").status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_planning_success_and_fail(processor, state_manager, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plans = tmp_path / ".sisyphus" / "plans"
    plans.mkdir(parents=True)
    state = state_manager.create_state("PL-1", "implement feature", "big feature implement create")
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0, stdout="planned")
    (plans / "PL-1.md").write_text("# plan\n- [ ] step")

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.planning_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            # on_retry callback path during planning — success no retry
            await processor._start_planning_workflow(state)

    assert state_manager.get_state("PL-1").status == TaskStatus.PLAN_READY

    # fail path
    state2 = state_manager.create_state("PL-2", "s", "d")
    git2, runner2 = _mock_git_and_agent(processor, tmp_path, returncode=1, stderr="plan fail")
    with patch.object(processor, "_init_git_manager", return_value=git2):
        with patch("src.processor.settings") as s:
            s.planning_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            await processor._start_planning_workflow(state2)
    assert state_manager.get_state("PL-2").status == TaskStatus.ERROR

    # success never auto-starts (intentional; label or new Mode: build issue)
    state3 = state_manager.create_state("PL-3", "s", "d")
    (plans / "PL-3.md").write_text("# plan\n- [ ] step")
    git3, runner3 = _mock_git_and_agent(processor, tmp_path, returncode=0)
    with patch.object(processor, "_init_git_manager", return_value=git3):
        with patch("src.processor.settings") as s:
            s.planning_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            with patch.object(
                processor, "start_plan_execution", new_callable=AsyncMock
            ) as ex:
                await processor._start_planning_workflow(state3)
                ex.assert_not_awaited()
    assert state_manager.get_state("PL-3").status == TaskStatus.PLAN_READY


@pytest.mark.asyncio
async def test_planning_retry_callback(processor, state_manager, tmp_path):
    state = state_manager.create_state("PLR-1", "s", "d")
    git, runner = _mock_git_and_agent(processor, tmp_path)

    async def with_retry(task, on_output=None, on_progress=None, on_retry=None, **kw):
        if on_progress:
            on_progress(10, "p")
        if on_output:
            on_output("stdout", "line")
        if on_retry:
            on_retry(1, 1.0, "timeout", "/tmp/s.log", "err", -1, "ses_r")
        return {
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "session_file": "/tmp/s.log",
            "opencode_session_id": "ses_r",
            "retry_info": {
                "attempts": 2,
                "last_opencode_session_id": "ses_r",
            },
            "timed_out": True,
        }

    runner.run_agent_with_retry = with_retry
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "PLR-1.md").write_text("# plan\n- [ ] a\n")
    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.planning_agent = "prometheus"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 2
            s.full_plans_dir = plans
            s.sisyphus_plans_dir = Path(".sisyphus/plans")
            s.default_branch = "main"
            await processor._start_planning_workflow(state)
    assert state_manager.get_state("PLR-1").retry_count >= 1


@pytest.mark.asyncio
async def test_execution_and_direct_and_review(processor, state_manager, tmp_path):
    state = state_manager.create_state("EX-1", "s", "d")
    state_manager.update_state("EX-1", plan_path="p.md", metadata={"workflow_type": "planning"})
    git, runner = _mock_git_and_agent(processor, tmp_path, returncode=0, stdout="exec ok")

    with patch.object(processor, "_init_git_manager", return_value=git):
        with patch("src.processor.settings") as s:
            s.orchestrator_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.default_branch = "main"
            await processor._start_execution_workflow(state)

    assert state_manager.get_state("EX-1").status == TaskStatus.COMPLETED

    # execution fail
    state_f = state_manager.create_state("EX-F", "s", "d")
    git_f, runner_f = _mock_git_and_agent(processor, tmp_path, returncode=1, stderr="exec fail")
    with patch.object(processor, "_init_git_manager", return_value=git_f):
        with patch("src.processor.settings") as s:
            s.orchestrator_agent = "atlas"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            await processor._start_execution_workflow(state_f)
    assert state_manager.get_state("EX-F").status == TaskStatus.ERROR

    # direct success + review fail still completes
    state_d = state_manager.create_state("DX-1", "fix typo", "fix it")
    git_d, runner_d = _mock_git_and_agent(processor, tmp_path, returncode=0, stdout="fixed")
    # review fails
    async def run_agent_side(task, **kw):
        if getattr(task, "task_type", None) == "review":
            return {"returncode": 1, "stdout": "", "stderr": "review boom"}
        return {"returncode": 0, "stdout": "fixed", "stderr": ""}

    runner_d.run_agent = AsyncMock(side_effect=run_agent_side)
    with patch.object(processor, "_init_git_manager", return_value=git_d):
        with patch("src.processor.settings") as s:
            s.default_agent = "sisyphus"
            s.execution_category = "deep"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            s.default_branch = "main"
            await processor._start_execution_workflow(state_d)
    assert state_manager.get_state("DX-1").status == TaskStatus.COMPLETED

    # direct fail
    state_df = state_manager.create_state("DX-F", "fix", "fix")
    git_df, runner_df = _mock_git_and_agent(processor, tmp_path, returncode=1, stderr="nope")
    with patch.object(processor, "_init_git_manager", return_value=git_df):
        with patch("src.processor.settings") as s:
            s.default_agent = "sisyphus"
            s.execution_category = "deep"
            s.agent_task_timeout_seconds = 10
            s.agent_task_max_retries = 1
            await processor._start_execution_workflow(state_df)
    assert state_manager.get_state("DX-F").status == TaskStatus.ERROR



@pytest.mark.asyncio
async def test_push_and_create_mr_branches(processor, state_manager, tmp_path, fake_jira):
    state = state_manager.create_state("MR-1", "s", "d")
    # no git
    processor.git_manager = None
    await processor._push_and_create_mr(state)

    git = MagicMock()
    processor.git_manager = git
    processor._contexts[state.issue_key] = {"git": git, "runner": None}
    git.work_branch = "main"
    git.target_branch = "main"
    git.ensure_on_work_branch.return_value = True
    git.get_current_branch.return_value = "main"
    with patch("src.processor.settings") as s:
        s.default_branch = "main"
        await processor._push_and_create_mr(state)

    git.work_branch = "feature/MR-1"
    git.get_current_branch.return_value = "feature/MR-1"
    git.push.return_value = False
    await processor._push_and_create_mr(state)

    git.push.return_value = True
    git.get_last_commit_subject.return_value = None
    git.get_last_commit_message.return_value = None
    git.create_merge_request.return_value = None
    with patch("src.processor.settings") as s:
        s.default_branch = "develop"
        await processor._push_and_create_mr(state)

    git.create_merge_request.return_value = "http://mr/2"
    git.get_last_commit_subject.return_value = "subj"
    git.get_last_commit_message.return_value = "body"
    with patch("src.processor.settings") as s:
        s.default_branch = "develop"
        await processor._push_and_create_mr(state)
    loaded = state_manager.get_state("MR-1")
    assert loaded.metadata.get("merge_request_url") == "http://mr/2"


@pytest.mark.asyncio
async def test_oracle_success(processor, state_manager, tmp_path):
    state = state_manager.create_state("OR-1", "how to design", "architecture question")
    runner = MagicMock()
    runner.run_agent = AsyncMock(
        return_value={"returncode": 0, "stdout": "use pattern X", "stderr": ""}
    )
    processor.agent_runner = runner
    await processor._start_oracle_consultation(state)
    loaded = state_manager.get_state("OR-1")
    assert loaded.status == TaskStatus.COMPLETED
    assert loaded.current_task_id is None  # cleared after success


@pytest.mark.asyncio
async def test_oracle_sets_current_task_id_before_run(processor, state_manager):
    """Oracle must register current_task_id so /cancel and stuck watchdog work."""
    state = state_manager.create_state("OR-TID", "how to design", "architecture question")
    seen = {}

    async def _run(task, **_kwargs):
        st = state_manager.get_state("OR-TID")
        seen["task_id"] = task.task_id
        seen["state_task_id"] = st.current_task_id if st else None
        seen["status"] = st.status if st else None
        return {"returncode": 0, "stdout": "answer", "stderr": ""}

    runner = MagicMock()
    runner.run_agent = AsyncMock(side_effect=_run)
    processor.agent_runner = runner

    await processor._start_oracle_consultation(state)

    assert seen["status"] == TaskStatus.EXECUTING
    assert seen["state_task_id"] is not None
    assert seen["state_task_id"] == seen["task_id"]


@pytest.mark.asyncio
async def test_ensure_agent_runner_fallback(processor, tmp_path):
    processor.agent_runner = None
    with patch.object(processor, "_init_git_manager", side_effect=RuntimeError("no git")):
        with patch("src.processor.settings") as s:
            s.project_root = tmp_path
            runner = processor._ensure_agent_runner("X-1")
            assert runner is not None


@pytest.mark.asyncio
async def test_direct_request_success_and_init_fail(processor, state_manager, tmp_path, fake_jira):
    state_manager.create_state("DR-1", "s", "d")
    runner = MagicMock()
    runner.run_agent = AsyncMock(
        return_value={"returncode": 0, "stdout": "answer", "stderr": ""}
    )
    processor.agent_runner = runner
    await processor._handle_direct_request("DR-1", "what is status?")
    assert any("answer" in c["body"] for c in fake_jira.comments)

    processor.agent_runner = None
    with patch.object(processor, "_ensure_agent_runner", side_effect=RuntimeError("no")):
        await processor._handle_direct_request("DR-1", "x")


@pytest.mark.asyncio
async def test_fail_issue_missing_state(processor, fake_jira):
    processor._fail_issue("GHOST-1", "no state file")
    assert fake_jira.comments


@pytest.mark.asyncio
async def test_init_git_manager(processor, tmp_path, state_manager):
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: plan\n"
        "{params}\n"
    )
    state_manager.create_state("IG-1", "task", desc)
    with patch("src.processor.GitManager") as GM:
        inst = MagicMock()
        inst.get_working_directory.return_value = tmp_path
        GM.return_value = inst
        with patch("src.processor.AgentRunner"):
            g = processor._init_git_manager("IG-1")
            assert g is inst
            GM.assert_called()
            kwargs = GM.call_args.kwargs
            assert "gitlab.example.com" in kwargs.get("remote_url", "")
            assert kwargs.get("source_branch") == "develop"
            assert kwargs.get("target_branch") == "main"


@pytest.mark.asyncio
async def test_prepare_git_workspace_template_error(processor, state_manager, fake_jira):
    state = state_manager.create_state("TPL-1", "no git fields", "just a task")
    processor._begin_workflow_run = MagicMock()
    # Already need an in-flight-ish state for fail path
    out = processor._prepare_git_workspace_blocking(state)
    assert out is None
    assert fake_jira.comments  # Jira notified
    loaded = state_manager.get_state("TPL-1")
    assert loaded is not None
    assert loaded.status == TaskStatus.ERROR


@pytest.mark.asyncio
async def test_ack_failure_still_runs(processor, state_manager, tmp_path):
    processor.reporter.post_initial_acknowledgment = MagicMock(side_effect=RuntimeError("x"))
    with patch("src.processor.WorkflowRouter.route_issue", return_value=WorkflowType.ORACLE_CONSULT):
        with patch.object(processor, "_start_oracle_consultation", new_callable=AsyncMock) as m:
            await processor._handle_issue_created(
                make_issue_event(key="ACK-1", summary="how to approach architecture")
            )
            m.assert_awaited()


@pytest.mark.asyncio
async def test_route_planning_from_create(processor, tmp_path):
    with patch("src.processor.WorkflowRouter.route_issue", return_value=WorkflowType.PLANNING):
        with patch.object(processor, "_start_planning_workflow", new_callable=AsyncMock) as m:
            await processor._handle_issue_created(
                make_issue_event(key="RP-1", summary="implement feature", description="big")
            )
            m.assert_awaited()
