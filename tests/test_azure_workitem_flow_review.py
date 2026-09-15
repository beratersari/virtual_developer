"""Review Azure work-item intake vs Jira / GitLab / Azure PR.

Every assertion is a behaviour check. Failures here are the only
critical claims this review will report.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.azure.keys import is_azure_issue_key, is_azure_work_item_key
from src.azure.webhook import AZURE_PR_EVENTS
from src.azure.workitems import (
    AZURE_WORKITEM_EVENTS,
    azure_html_to_text,
    decide_azure_workitem_comment_webhook,
    decide_azure_workitem_webhook,
    evaluate_work_item_intake,
    is_azure_workitem_comment_event,
    parse_workitem_payload,
)
from src.issue_git_spec import parse_issue_git_spec
from src.processor import JobProcessor
from src.state.models import TaskStatus
from tests.test_azure_workitems import _wi_comment_payload, _wi_payload


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def test_jira_and_gitlab_and_pr_keys_are_not_work_items():
    for key in ("KAN-12", "PROJ-1", "GL-ACME-DEMO-4", "AZ-DEMO-APP-12"):
        assert is_azure_work_item_key(key) is False, key


def test_work_item_numeric_key_is_not_a_pr_or_gitlab_key():
    assert is_azure_work_item_key("42") is True
    assert is_azure_issue_key("42") is False
    from src.gitlab.keys import is_gitlab_issue_key

    assert is_gitlab_issue_key("42") is False


def test_processor_does_not_treat_work_item_as_pr_or_gitlab(processor, state_manager):
    state_manager.create_state("42", "s", "d")
    state_manager.update_state("42", metadata={"source": "azure_workitem"})
    assert processor._is_azure_workitem_triggered("42") is True
    assert processor._is_azure_triggered("42") is False
    assert processor._is_gitlab_triggered("42") is False
    assert processor._is_git_comment_triggered("42") is False


def test_processor_does_not_treat_jira_key_as_work_item(processor, state_manager):
    state_manager.create_state("KAN-12", "s", "d")
    state_manager.update_state("KAN-12", metadata={"source": "jira"})
    assert processor._is_azure_workitem_triggered("KAN-12") is False
    assert processor._is_azure_triggered("KAN-12") is False
    assert processor._is_gitlab_triggered("KAN-12") is False


def test_processor_does_not_treat_pr_key_as_work_item(processor, state_manager):
    state_manager.create_state("AZ-DEMO-APP-12", "s", "d")
    state_manager.update_state("AZ-DEMO-APP-12", metadata={"source": "azure"})
    assert processor._is_azure_workitem_triggered("AZ-DEMO-APP-12") is False
    assert processor._is_azure_triggered("AZ-DEMO-APP-12") is True
    assert processor._is_git_comment_triggered("AZ-DEMO-APP-12") is True


def test_pr_event_types_are_not_workitem_event_types():
    assert not (AZURE_PR_EVENTS & AZURE_WORKITEM_EVENTS)


def test_pr_comment_payload_is_not_a_workitem_comment():
    payload = {
        "eventType": "git.pullrequest.comment",
        "resource": {
            "comment": {"content": "@yaver /yaver fix"},
            "pullRequest": {"pullRequestId": 9},
        },
    }
    assert is_azure_workitem_comment_event(payload) is False
    assert decide_azure_workitem_webhook(payload, enabled=True).accepted is False


def test_html_params_still_parse_after_azure_flatten():
    html = (
        "<div>{params}<br/>Repository: https://tfs.example.com/tfs/Col/P/_git/app"
        "<br/>Source branch: feature/a<br/>Target branch: develop"
        "<br/>Mode: build<br/>{params}</div>"
    )
    text = azure_html_to_text(html)
    spec, err = parse_issue_git_spec("do it", text)
    assert err is None
    assert spec is not None
    assert spec.mode == "build"
    assert "app" in spec.repository_url
    assert spec.source_branch == "feature/a"
    assert spec.target_branch == "develop"


def test_html_params_in_paragraphs_still_parse():
    html = (
        "<p>{params}</p><p>Repository: https://example.com/r.git</p>"
        "<p>Source branch: develop</p><p>Target branch: main</p>"
        "<p>Mode: plan</p><p>{params}</p>"
    )
    spec, err = parse_issue_git_spec("x", azure_html_to_text(html))
    assert err is None
    assert spec is not None
    assert spec.mode == "plan"
    assert spec.source_branch == "develop"
    assert spec.target_branch == "main"


def test_in_flight_work_item_is_not_restarted():
    issue = parse_workitem_payload(_wi_payload(state="Active")).issue
    for status in (TaskStatus.PENDING, TaskStatus.PLANNING, TaskStatus.EXECUTING):
        state = SimpleNamespace(status=status, metadata={})
        decision = evaluate_work_item_intake(
            issue, state=state, trigger_needles=["yaver"]
        )
        assert decision.action == "skip", status
        assert "in-flight" in decision.reason


def test_plan_ready_assignment_does_not_start_implement():
    issue = parse_workitem_payload(_wi_payload(state="Active")).issue
    state = SimpleNamespace(status=TaskStatus.PLAN_READY, metadata={})
    decision = evaluate_work_item_intake(
        issue, state=state, trigger_needles=["yaver"]
    )
    assert decision.action == "skip"
    assert "plan_ready" in decision.reason


def test_plan_execute_comment_without_plan_ready_does_not_enqueue(processor):
    payload = _wi_comment_payload(note="@yaver /planExecute")
    decision = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert decision.accepted is True
    assert decision.event.plan_handoff == "execute"

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=decision.event.issue,
        ):
            return await processor.ingest_azure_work_item(decision.event)

    import asyncio

    result = asyncio.run(_run())
    assert result.get("queued") is False
    assert result.get("started") is False
    assert "plan_ready" in (result.get("reason") or "")


def test_done_work_item_is_not_accepted():
    issue = parse_workitem_payload(_wi_payload(state="Done")).issue
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "skip"
    assert decision.is_done is True


def test_unassigned_new_item_is_not_accepted():
    issue = parse_workitem_payload(
        _wi_payload(state="New", assignee="")
    ).issue
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "skip"


def test_new_assigned_item_is_accepted():
    issue = parse_workitem_payload(_wi_payload(state="New")).issue
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "accept"
    assert decision.will_process is True
