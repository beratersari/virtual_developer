"""
Architecture critical findings — proof tests.

These encode the *correct* behaviour for issues found by multi-agent review.
If a test FAILS, the production code still has that logical bug.
If a test PASSES, that finding is fixed (or never existed under current code).

No assumptions: each test constructs concrete inputs and asserts observable
outcomes only. Green CI may still ignore this file until bugs are fixed;
run explicitly:

  .venv/bin/python -m pytest tests/test_architecture_critical_findings.py -v
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    p._last_jira_status = {}
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


# ---------------------------------------------------------------------------
# C1 — Poller: category-"new" status names outside hard-coded To Do list
# ---------------------------------------------------------------------------


def test_c1_completed_on_selected_for_development_must_not_reprocess_every_poll(
    poller, state_manager
):
    """
    Board eligibility uses statusCategory.key == 'new' (locale-safe).
    Reprocess uses _is_todo_status_name(prev) only — a small name set.

    'Selected for Development' is category-new but not in that name set.
    If the ticket stays there after COMPLETED, entered_todo_from_elsewhere
    must NOT fire every poll (infinite re-execution).
    """
    state_manager.create_state("SFD-1", "done", "")
    state_manager.update_state("SFD-1", status=TaskStatus.COMPLETED)
    # Same status name before and after the poll (no user move)
    poller._status_before_poll = {"SFD-1": "selected for development"}
    poller._last_jira_status = {"SFD-1": "selected for development"}

    issues = [
        {
            "key": "SFD-1",
            "fields": {
                "status": {
                    "name": "Selected for Development",
                    "statusCategory": {"key": "new"},
                },
                "labels": ["ai-assist"],
            },
        }
    ]
    assert poller.check_status_changes(issues) == [], (
        "Terminal issue still on category-new 'Selected for Development' "
        "must not reprocess every poll"
    )


def test_c1_still_on_category_new_status_does_not_look_like_reentry(poller, state_manager):
    """Symmetric case: ERROR terminal, same non-English-ish category-new name."""
    state_manager.create_state("SFD-2", "err", "body")
    state_manager.update_state(
        "SFD-2",
        status=TaskStatus.ERROR,
        metadata={
            "requeue_eligible": True,
            # Match light board fingerprint (summary only) so text_changed is false
            "last_intake_fingerprint": poller.issue_text_fingerprint(
                {"fields": {"summary": "err", "description": ""}}
            ),
        },
    )
    poller._status_before_poll = {"SFD-2": "ready for development"}
    issues = [
        {
            "key": "SFD-2",
            "fields": {
                "summary": "err",
                "status": {
                    "name": "Ready for Development",
                    "statusCategory": {"key": "new"},
                },
                "labels": ["bot"],
            },
        }
    ]
    assert poller.check_status_changes(issues) == []


# ---------------------------------------------------------------------------
# C2 — Poller: light board payload vs full-description fingerprint
# ---------------------------------------------------------------------------


def test_c2_error_reprocess_must_not_fire_when_board_omits_description(
    poller, state_manager
):
    """
    poll_board fetches issues WITHOUT description.
    _fail_issue stores fingerprint of summary + full description.
    Comparing against a description-less board issue always looks 'edited'.

    Expected: no reprocess when the user did not change text.
    """
    summary = "fix auth"
    full_desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/g/r.git\n"
        "Source branch: feature/x\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}"
    )
    # What _fail_issue stores (full description from local state)
    last_fp = hashlib.sha256(
        f"{summary}\n{full_desc}".encode("utf-8", errors="replace")
    ).hexdigest()[:20]

    state_manager.create_state("E-LIGHT", summary, full_desc)
    state_manager.update_state(
        "E-LIGHT",
        status=TaskStatus.ERROR,
        metadata={
            "requeue_eligible": True,
            "last_intake_fingerprint": last_fp,
        },
    )
    poller._status_before_poll = {"E-LIGHT": "to do"}

    # What poll_board actually hands to check_status_changes (no description)
    light = {
        "key": "E-LIGHT",
        "fields": {
            "summary": summary,
            # description intentionally absent
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": ["bot"],
        },
    }
    assert poller.check_status_changes([light]) == [], (
        "Board scan without description must not look like a text change "
        "vs fingerprint stored with full description"
    )


# ---------------------------------------------------------------------------
# C3 — Fail → In Progress tracker is INTENTIONAL when the board really left To Do
# ---------------------------------------------------------------------------


def test_c3_intentional_force_after_in_progress_reprocesses_on_return_to_todo(
    poller, state_manager
):
    """
    Product UX (AGENTS.md): accept issue → move Jira to In Progress → on ERROR
    post a clear comment → operator fixes and moves *back* to To Do → requeue.

    force_after_in_progress when prev tracker is 'in progress' and board is To Do
    is the designed leave→return signal (not a bug).
    """
    state_manager.create_state("IP-OK", "s", "body")
    state_manager.update_state(
        "IP-OK",
        status=TaskStatus.ERROR,
        metadata={"requeue_eligible": True, "last_intake_fingerprint": "abc"},
    )
    # Real prior board status was In Progress (transition succeeded)
    poller._status_before_poll = {"IP-OK": "in progress"}
    issues = [
        {
            "key": "IP-OK",
            "fields": {
                "summary": "s",
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["bot"],
            },
        }
    ]
    out = poller.check_status_changes(issues)
    assert [i["key"] for i in out] == ["IP-OK"]


def test_c3_fail_does_not_invent_in_progress_tracker_when_transition_fails(
    processor, state_manager, fake_jira
):
    """
    Residual edge case under the intentional design: if the Jira transition
    never succeeds, the poller tracker must NOT be forced to 'in progress'
    (that would re-fire every poll while the ticket stays To Do forever).
    """
    state_manager.create_state("IP-STUCK", "s", "missing mode")
    fake_jira.transition_to_in_progress = MagicMock(return_value=False)
    poller = MagicMock()
    poller._last_jira_status = {"IP-STUCK": "to do"}
    processor._poller = poller

    processor._fail_issue("IP-STUCK", "config error: missing Mode")

    assert state_manager.get_state("IP-STUCK").status == TaskStatus.ERROR
    # Tracker must stay aligned with board (still To Do)
    assert poller._last_jira_status.get("IP-STUCK") == "to do"


# ---------------------------------------------------------------------------
# C4 — Cancel race: non-CAS _begin_workflow_run overwrites CANCELLED
# ---------------------------------------------------------------------------


def test_c4_begin_workflow_must_not_overwrite_cancelled(processor, state_manager):
    """
    cancel_job does not take the issue lock (by design).
    _begin_workflow_run uses plain update_state(status=PLANNING|EXECUTING),
    which can revive a CANCELLED issue after dashboard cancel succeeded.

    Expected: begin refuses to move CANCELLED → EXECUTING.
    """
    state_manager.create_state("RACE-1", "s", "d")
    # Operator cancelled while still PENDING (before agent start)
    state_manager.update_state(
        "RACE-1",
        status=TaskStatus.CANCELLED,
        error_message="Cancelled from dashboard",
        completed_at=datetime.now(),
    )
    assert state_manager.get_state("RACE-1").status == TaskStatus.CANCELLED

    from src.orchestrator.agent_runner import AgentTask

    task = AgentTask(
        description="d",
        prompt="p",
        agent="a",
        issue_key="RACE-1",
    )
    processor._begin_workflow_run(
        state_manager.get_state("RACE-1"),
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="a",
        job_status="executing",
    )
    final = state_manager.get_state("RACE-1")
    assert final is not None
    assert final.status == TaskStatus.CANCELLED, (
        f"cancel must stick; got {final.status.value} "
        "(non-CAS begin overwrote CANCELLED)"
    )


@pytest.mark.asyncio
async def test_c4_cancel_then_begin_under_lock_stays_cancelled(processor, state_manager):
    """End-to-end of the race: cancel while lock held, then begin continues."""
    state_manager.create_state("RACE-2", "s", "d")  # PENDING
    lock = processor._get_issue_lock("RACE-2")
    await lock.acquire()
    try:
        out = await processor.cancel_job("RACE-2")
        assert out.get("ok") is True
        assert state_manager.get_state("RACE-2").status == TaskStatus.CANCELLED

        from src.orchestrator.agent_runner import AgentTask

        task = AgentTask(
            description="d", prompt="p", agent="a", issue_key="RACE-2"
        )
        job_id = processor._begin_workflow_run(
            state_manager.get_state("RACE-2"),
            status=TaskStatus.EXECUTING,
            task=task,
            workflow_type="execution",
            agent="a",
            job_status="executing",
        )
        assert job_id is None, "begin must refuse after cancel"
        assert state_manager.get_state("RACE-2").status == TaskStatus.CANCELLED
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# C4b — Cancel mid-clone must not re-arm context / start agent
# ---------------------------------------------------------------------------


def test_c4b_init_skips_when_already_cancelled(processor, state_manager):
    """Already CANCELLED: no clone, no context."""
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state_manager.create_state("CLONE-ABORT-0", "s", desc)
    state_manager.update_state(
        "CLONE-ABORT-0",
        status=TaskStatus.CANCELLED,
        error_message="Cancelled from dashboard",
        completed_at=datetime.now(),
    )
    with patch("src.processor.GitManager") as GM:
        out = processor._init_git_manager("CLONE-ABORT-0")
        assert out is None
        GM.assert_not_called()
        assert "CLONE-ABORT-0" not in processor._contexts


def test_c4b_init_git_manager_discards_workspace_when_cancelled(
    processor, state_manager, tmp_path
):
    """Cancel during GitManager construct: cleanup + no _contexts."""
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state_manager.create_state("CLONE-ABORT-1", "s", desc)
    state_manager.update_state("CLONE-ABORT-1", status=TaskStatus.EXECUTING)

    fake_git = MagicMock()
    fake_git.get_working_directory.return_value = tmp_path
    fake_git.cleanup = MagicMock(return_value=True)

    def construct_and_cancel(*_a, **_k):
        state_manager.update_state(
            "CLONE-ABORT-1",
            status=TaskStatus.CANCELLED,
            error_message="Cancelled from dashboard",
            completed_at=datetime.now(),
        )
        return fake_git

    with patch("src.processor.GitManager", side_effect=construct_and_cancel):
        with patch("src.processor.AgentRunner") as AR:
            out = processor._init_git_manager("CLONE-ABORT-1")
            assert out is None
            AR.assert_not_called()
            fake_git.cleanup.assert_called_once()
            assert "CLONE-ABORT-1" not in processor._contexts


def test_c4b_prepare_blocking_returns_none_when_aborted_after_init(
    processor, state_manager
):
    """If cancel lands after context register, prep must release and return None."""
    desc = (
        "{params}\n"
        "Repository: https://gitlab.example.com/group/repo.git\n"
        "Source branch: develop\n"
        "Target branch: main\n"
        "Mode: build\n"
        "{params}\n"
    )
    state = state_manager.create_state("CLONE-ABORT-2", "s", desc)
    state_manager.update_state("CLONE-ABORT-2", status=TaskStatus.EXECUTING)

    git = MagicMock()
    git.target_branch = "main"
    git.ensure_feature_branch.return_value = "feature/CLONE-ABORT-2"
    git.cleanup = MagicMock(return_value=True)

    def init_then_cancel(issue_key, st=None):
        # Simulate cancel winning while clone/setup was in flight
        state_manager.update_state(
            issue_key,
            status=TaskStatus.CANCELLED,
            error_message="Cancelled from dashboard",
            completed_at=datetime.now(),
        )
        processor._contexts[issue_key] = {"git": git, "runner": MagicMock()}
        return git

    with patch.object(processor, "_init_git_manager", side_effect=init_then_cancel):
        out = processor._prepare_git_workspace_blocking(state)

    assert out is None
    assert "CLONE-ABORT-2" not in processor._contexts
    assert state_manager.get_state("CLONE-ABORT-2").status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# C7 — git push / glab must hard-timeout
# ---------------------------------------------------------------------------


def test_c7_run_git_applies_timeout():
    """_run_git must pass timeout= to subprocess.run (no hang forever)."""
    from pathlib import Path
    import tempfile

    from src.git_manager import GitManager

    with tempfile.TemporaryDirectory() as td:
        gm = GitManager.__new__(GitManager)
        gm.temp_dir = Path(td)
        gm.remote_url = "https://gitlab.example.com/g/r.git"
        gm.remote_enabled = True
        gm._pat_for_remote = MagicMock(return_value=None)
        gm._redact_git_args = lambda args: args
        gm._redact_secret_text = lambda t: t
        gm._git_auth_env = MagicMock(return_value=None)

        with patch("src.git_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("src.git_manager.settings") as s:
                s.git_command_timeout_seconds = 42
                gm._run_git(["status"], check=False)
            assert run.called
            kwargs = run.call_args.kwargs
            assert kwargs.get("timeout") == 42


def test_c7_run_git_timeout_raises_when_check_true():
    from pathlib import Path
    import subprocess
    import tempfile

    from src.git_manager import GitManager

    with tempfile.TemporaryDirectory() as td:
        gm = GitManager.__new__(GitManager)
        gm.temp_dir = Path(td)
        gm.remote_url = ""
        gm._pat_for_remote = MagicMock(return_value=None)
        gm._redact_git_args = lambda args: args
        gm._redact_secret_text = lambda t: t
        gm._git_auth_env = MagicMock(return_value=None)

        with patch(
            "src.git_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            with patch("src.git_manager.settings") as s:
                s.git_command_timeout_seconds = 30
                with pytest.raises(RuntimeError, match="timed out"):
                    gm._run_git(["push", "-u", "origin", "feature/x"], check=True)


def test_c7_run_glab_applies_timeout():
    from pathlib import Path
    import tempfile

    from src.git_manager import GitManager

    with tempfile.TemporaryDirectory() as td:
        gm = GitManager.__new__(GitManager)
        gm.temp_dir = Path(td)
        gm.remote_url = "https://gitlab.example.com/g/r.git"
        gm._glab_env = MagicMock(return_value={})

        with patch("src.git_manager.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("src.git_manager.settings") as s:
                s.git_command_timeout_seconds = 99
                gm._run_glab(["mr", "create", "--fill"], check=False)
            assert run.call_args.kwargs.get("timeout") == 99


# ---------------------------------------------------------------------------
# C5 — Git option smuggling via Source branch starting with '-'
# ---------------------------------------------------------------------------


def test_c5_branch_names_starting_with_dash_must_be_rejected():
    """
    _looks_like_branch allows leading '-' (regex [A-Za-z0-9._/\\-]+).
    Source branch '--mirror' is accepted and later becomes:
      git push -u origin --mirror
    which is a push *option*, not a ref.

    Expected: reject branch names that start with '-'.
    """
    from src.issue_git_spec import _looks_like_branch, parse_issue_git_spec

    assert _looks_like_branch("--mirror") is False
    assert _looks_like_branch("--force") is False
    assert _looks_like_branch("-u") is False
    # Still allow normal branches with hyphens mid-name
    assert _looks_like_branch("feature/ok-1.2") is True

    body = """{params}
Repository: https://gitlab.example.com/g/r.git
Source branch: --mirror
Target branch: develop
Mode: build
{params}"""
    spec, err = parse_issue_git_spec(description=body)
    assert err is not None or (
        spec is not None and not (spec.source_branch or "").startswith("-")
    ), (
        f"Source branch '--mirror' must not parse as a valid branch "
        f"(spec={spec!r}, err={err!r})"
    )


def test_c5_push_must_not_pass_option_like_branch_as_ref(tmp_path, monkeypatch):
    """Even if a bad name reaches push, it must refuse rather than smuggle options."""
    from src.git_manager import GitManager

    monkeypatch.chdir(tmp_path)
    gm = GitManager.__new__(GitManager)
    gm.remote_enabled = True
    gm.work_branch = "--mirror"
    gm.temp_dir = tmp_path
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    gm.gitlab_host = "gitlab.example.com"
    gm.gitlab_token = "pat"
    gm.issue_key = "INJ-1"
    gm.target_branch = "develop"
    gm.source_branch = "--mirror"

    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch.object(gm, "_run_git", side_effect=fake_run):
        with patch.object(gm, "_with_auth_remote"):
            with patch.object(gm, "_scrub_remote_credentials"):
                with patch.object(gm, "_pat_for_remote", return_value="pat"):
                    ok = gm.push("--mirror")

    # Correct behaviour: refuse (False) OR never pass bare --mirror as last ref arg
    push_cmds = [c for c in calls if c and c[0] == "push"]
    for cmd in push_cmds:
        # After 'origin', ref must not start with '-'
        if "origin" in cmd:
            idx = cmd.index("origin")
            if idx + 1 < len(cmd):
                assert not cmd[idx + 1].startswith("-"), (
                    f"push must not smuggle option as ref: {cmd}"
                )
    if ok is True and not push_cmds:
        pytest.fail("push returned True without a push command")
    if ok is True:
        # If push "succeeded", it must not have used option-like branch
        assert not any(
            c[-1].startswith("-") for c in push_cmds if c
        ), f"push accepted option-like branch: {push_cmds}"


# ---------------------------------------------------------------------------
# C6 — Delivery baseline fail-open when snapshot missing
# ---------------------------------------------------------------------------


def test_c6_assert_build_delivery_fails_closed_without_baseline(processor):
    """
    When job-start baseline is None (snapshot failed), a branch already ahead
    from a *previous* job must not count as delivery for this job.

    Currently: baseline None → skip noop check → ahead>=1 → None (OK).
    Correct: refuse / require baseline when re-using an existing branch.
    """
    git = MagicMock()
    git.work_branch = "feature/BD-X"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = "bbb222ccc333"
    git.delivery_baseline_sha = None  # snapshot failed
    git.commits_ahead_of_target.return_value = 5  # prior job commits
    processor._contexts["BD-X"] = {"git": git, "runner": None}

    # Also clear any state metadata baseline
    processor.state_manager.create_state("BD-X", "s", "d")
    processor.state_manager.update_state("BD-X", status=TaskStatus.EXECUTING)

    err = processor._assert_build_delivery("BD-X")
    assert err is not None, (
        "Missing delivery baseline must not treat prior commits as this job's delivery "
        f"(got err={err!r})"
    )


# ---------------------------------------------------------------------------
# C7 — Agent timeout hole: process.wait() after stream EOF unguarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c7_run_agent_timeout_covers_process_wait_after_eof(tmp_path, monkeypatch):
    """
    Wall-clock timeout must cover process.wait() after stdout/stderr EOF.
    Hung OpenCode child that closed pipes must still time out.
    """
    from src.orchestrator.agent_runner import AgentRunner, AgentTask
    import asyncio

    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(working_directory=tmp_path)

    class EofButAlive:
        def __init__(self):
            self.returncode = None
            self.pid = 4242
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._killed = False

        async def wait(self):
            # Hang until killed
            while self.returncode is None:
                await asyncio.sleep(0.05)
            return self.returncode

        def kill(self):
            self.returncode = -9
            self._killed = True

        def terminate(self):
            self.returncode = -15
            self._killed = True

    proc = EofButAlive()

    with patch("src.orchestrator.agent_runner.settings") as s:
        s.opencode_cli = "opencode"
        s.default_model = "m"
        s.agent_task_timeout_seconds = 1
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await asyncio.wait_for(
                runner.run_agent(task, timeout_seconds=0.3),
                timeout=5.0,
            )

    assert result.get("timed_out") is True, (
        f"agent that hangs after EOF must time out; got {result!r}"
    )
    assert result.get("returncode") == -1


# ---------------------------------------------------------------------------
# Sanity: previously documented logical issues that may already be fixed
# ---------------------------------------------------------------------------


def test_sanity_max_retries_zero_means_one_attempt():
    """Regression: max_retries=0 must not fall through to settings via `or`."""
    from src.orchestrator.agent_runner import AgentRunner, AgentTask
    import asyncio

    runner = AgentRunner()
    calls = {"n": 0}

    async def fail(*a, **k):
        calls["n"] += 1
        return {
            "task_id": "t",
            "returncode": 1,
            "stdout": "",
            "stderr": "err",
            "session_file": None,
            "opencode_session_id": None,
            "progress": 0,
        }

    async def run():
        with patch.object(runner, "run_agent", side_effect=fail):
            with patch("src.orchestrator.agent_runner.settings") as s:
                s.agent_task_max_retries = 5
                s.agent_task_retry_delay_seconds = 0.01
                s.agent_task_retry_backoff_multiplier = 1.0
                s.agent_task_retry_on_timeout = True
                s.agent_task_retry_on_error = True
                task = AgentTask(description="d", prompt="p", agent="a")
                await runner.run_agent_with_retry(task, max_retries=0)

    asyncio.run(run())
    assert calls["n"] == 1


def test_sanity_create_issue_uses_fields_wrapper():
    """Regression: Jira REST v2 requires {\"fields\": {...}}."""
    from src.jira.client import JiraClient

    with patch("src.jira.client.httpx.Client") as C:
        http = MagicMock()
        C.return_value = http
        with patch("src.jira.client.settings") as s:
            s.jira_host = "https://jira.example.com"
            s.jira_api_token = "t"
            s.jira_email = ""
            c = JiraClient()
            c.client = http
            c.resolve_issuetype_ref = MagicMock(return_value={"name": "Task"})
            resp = MagicMock()
            resp.status_code = 201
            resp.json.return_value = {"key": "P-1"}
            resp.text = ""
            resp.raise_for_status = MagicMock()
            http.post.return_value = resp
            c.create_issue("PROJ", "sum", "desc")
            payload = http.post.call_args.kwargs.get("json") or {}
            assert "fields" in payload
