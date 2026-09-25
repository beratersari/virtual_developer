"""Edge-case coverage: Jira user-facing messages stay reasonable under all conditions."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from src.reporter.jira_reporter import JiraReporter, _clip
from src.state.models import JiraAgentState, TaskStatus
from tests.conftest import FakeJiraClient


@pytest.fixture
def client():
    return FakeJiraClient()


@pytest.fixture
def reporter(client):
    return JiraReporter(client=client)


def _state(**kwargs) -> JiraAgentState:
    base = dict(
        issue_key="MSG-1",
        issue_summary="Sample issue",
        status=TaskStatus.EXECUTING,
        metadata={"workflow_type": "execution", "backend": "claude"},
        progress_percentage=40,
        retry_count=0,
        max_retries=3,
    )
    base.update(kwargs)
    return JiraAgentState(**base)


def test_clip_helper():
    assert _clip("short", 100) == "short"
    assert "truncated" in _clip("x" * 500, 50)


def test_ack_includes_issue_and_workflow(reporter, client):
    st = _state(metadata={"workflow_type": "planning", "backend": "claude"})
    reporter.post_initial_acknowledgment(st)
    body = client.comments[-1]["body"]
    assert "İş başladı" in body
    assert "`Claude Code`" in body
    assert "MSG-1" in body
    assert "planning" in body.lower()
    assert "Devam Ediyor" in body


def test_ack_unknown_workflow_and_empty_summary(reporter, client):
    st = _state(issue_summary="", metadata={})
    reporter.post_initial_acknowledgment(st)
    body = client.comments[-1]["body"]
    assert "özet yok" in body.lower()
    assert "unknown" not in body.lower()
    assert "*İş akışı:*" not in body


def test_plan_summary_empty_plan_explains_next_steps(reporter, client):
    st = _state(plan_path="/tmp/missing.md", status=TaskStatus.PLAN_READY)
    reporter.post_plan_summary(st, "")
    body = client.comments[-1]["body"]
    assert "— Plan**" in body
    assert "`Claude Code`" in body
    assert "plan içeriği" in body.lower() or "bulunamadı" in body.lower()
    assert "Mode: build" in body
    assert "yorumda" in body.lower()


def test_plan_summary_whitespace_only(reporter, client):
    st = _state(status=TaskStatus.PLAN_READY)
    reporter.post_plan_summary(st, "   \n\n  ")
    body = client.comments[-1]["body"]
    assert "— Plan**" in body


def test_progress_empty_message_and_clamps_pct(reporter, client):
    st = _state()
    reporter.post_progress_update(st, "", progress_percentage=150)
    body = client.comments[-1]["body"]
    assert "İlerleme" in body
    assert "`Claude Code`" in body
    assert "100%" in body
    assert "ayrıntı yok" in body.lower()


def test_progress_negative_pct_clamped(reporter, client):
    st = _state()
    reporter.post_progress_update(st, "retrying", progress_percentage=-5)
    assert "0%" in client.comments[-1]["body"]


def test_progress_invalid_pct_omitted(reporter, client):
    st = _state()
    reporter.post_progress_update(st, "ok", progress_percentage="not-a-number")  # type: ignore[arg-type]
    body = client.comments[-1]["body"]
    assert "İlerleme" in body
    assert "not-a-number" not in body


def test_progress_none_state():
    r = JiraReporter(client=FakeJiraClient())
    assert r.post_progress_update(None, "x") is None  # type: ignore[arg-type]


def test_completion_with_mr_and_branch(reporter, client):
    st = _state(
        status=TaskStatus.COMPLETED,
        completed_at=datetime(2026, 6, 1, 10, 0, 0),
        execution_duration_seconds=12.5,
        metadata={
            "workflow_type": "execution",
            "backend": "claude",
            "merge_request_url": "https://gitlab.example/mr/1",
            "feature_branch": "feature/MSG-1",
        },
    )
    reporter.post_completion(st, "Implemented main.cpp")
    body = client.comments[-1]["body"]
    assert "tamamlandı" in body.lower()
    assert "`Claude Code`" in body
    assert "https://gitlab.example/mr/1" in body
    assert "feature/MSG-1" in body
    assert "12.5" in body


def test_completion_without_mr_mentions_branch_or_manual(reporter, client):
    st = _state(
        status=TaskStatus.COMPLETED,
        metadata={"feature_branch": "feature/MSG-1"},
    )
    reporter.post_completion(st, "")
    body = client.comments[-1]["body"]
    assert "tamamlandı" in body.lower()
    assert "feature/MSG-1" in body
    assert "birleştirme" in body.lower()
    assert "bitti" in body.lower() or "dal" in body.lower()


def test_completion_no_delivery_metadata(reporter, client):
    st = _state(status=TaskStatus.COMPLETED, metadata={})
    reporter.post_completion(st, "done")
    body = client.comments[-1]["body"]
    assert "Birleştirme isteği adresi" in body or "feature/" in body


def test_error_empty_message_and_default_suggestion(reporter, client):
    st = _state(status=TaskStatus.ERROR, error_message=None)
    reporter.post_error(st, "")
    body = client.comments[-1]["body"]
    assert "Başarısız" in body
    assert "`Claude Code`" in body
    assert "Bilinmeyen hata" in body
    assert "Öneri" in body
    assert "Yapılacaklar" in body


def test_error_truncates_huge_message(reporter, client):
    st = _state(status=TaskStatus.ERROR)
    huge = "E" * 10000
    reporter.post_error(st, huge, suggestion="retry")
    body = client.comments[-1]["body"]
    assert "truncated" in body
    assert len(body) < 5000


def test_error_timeout_and_retries(reporter, client):
    st = _state(
        status=TaskStatus.ERROR,
        timed_out=True,
        timeout_seconds=60,
        retry_count=3,
        max_retries=3,
        current_opencode_session_id="ses_x",
    )
    reporter.post_error(st, "hung")
    body = client.comments[-1]["body"]
    assert "Zaman aşımı" in body
    assert "Denemeler tükendi" in body
    assert "ses_x" in body


def test_comment_response_empty(reporter, client):
    reporter.post_comment_response("MSG-1", "")
    body = client.comments[-1]["body"]
    assert "Yanıt" in body
    assert "boş" in body.lower()


def test_comment_response_exception_returns_none():
    client = MagicMock()
    client.add_comment.side_effect = RuntimeError("down")
    r = JiraReporter(client=client)
    assert r.post_comment_response("X-1", "hi") is None




def test_all_message_types_have_yaver_header(reporter, client):
    """Every user-visible template starts with the Yaver header, not a second title."""
    st = _state(
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        plan_path="p.md",
    )
    reporter.post_initial_acknowledgment(st)
    reporter.post_plan_summary(st, "# plan")
    reporter.post_progress_update(st, "halfway", 50)
    reporter.post_completion(st, "done")
    reporter.post_error(st, "fail", suggestion="retry")
    reporter.post_comment_response("MSG-1", "ok")

    bodies = [c["body"] for c in client.comments]
    assert any("İş başladı" in b for b in bodies)
    assert any("— Plan**" in b for b in bodies)
    assert any("İlerleme" in b for b in bodies)
    assert any("Tamamlandı" in b for b in bodies)
    assert any("Başarısız" in b for b in bodies)
    assert any("Yanıt" in b for b in bodies)
    assert not any("Yapay zekâ —" in b for b in bodies)
    assert not any(line.startswith("h3.") for b in bodies for line in b.splitlines())


def test_fail_issue_posts_error_and_sets_status(state_manager, fake_jira):
    from src.processor import JobProcessor
    from unittest.mock import patch

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        with patch("src.reporter.jira_reporter.create_jira_client", return_value=fake_jira):
            proc = JobProcessor()
    # Force both state + reporting onto the same fake client
    proc.state_manager = state_manager
    proc.reporter = JiraReporter(client=fake_jira)
    proc.jira_client = fake_jira

    state_manager.create_state("FAIL-1", "s", "d")
    state_manager.update_state("FAIL-1", status=TaskStatus.EXECUTING)
    proc._fail_issue("FAIL-1", "", suggestion=None)

    loaded = state_manager.get_state("FAIL-1")
    assert loaded.status == TaskStatus.ERROR
    assert fake_jira.comments, "expected a Jira error comment"
    body = fake_jira.comments[-1]["body"]
    assert "Başarısız" in body
    assert "Bilinmeyen hata" in body or "hata" in body.lower()


def test_fail_issue_missing_state_still_comments(state_manager, fake_jira):
    from src.processor import JobProcessor
    from unittest.mock import patch

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        with patch("src.reporter.jira_reporter.create_jira_client", return_value=fake_jira):
            proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = JiraReporter(client=fake_jira)
    proc.jira_client = fake_jira

    proc._fail_issue("GHOST-9", "no state file")
    assert any(
        "no state file" in c["body"].lower() or "error occurred" in c["body"].lower()
        for c in fake_jira.comments
    ), fake_jira.comments