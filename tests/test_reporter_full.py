"""Full branch coverage for JiraReporter."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.reporter.jira_reporter import JiraReporter
from src.state.models import JiraAgentState, TaskStatus
from tests.conftest import FakeJiraClient


@pytest.fixture
def state():
    return JiraAgentState(
        issue_key="R-1",
        issue_summary="sum",
        description="d",
        status=TaskStatus.EXECUTING,
        metadata={"workflow_type": "direct"},
        progress_percentage=50,
        retry_count=1,
        max_retries=3,
        estimated_cost=0.12,
        execution_duration_seconds=10.5,
        token_usage_input=100,
        token_usage_output=50,
        completed_at=datetime(2026, 1, 1, 12, 0, 0),
        current_opencode_session_id="ses_r1",
    )


def test_init_with_client():
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    assert r.client is client


def test_init_simulated_flag():
    with patch("src.reporter.jira_reporter.create_jira_client") as factory:
        factory.return_value = FakeJiraClient()
        with patch("src.reporter.jira_reporter.settings") as s:
            s.is_configured.return_value = True
            s.jira_host = "https://jira.example.com"
            JiraReporter(simulated=True)
            factory.assert_called_with(simulated=True)


def test_init_auto_simulated_placeholder_host():
    with patch("src.reporter.jira_reporter.create_jira_client") as factory:
        factory.return_value = FakeJiraClient()
        with patch("src.reporter.jira_reporter.settings") as s:
            s.is_configured.return_value = True
            s.jira_host = "https://yourcompany.atlassian.net"
            JiraReporter()
            factory.assert_called_with(simulated=True)


def test_post_initial_ack_success(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    cid = r.post_initial_acknowledgment(state)
    assert cid == "1"
    assert "AI Agent Activated" in client.comments[0]["body"]


def test_post_initial_ack_exception(state):
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("net")
    r = JiraReporter(client=client)
    assert r.post_initial_acknowledgment(state) is None


def test_post_plan_summary(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    plan = "\n".join(f"line {i}" for i in range(30))
    state.plan_path = "/plans/R-1.md"
    cid = r.post_plan_summary(state, plan)
    assert cid is not None
    assert client.updated  # labels update


def test_post_plan_summary_empty_lines(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    cid = r.post_plan_summary(state, "\n\n\n")
    assert cid is not None


def test_post_progress_with_and_without_pct(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    assert r.post_progress_update(state, "working", progress_percentage=50) is not None
    assert "50%" in client.comments[-1]["body"]
    assert r.post_progress_update(state, "working") is not None


def test_post_completion_with_changes_and_cost(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    cid = r.post_completion(state, "done", changes_made=["a", "b"])
    assert cid is not None
    body = client.comments[-1]["body"]
    assert "Changes Made" in body
    assert "Cost Summary" in body
    assert "ses_r1" in body


def test_post_completion_zero_cost(state):
    state.estimated_cost = 0
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    body_id = r.post_completion(state, "done")
    assert body_id is not None
    assert "Cost Summary" not in client.comments[-1]["body"]


def test_post_completion_no_completed_at(state):
    state.completed_at = None
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    r.post_completion(state, "done")
    assert "N/A" in client.comments[-1]["body"]


def test_post_error_with_suggestion(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    r.post_error(state, "fail", suggestion="try again")
    assert "Suggestion" in client.comments[-1]["body"]


def test_post_error_not_timed_out_not_exhausted(state):
    state.timed_out = False
    state.retry_count = 0
    state.max_retries = 3
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    r.post_error(state, "fail")
    body = client.comments[-1]["body"]
    assert "Timed out" not in body
    assert "Retries exhausted" not in body


def test_post_comment_response():
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    assert r.post_comment_response("R-1", "hello") is not None


def test_post_code_review_truncates(state):
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    long_review = "x" * 4000
    cid = r.post_code_review(state, long_review, "model-free")
    assert cid is not None
    assert "truncated" in client.comments[-1]["body"]
    assert any(u.get("labels") == ["ai-reviewed"] for u in client.updated)


def test_post_code_review_short_and_label_fail(state):
    client = MagicMock()
    client.add_comment.return_value = {"id": "9"}
    client.update_issue.side_effect = RuntimeError("label fail")
    r = JiraReporter(client=client)
    assert r.post_code_review(state, "short", "m") == "9"


def test_post_oracle_response():
    client = FakeJiraClient()
    r = JiraReporter(client=client)
    assert r.post_oracle_response("R-1", "q?", "a!") is not None


def test_update_issue_status_and_attach():
    client = MagicMock()
    client.transition_issue.return_value = True
    client.add_attachment.return_value = {"id": "1"}
    r = JiraReporter(client=client)
    assert r.update_issue_status("R-1", "Done") is True
    assert r.attach_file("R-1", "/tmp/f.txt", "f.txt") is True
    client.add_attachment.return_value = None
    assert r.attach_file("R-1", "/tmp/f.txt") is False


def test_post_completion_add_comment_raises(state):
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("down")
    r = JiraReporter(client=client)
    assert r.post_completion(state, "x") is None


def test_post_error_add_comment_raises(state):
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("down")
    r = JiraReporter(client=client)
    assert r.post_error(state, "x") is None


def test_post_progress_client_returns_none(state):
    client = MagicMock()
    client.add_comment.return_value = None
    r = JiraReporter(client=client)
    assert r.post_progress_update(state, "m") is None
