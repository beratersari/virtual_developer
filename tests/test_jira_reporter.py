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


def test_post_error_clarifying_question_category(reporter, fake_jira):
    """Question stops must not look like compaction budget failures."""
    state = JiraAgentState(
        issue_key="PROJ-Q",
        issue_summary="Needs decisions",
        status=TaskStatus.ERROR,
        current_opencode_session_id="ses_q1",
    )
    comment_id = reporter.post_error(
        state,
        "assistant asked a clarifying question",
        suggestion="Add constraints to the description, then re-queue from To Do.",
        category="question",
    )
    assert comment_id is not None
    body = fake_jira.comments[-1]["body"]
    assert "Clarifying question" in body
    assert "unattended" in body.lower()
    assert "compaction" not in body.lower()
    assert "ses_q1" in body


def test_post_error_thread_lock_category(reporter, fake_jira):
    """Codex writer lock must not look like an OpenCode incomplete session."""
    state = JiraAgentState(
        issue_key="KAN-12371",
        issue_summary="Django long job",
        status=TaskStatus.ERROR,
        current_opencode_session_id="01a03397-15ff-7941-a5e7-17e23b3d7b82",
    )
    comment_id = reporter.post_error(
        state,
        "already has an active writer",
        category="thread_lock",
    )
    assert comment_id is not None
    body = fake_jira.comments[-1]["body"]
    assert "Codex thread locked" in body
    assert "active writer" in body.lower()
    assert "Incomplete session" not in body


def test_post_completion_none_state_returns_none(reporter):
    assert reporter.post_completion(None, "x") is None
