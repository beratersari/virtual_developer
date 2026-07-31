"""Unit tests for JiraReporter user-facing messages."""

from datetime import datetime

from src.state.models import JiraAgentState, TaskStatus


def test_post_completion_uses_opencode_session_id(reporter, fake_jira):
    """Regression: completion must show current_opencode_session_id, not missing field."""
    state = JiraAgentState(
        issue_key="PROJ-1",
        issue_summary="Done work",
        status=TaskStatus.COMPLETED,
        completed_at=datetime(2026, 1, 1, 12, 0, 0),
        current_opencode_session_id="ses_abc123",
        estimated_cost=0.0,
    )
    comment_id = reporter.post_completion(state, summary="All good")
    assert comment_id is not None
    body = fake_jira.comments[-1]["body"]
    assert "ses_abc123" in body
    assert "Work Completed" in body
    assert "token" not in body.lower()
    assert "Cost" not in body


def test_post_error_includes_timeout_and_retries(reporter, fake_jira):
    state = JiraAgentState(
        issue_key="PROJ-2",
        issue_summary="Failed",
        status=TaskStatus.ERROR,
        retry_count=3,
        max_retries=3,
        timed_out=True,
        timeout_seconds=1800,
        current_opencode_session_id="ses_err",
        error_message="boom",
    )
    comment_id = reporter.post_error(state, "agent timed out")
    assert comment_id is not None
    body = fake_jira.comments[-1]["body"]
    assert "Timed out" in body
    assert "Retries exhausted" in body
    assert "ses_err" in body
    assert "agent timed out" in body


def test_post_error_none_state_returns_none(reporter):
    assert reporter.post_error(None, "x") is None


def test_post_completion_none_state_returns_none(reporter):
    assert reporter.post_completion(None, "x") is None
