"""Full coverage for state models serialization/deserialization."""

from datetime import datetime

from src.state.models import JiraAgentState, RetryAttempt, TaskStatus


def test_retry_attempt_to_from_dict_roundtrip():
    ts = datetime(2026, 1, 2, 3, 4, 5)
    r = RetryAttempt(
        attempt_number=1,
        timestamp=ts,
        reason="timeout",
        delay_seconds=2.5,
        session_log_path="/tmp/a.log",
        error_message="boom",
        return_code=-1,
        opencode_session_id="ses_1",
    )
    d = r.to_dict()
    assert d["timestamp"] == ts.isoformat()
    r2 = RetryAttempt.from_dict(d)
    assert r2.attempt_number == 1
    assert r2.timestamp == ts
    assert r2.opencode_session_id == "ses_1"


def test_retry_attempt_from_dict_missing_timestamp():
    r = RetryAttempt.from_dict({"attempt_number": 2, "reason": "err"})
    assert r.timestamp is None
    assert r.attempt_number == 2
    assert r.delay_seconds == 0.0


def test_retry_attempt_to_dict_none_timestamp():
    r = RetryAttempt(0, None, "x", 0.0)  # type: ignore[arg-type]
    d = r.to_dict()
    assert d["timestamp"] is None


def test_jira_agent_state_roundtrip_full():
    ts = datetime(2026, 2, 1, 0, 0, 0)
    attempt = RetryAttempt(1, ts, "timeout", 1.0)
    state = JiraAgentState(
        issue_key="K-1",
        issue_summary="s",
        description="d",
        status=TaskStatus.EXECUTING,
        progress_percentage=40,
        metadata={"workflow_type": "execution"},
        plan_path="/p.md",
        started_at=ts,
        completed_at=ts,
        execution_duration_seconds=12.5,
        current_task_id="task_1",
        current_opencode_session_id="ses_x",
        error_message=None,
        retry_count=1,
        max_retries=3,
        last_retry_at=ts,
        timed_out=True,
        timeout_seconds=100,
        retry_history=[attempt],
        token_usage_input=10,
        token_usage_output=20,
        estimated_cost=0.01,
        jira_assignee="bot",
        triggered_by="poller",
    )
    d = state.to_dict()
    s2 = JiraAgentState.from_dict(d)
    assert s2.issue_key == "K-1"
    assert s2.status == TaskStatus.EXECUTING
    assert s2.timed_out is True
    assert len(s2.retry_history) == 1
    assert s2.retry_history[0].reason == "timeout"


def test_from_dict_minimal_defaults():
    s = JiraAgentState.from_dict({"issue_key": "Z-1"})
    assert s.issue_summary == ""
    assert s.status == TaskStatus.PENDING
    assert s.retry_history == []
    assert s.started_at is None
    assert s.completed_at is None
    assert s.last_retry_at is None


def test_add_retry_attempt_updates_counts():
    s = JiraAgentState(issue_key="Z-2", issue_summary="s")
    ts = datetime.now()
    s.add_retry_attempt(RetryAttempt(1, ts, "err", 1.0))
    assert s.retry_count == 1
    assert s.last_retry_at == ts
    s.add_retry_attempt(RetryAttempt(2, ts, "timeout", 2.0))
    assert s.retry_count == 2
    assert len(s.retry_history) == 2


def test_all_task_status_values():
    for st in TaskStatus:
        s = JiraAgentState(issue_key="T-1", issue_summary="x", status=st)
        assert JiraAgentState.from_dict(s.to_dict()).status == st
