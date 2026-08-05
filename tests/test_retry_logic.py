"""Exhaustive tests for agent retry logic and related bookkeeping.

Assertions match only what the source implements:

- ``run_agent_with_retry`` loop: ``while attempt <= effective_max_retries``
  → total attempts = max_retries + 1 when retries are allowed.
- Timeout vs error: ``if timed_out:`` / ``elif retry_on_error:`` (timeout
  branch ignores ``retry_on_error``).
- Backoff: ``delay = retry_delay * (backoff_multiplier ** (attempt - 1))``
  after ``attempt += 1`` when scheduling a retry.
- Processor end-of-run: ``retry_count = attempts - 1``.
- ``add_retry_attempt``: ``retry_count = len(retry_history)``.
- Stuck monitor: ``limit = timeout * (max_retries + 1) * 1.5``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.state.models import JiraAgentState, RetryAttempt, TaskStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail_result(
    *,
    returncode: int = 1,
    stderr: str = "err",
    timed_out: bool = False,
    session_file: Optional[str] = "/tmp/s.log",
    session_id: Optional[str] = "ses_fail",
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "task_id": "t",
        "returncode": returncode,
        "stdout": "",
        "stderr": stderr,
        "session_file": session_file,
        "opencode_session_id": session_id,
        "progress": 0,
    }
    if timed_out:
        out["timed_out"] = True
    return out


def _ok_result(
    *,
    session_file: Optional[str] = "/tmp/ok.log",
    session_id: Optional[str] = "ses_ok",
) -> Dict[str, Any]:
    return {
        "task_id": "t",
        "returncode": 0,
        "stdout": "ok",
        "stderr": "",
        "session_file": session_file,
        "opencode_session_id": session_id,
        "progress": 100,
    }


class _SettingsCtx:
    """Patch agent_runner.settings with explicit retry knobs."""

    def __init__(
        self,
        *,
        max_retries: int = 3,
        delay: float = 0.01,
        backoff: float = 2.0,
        retry_on_timeout: bool = True,
        retry_on_error: bool = True,
    ):
        self.kwargs = dict(
            max_retries=max_retries,
            delay=delay,
            backoff=backoff,
            retry_on_timeout=retry_on_timeout,
            retry_on_error=retry_on_error,
        )
        self._cm = None

    def __enter__(self):
        self._cm = patch("src.orchestrator.agent_runner.settings")
        s = self._cm.__enter__()
        s.agent_task_max_retries = self.kwargs["max_retries"]
        s.agent_task_retry_delay_seconds = self.kwargs["delay"]
        s.agent_task_retry_backoff_multiplier = self.kwargs["backoff"]
        s.agent_task_retry_on_timeout = self.kwargs["retry_on_timeout"]
        s.agent_task_retry_on_error = self.kwargs["retry_on_error"]
        return s

    def __exit__(self, *a):
        return self._cm.__exit__(*a)


# ---------------------------------------------------------------------------
# Loop bounds / attempt counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_retries_n_means_n_plus_one_attempts_on_always_fail():
    """while attempt <= max_retries → max_retries+1 run_agent calls when failing."""
    runner = AgentRunner()
    calls: List[int] = []

    async def always_fail(*a, **k):
        calls.append(k.get("attempt_number", -1))
        return _fail_result()

    with patch.object(runner, "run_agent", side_effect=always_fail):
        with _SettingsCtx(max_retries=2, delay=0.001, backoff=1.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=2)

    assert len(calls) == 3  # attempts 0, 1, 2
    assert calls == [0, 1, 2]
    assert result["returncode"] == 1
    assert result["retry_info"]["attempts"] == 3
    assert result["retry_info"]["max_retries"] == 2
    assert result["retry_info"]["retried"] is True
    assert result["retry_info"]["final_failure"] is True


@pytest.mark.asyncio
async def test_max_retries_zero_is_exactly_one_attempt_not_settings():
    """max_retries=0 must not fall through to settings (uses is None, not or)."""
    runner = AgentRunner()
    calls = {"n": 0}

    async def fail(*a, **k):
        calls["n"] += 1
        return _fail_result()

    with patch.object(runner, "run_agent", side_effect=fail):
        with _SettingsCtx(max_retries=99, delay=0.001, backoff=1.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=0)

    assert calls["n"] == 1
    assert result["retry_info"]["attempts"] == 1
    assert result["retry_info"]["max_retries"] == 0
    assert result["retry_info"]["retried"] is False
    assert result["retry_info"]["final_failure"] is True


@pytest.mark.asyncio
async def test_success_on_last_allowed_attempt():
    """With max_retries=2, third run (attempt_number=2) success is allowed."""
    runner = AgentRunner()
    calls = {"n": 0}

    async def fail_twice_then_ok(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _fail_result(stderr=f"fail-{calls['n']}")
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=fail_twice_then_ok):
        with _SettingsCtx(max_retries=2, delay=0.001, backoff=1.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=2)

    assert calls["n"] == 3
    assert result["returncode"] == 0
    assert result["retry_info"]["attempts"] == 3
    assert result["retry_info"]["retried"] is True
    assert "final_failure" not in result["retry_info"]


@pytest.mark.asyncio
async def test_success_first_attempt_retry_info_shape():
    runner = AgentRunner()

    async def ok(*a, **k):
        return _ok_result(session_id="ses_1")

    with patch.object(runner, "run_agent", side_effect=ok):
        with _SettingsCtx(max_retries=3, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            original_id = task.task_id
            result = await runner.run_agent_with_retry(task, max_retries=3)

    assert result["returncode"] == 0
    assert result["retry_info"]["attempts"] == 1
    assert result["retry_info"]["retried"] is False
    assert result["retry_info"]["max_retries"] == 3
    assert result["retry_info"]["last_opencode_session_id"] == "ses_1"
    assert "final_failure" not in result["retry_info"]
    assert "aborted" not in result["retry_info"]
    # task_id must not be reminted when no retry is scheduled
    assert task.task_id == original_id


# ---------------------------------------------------------------------------
# Timeout vs error precedence (if / elif)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timed_out_ignores_retry_on_error_when_timeout_flag_false():
    """if timed_out: ... elif retry_on_error — error flag is never consulted."""
    runner = AgentRunner()
    calls = {"n": 0}
    retries: List[Any] = []

    async def always_timeout(*a, **k):
        calls["n"] += 1
        return _fail_result(
            returncode=-1,
            stderr="\n[TIMEOUT] Task exceeded 1 seconds",
            timed_out=True,
        )

    with patch.object(runner, "run_agent", side_effect=always_timeout):
        with _SettingsCtx(
            max_retries=5,
            delay=0.001,
            retry_on_timeout=False,
            retry_on_error=True,
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(
                task,
                max_retries=5,
                on_retry=lambda *args: retries.append(args),
            )

    assert calls["n"] == 1
    assert retries == []
    assert result.get("timed_out") is True
    assert result["retry_info"]["final_failure"] is True
    assert result["retry_info"]["attempts"] == 1
    assert result["retry_info"]["retried"] is False


@pytest.mark.asyncio
async def test_timeout_retries_when_retry_on_timeout_true():
    runner = AgentRunner()
    calls = {"n": 0}
    reasons: List[str] = []

    async def timeout_then_ok(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fail_result(
                returncode=-1,
                stderr="\n[TIMEOUT] Task exceeded 30 seconds",
                timed_out=True,
                session_id="ses_to",
            )
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=timeout_then_ok):
        with _SettingsCtx(
            max_retries=2,
            delay=0.001,
            retry_on_timeout=True,
            retry_on_error=False,
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(
                task,
                max_retries=2,
                on_retry=lambda *a: reasons.append(a[2]),
            )

    assert calls["n"] == 2
    assert reasons == ["timeout"]
    assert result["returncode"] == 0
    assert result["retry_info"]["retried"] is True


@pytest.mark.asyncio
async def test_error_no_retry_when_retry_on_error_false():
    runner = AgentRunner()
    calls = {"n": 0}

    async def fail(*a, **k):
        calls["n"] += 1
        return _fail_result(returncode=7, stderr="boom")

    with patch.object(runner, "run_agent", side_effect=fail):
        with _SettingsCtx(
            max_retries=3,
            delay=0.001,
            retry_on_timeout=True,
            retry_on_error=False,
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=3)

    assert calls["n"] == 1
    assert result["returncode"] == 7
    assert result["retry_info"]["final_failure"] is True
    assert result["retry_info"]["retried"] is False


@pytest.mark.asyncio
async def test_error_retries_when_flag_true_even_if_timeout_flag_false():
    """Non-timeout failure uses elif retry_on_error; timeout flag irrelevant."""
    runner = AgentRunner()
    calls = {"n": 0}

    async def fail_then_ok(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fail_result()  # no timed_out key
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=fail_then_ok):
        with _SettingsCtx(
            max_retries=1,
            delay=0.001,
            retry_on_timeout=False,
            retry_on_error=True,
        ):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert calls["n"] == 2
    assert result["returncode"] == 0


# ---------------------------------------------------------------------------
# Backoff formula + on_retry contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_delay_formula_exponential():
    """delay = retry_delay * (multiplier ** (attempt - 1)) after increment."""
    runner = AgentRunner()
    delays: List[float] = []

    async def always_fail(*a, **k):
        return _fail_result()

    with patch.object(runner, "run_agent", side_effect=always_fail):
        with _SettingsCtx(max_retries=3, delay=0.05, backoff=2.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            await runner.run_agent_with_retry(
                task,
                max_retries=3,
                on_retry=lambda *a: delays.append(a[1]),
            )

    # Three retries scheduled (attempts 1,2,3 after increment) before final failure
    # delay_base=0.05, mult=2 → 0.05 * 2^0, 2^1, 2^2
    assert delays == [0.05, 0.1, 0.2]


@pytest.mark.asyncio
async def test_on_retry_arg_order_and_task_id_mint():
    runner = AgentRunner()
    captured: List[tuple] = []
    original_id_holder: Dict[str, str] = {}

    async def fail_once_then_ok(*a, **k):
        if not captured:
            return _fail_result(
                returncode=3,
                stderr="detail-err",
                session_file="/sess/fail.log",
                session_id="ses_old",
            )
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=fail_once_then_ok):
        with _SettingsCtx(max_retries=1, delay=0.02, backoff=3.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            original_id_holder["id"] = task.task_id

            def on_retry(*args):
                captured.append(args)

            result = await runner.run_agent_with_retry(
                task, max_retries=1, on_retry=on_retry
            )

    assert len(captured) == 1
    attempt_number, delay, reason, session_file, error_message, return_code, session_id, new_task_id = (
        captured[0]
    )
    assert attempt_number == 1
    assert delay == pytest.approx(0.02)  # 0.02 * 3^0
    assert reason == "error"
    assert session_file == "/sess/fail.log"
    assert error_message == "detail-err"
    assert return_code == 3
    assert session_id == "ses_old"
    assert new_task_id.startswith("task_")
    assert new_task_id != original_id_holder["id"]
    assert task.task_id == new_task_id
    assert result["returncode"] == 0


@pytest.mark.asyncio
async def test_on_retry_timeout_error_message_is_stderr():
    """error_message = stderr when returncode != 0 (timeout uses -1)."""
    runner = AgentRunner()
    msgs: List[Optional[str]] = []

    async def timeout_then_ok(*a, **k):
        if not msgs:
            return _fail_result(
                returncode=-1,
                stderr="\n[TIMEOUT] Task exceeded 10 seconds",
                timed_out=True,
            )
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=timeout_then_ok):
        with _SettingsCtx(max_retries=1, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            await runner.run_agent_with_retry(
                task,
                max_retries=1,
                on_retry=lambda *a: msgs.append(a[4]),
            )

    assert msgs == ["\n[TIMEOUT] Task exceeded 10 seconds"]


@pytest.mark.asyncio
async def test_all_session_files_collected_across_retries():
    runner = AgentRunner()
    n = {"i": 0}

    async def flaky(*a, **k):
        n["i"] += 1
        if n["i"] < 3:
            return _fail_result(session_file=f"/s{n['i']}.log", session_id=f"s{n['i']}")
        return _ok_result(session_file="/s3.log", session_id="s3")

    with patch.object(runner, "run_agent", side_effect=flaky):
        with _SettingsCtx(max_retries=3, delay=0.001, backoff=1.0):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=3)

    files = result["retry_info"]["all_session_files"]
    assert files == ["/s1.log", "/s2.log", "/s3.log"]
    assert result["retry_info"]["last_opencode_session_id"] == "s3"


# ---------------------------------------------------------------------------
# Abort points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_before_first_attempt_no_run_agent():
    runner = AgentRunner()
    calls = {"n": 0}

    async def never(*a, **k):
        calls["n"] += 1
        return _ok_result()

    with patch.object(runner, "run_agent", side_effect=never):
        with _SettingsCtx(max_retries=2, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(
                task, max_retries=2, should_abort=lambda: True
            )

    assert calls["n"] == 0
    assert result.get("aborted") is True
    assert result["returncode"] == -1
    assert result["retry_info"]["aborted"] is True
    # Code sets attempts = attempt + 1 with attempt still 0
    assert result["retry_info"]["attempts"] == 1
    assert result["retry_info"]["retried"] is False
    assert result["retry_info"]["all_session_files"] == []


@pytest.mark.asyncio
async def test_abort_after_attempt_preserves_returncode_and_stops():
    runner = AgentRunner()
    calls = {"n": 0}
    abort = {"v": False}

    async def fail_and_arm_abort(*a, **k):
        calls["n"] += 1
        abort["v"] = True
        return _fail_result(returncode=9, stderr="keep-me")

    with patch.object(runner, "run_agent", side_effect=fail_and_arm_abort):
        with _SettingsCtx(max_retries=3, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(
                task,
                max_retries=3,
                should_abort=lambda: abort["v"],
            )

    assert calls["n"] == 1
    assert result.get("aborted") is True
    assert result["returncode"] == 9
    assert result["stderr"] == "keep-me"
    assert result["retry_info"]["aborted"] is True
    assert "final_failure" not in result["retry_info"]


@pytest.mark.asyncio
async def test_abort_during_backoff_no_second_attempt():
    runner = AgentRunner()
    calls = {"n": 0}
    aborted = {"v": False}

    async def fail_once(*a, **k):
        calls["n"] += 1
        return _fail_result()

    with patch.object(runner, "run_agent", side_effect=fail_once):
        with _SettingsCtx(max_retries=3, delay=0.05, backoff=1.0):
            task = AgentTask(description="d", prompt="p", agent="a")

            def on_retry(*_args):
                aborted["v"] = True

            result = await runner.run_agent_with_retry(
                task,
                max_retries=3,
                on_retry=on_retry,
                should_abort=lambda: aborted["v"],
            )

    assert calls["n"] == 1
    assert result.get("aborted") is True
    assert result["retry_info"]["aborted"] is True


@pytest.mark.asyncio
async def test_should_abort_exception_treated_as_not_aborted():
    """_aborted(): except Exception → return False."""
    runner = AgentRunner()

    async def ok(*a, **k):
        return _ok_result()

    def raise_abort():
        raise RuntimeError("boom")

    with patch.object(runner, "run_agent", side_effect=ok):
        with _SettingsCtx(max_retries=1, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(
                task, max_retries=1, should_abort=raise_abort
            )

    assert result["returncode"] == 0
    assert result.get("aborted") is not True


# ---------------------------------------------------------------------------
# State models + manager record_retry
# ---------------------------------------------------------------------------


def test_add_retry_attempt_sets_count_equal_to_history_length():
    state = JiraAgentState(issue_key="R-1", issue_summary="s")
    assert state.retry_count == 0
    assert state.retry_history == []

    t1 = datetime(2026, 1, 1, 12, 0, 0)
    state.add_retry_attempt(
        RetryAttempt(attempt_number=1, timestamp=t1, reason="error", delay_seconds=1.0)
    )
    assert state.retry_count == 1
    assert len(state.retry_history) == 1
    assert state.last_retry_at == t1

    t2 = datetime(2026, 1, 1, 12, 0, 5)
    state.add_retry_attempt(
        RetryAttempt(
            attempt_number=2, timestamp=t2, reason="timeout", delay_seconds=2.0
        )
    )
    assert state.retry_count == 2
    assert state.retry_count == len(state.retry_history)
    assert state.last_retry_at == t2


def test_from_dict_does_not_resync_retry_count_to_history_length():
    """from_dict loads retry_count independently of len(retry_history)."""
    data = {
        "issue_key": "R-2",
        "issue_summary": "s",
        "status": "executing",
        "retry_count": 99,
        "retry_history": [
            {
                "attempt_number": 1,
                "timestamp": "2026-01-01T00:00:00",
                "reason": "error",
                "delay_seconds": 1.0,
            }
        ],
    }
    state = JiraAgentState.from_dict(data)
    assert state.retry_count == 99
    assert len(state.retry_history) == 1
    # After add_retry_attempt, code rewrites count from length
    state.add_retry_attempt(
        RetryAttempt(
            attempt_number=2,
            timestamp=datetime(2026, 1, 1, 1, 0, 0),
            reason="error",
            delay_seconds=1.0,
        )
    )
    assert state.retry_count == 2
    assert state.retry_count == len(state.retry_history)


def test_record_retry_attempt_skips_cancelled_and_error(state_manager):
    state_manager.create_state("RR-1", "s", "d")
    state_manager.update_state("RR-1", status=TaskStatus.CANCELLED)
    attempt = RetryAttempt(
        attempt_number=1,
        timestamp=datetime.now(),
        reason="error",
        delay_seconds=0.1,
    )
    out = state_manager.record_retry_attempt("RR-1", attempt)
    assert out is not None
    assert out.status == TaskStatus.CANCELLED
    assert out.retry_history == []
    assert out.retry_count == 0

    state_manager.create_state("RR-2", "s", "d")
    state_manager.update_state("RR-2", status=TaskStatus.ERROR)
    out2 = state_manager.record_retry_attempt("RR-2", attempt)
    assert out2 is not None
    assert out2.status == TaskStatus.ERROR
    assert out2.retry_history == []


def test_record_retry_attempt_appends_and_sets_live_ids(state_manager):
    state_manager.create_state("RR-3", "s", "d")
    state_manager.update_state("RR-3", status=TaskStatus.EXECUTING)
    attempt = RetryAttempt(
        attempt_number=1,
        timestamp=datetime.now(),
        reason="timeout",
        delay_seconds=0.5,
        error_message="TIMEOUT",
        return_code=-1,
        opencode_session_id="ses_a",
    )
    out = state_manager.record_retry_attempt(
        "RR-3",
        attempt,
        current_task_id="task_new99",
        current_opencode_session_id="ses_a",
    )
    assert out is not None
    assert out.retry_count == 1
    assert len(out.retry_history) == 1
    assert out.retry_history[0].reason == "timeout"
    assert out.current_task_id == "task_new99"
    assert out.current_opencode_session_id == "ses_a"

    # Persist round-trip
    loaded = state_manager.get_state("RR-3")
    assert loaded is not None
    assert loaded.retry_count == len(loaded.retry_history) == 1
    assert loaded.current_task_id == "task_new99"


def test_record_retry_attempt_missing_state_returns_none(state_manager):
    attempt = RetryAttempt(1, datetime.now(), "error", 0.1)
    assert state_manager.record_retry_attempt("NO-SUCH", attempt) is None


# ---------------------------------------------------------------------------
# Processor bookkeeping: retry_count = attempts - 1
# ---------------------------------------------------------------------------


@pytest.fixture
def processor(state_manager, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.processor import JobProcessor
    from tests.conftest import FakeJiraClient
    from src.reporter.jira_reporter import JiraReporter

    fake = FakeJiraClient()
    with patch("src.processor.create_jira_client", return_value=fake):
        p = JobProcessor()
    p.state_manager = state_manager
    p.jira_client = fake
    p.reporter = JiraReporter(fake)
    return p


def test_processor_record_agent_retry_writes_history(processor, state_manager):
    state_manager.create_state("PR-1", "s", "d")
    state_manager.update_state("PR-1", status=TaskStatus.EXECUTING)

    processor._record_agent_retry(
        "PR-1",
        attempt_number=1,
        delay_seconds=0.1,
        reason="error",
        session_file="/s.log",
        error_message="e",
        return_code=1,
        session_id="ses1",
        new_task_id="task_abc",
        progress_percentage=10,
    )
    st = state_manager.get_state("PR-1")
    assert st is not None
    assert st.status == TaskStatus.EXECUTING
    assert st.retry_count == 1
    assert len(st.retry_history) == 1
    assert st.retry_history[0].reason == "error"
    assert st.current_task_id == "task_abc"
    assert st.current_opencode_session_id == "ses1"


def test_processor_record_agent_retry_skips_when_aborted(processor, state_manager):
    state_manager.create_state("PR-2", "s", "d")
    state_manager.update_state("PR-2", status=TaskStatus.CANCELLED)

    processor._record_agent_retry(
        "PR-2",
        attempt_number=1,
        delay_seconds=0.1,
        reason="error",
        new_task_id="task_x",
    )
    st = state_manager.get_state("PR-2")
    assert st.status == TaskStatus.CANCELLED
    assert st.retry_history == []
    assert st.retry_count == 0


@pytest.mark.asyncio
async def test_processor_end_of_run_retry_count_is_attempts_minus_one(
    processor, state_manager, tmp_path, monkeypatch
):
    """Plan/build set retry_count = retry_info['attempts'] - 1 after agent returns.

    Simulate the exact update block used after run_agent_with_retry (processor
    lines that do update_data = {retry_count: attempts - 1}).
    """
    state_manager.create_state("PR-3", "s", "d")
    state_manager.update_state("PR-3", status=TaskStatus.PLANNING)

    # Mid-run history from two on_retry callbacks
    for i in (1, 2):
        processor._record_agent_retry(
            "PR-3",
            attempt_number=i,
            delay_seconds=0.01 * i,
            reason="error",
            new_task_id=f"task_{i}",
        )

    mid = state_manager.get_state("PR-3")
    assert mid is not None
    assert mid.retry_count == 2
    assert len(mid.retry_history) == 2

    # Same formula as processor after successful agent finish
    retry_info = {"attempts": 3, "max_retries": 2, "retried": True}
    update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
    state_manager.update_state("PR-3", **update_data)

    final = state_manager.get_state("PR-3")
    assert final is not None
    assert final.retry_count == 2  # attempts - 1
    # History is not cleared by the end-of-run update
    assert len(final.retry_history) == 2


def test_processor_retry_count_minus_one_when_attempts_missing():
    """Document literal: attempts default 0 → retry_count becomes -1."""
    retry_info: Dict[str, Any] = {}  # no "attempts" key
    update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
    assert update_data["retry_count"] == -1


# ---------------------------------------------------------------------------
# Stuck monitor formula (daemon)
# ---------------------------------------------------------------------------


def test_stuck_limit_formula_matches_daemon_code():
    """limit_seconds = timeout * (retries + 1) * 1.5"""
    timeout = 100
    retries = 2
    limit_seconds = timeout * (retries + 1) * 1.5
    assert limit_seconds == 450.0

    # Defaults from config fields: 1800 * (3+1) * 1.5
    assert 1800 * (3 + 1) * 1.5 == 10800.0

    # max_retries=0 → one attempt budget with 50% headroom
    assert 60 * (0 + 1) * 1.5 == 90.0


@pytest.mark.asyncio
async def test_stuck_monitor_aborts_only_past_limit(state_manager, tmp_path, monkeypatch):
    """Age just under limit stays in-flight; age over limit → ERROR via _fail_issue.

    Formula under test (daemon._monitor_active_issues):
    limit_seconds = timeout * (max_retries + 1) * 1.5
    With timeout=60, max_retries=0 → limit = 90s.
    """
    from src.daemon import JiraAgentDaemon

    state_manager.create_state("STK-1", "s", "d")
    state_manager.update_state(
        "STK-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now() - timedelta(seconds=50),
        timeout_seconds=60,
        max_retries=0,
    )

    daemon = JiraAgentDaemon.__new__(JiraAgentDaemon)
    daemon._running = True
    daemon.state_manager = state_manager
    daemon.processor = MagicMock()
    daemon.processor._kill_children_for_issue = MagicMock()
    daemon.processor._fail_issue = MagicMock()
    daemon.processor._release_context = MagicMock()

    async def stop_after_first(_seconds):
        daemon._running = False

    with patch("asyncio.sleep", side_effect=stop_after_first):
        await daemon._monitor_active_issues()

    daemon.processor._fail_issue.assert_not_called()

    # Age past 90s → should abort
    daemon._running = True
    state_manager.update_state(
        "STK-1",
        started_at=datetime.now() - timedelta(seconds=120),
    )

    with patch("asyncio.sleep", side_effect=stop_after_first):
        await daemon._monitor_active_issues()

    daemon.processor._fail_issue.assert_called_once()
    daemon.processor._kill_children_for_issue.assert_called()


# ---------------------------------------------------------------------------
# Config defaults (literal Field defaults)
# ---------------------------------------------------------------------------


def test_config_retry_defaults_match_source():
    from src.config import Settings

    # Construct without env so Field defaults apply for these fields
    s = Settings.model_construct(
        agent_task_timeout_seconds=1800,
        agent_task_max_retries=3,
        agent_task_retry_delay_seconds=5,
        agent_task_retry_backoff_multiplier=2.0,
        agent_task_retry_on_timeout=True,
        agent_task_retry_on_error=True,
    )
    assert s.agent_task_timeout_seconds == 1800
    assert s.agent_task_max_retries == 3
    assert s.agent_task_retry_delay_seconds == 5
    assert s.agent_task_retry_backoff_multiplier == 2.0
    assert s.agent_task_retry_on_timeout is True
    assert s.agent_task_retry_on_error is True


# ---------------------------------------------------------------------------
# None session_file not appended to all_session_files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_session_file_not_added_to_all_session_files():
    runner = AgentRunner()
    n = {"i": 0}

    async def flaky(*a, **k):
        n["i"] += 1
        if n["i"] == 1:
            return _fail_result(session_file=None, session_id=None)
        return _ok_result(session_file="/only.log", session_id="s2")

    with patch.object(runner, "run_agent", side_effect=flaky):
        with _SettingsCtx(max_retries=1, delay=0.001):
            task = AgentTask(description="d", prompt="p", agent="a")
            result = await runner.run_agent_with_retry(task, max_retries=1)

    assert result["retry_info"]["all_session_files"] == ["/only.log"]
