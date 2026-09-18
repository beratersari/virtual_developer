"""Yaver MR/PR review path: always on. Work-item /review stays silent."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.gitlab.mentions import (
    EMPTY_ASK_REASON,
    REVIEW_HANDOFF_REASON,
    classify_comment_command,
    note_is_other_agent_handoff,
)
from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.session_bind_store import normalize_session_kind
from src.azure.webhook import decide_azure_pr_webhook
from src.gitlab.webhook import decide_gitlab_mr_webhook
from src.review_flow import ask_wants_new_review
from tests.test_ask_handoff import _az, _gl, _mr_payload
from tests.test_azure_webhook import _pr_lifecycle_payload
from tests.test_gitlab_webhook import _mr_lifecycle_payload


def test_review_helpers_still_detect_commands():
    assert note_is_other_agent_handoff("@yaver /review", ["yaver"]) is True
    assert note_is_other_agent_handoff("@yaver /ask why", ["yaver"]) is True


def test_review_and_ask_accepted():
    d = _gl("@berat_ai /review the auth change")
    assert d.accepted is True
    assert d.event.command == "review"
    assert "auth change" in d.event.prompt
    d2 = _gl("@berat_ai /ask why is this lock needed?")
    assert d2.accepted is True
    assert d2.event.command == "ask"
    assert "lock" in d2.event.prompt
    d3 = _az("@yaver /review look at login")
    assert d3.accepted is True
    assert d3.event.command == "review"
    d4 = _az("@yaver /ask what does line 40 do")
    assert d4.accepted is True
    assert d4.event.command == "ask"


def test_empty_ask_skipped():
    d = _gl("@berat_ai /ask")
    assert d.accepted is False
    assert d.reason == EMPTY_ASK_REASON
    assert d.usage_note is not True


def test_yaver_still_starts_job():
    d = _gl("@berat_ai /yaver add tests")
    assert d.accepted is True
    assert d.event.command == "yaver"
    d2 = _az("@yaver /yaver add tests")
    assert d2.accepted is True
    assert d2.event.command == "yaver"


def test_ask_review_again_becomes_full_review():
    assert ask_wants_new_review("please review again") is True
    d = _gl("@berat_ai /ask please review again")
    assert d.accepted is True
    assert d.event.command == "review"


def test_gitlab_note_edit_is_ignored():
    payload = _mr_payload(note="@berat_ai /review the auth change")
    payload["object_attributes"]["action"] = "update"
    from src.gitlab.webhook import decide_gitlab_note_webhook

    d = decide_gitlab_note_webhook(
        payload,
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert d.accepted is False
    assert d.reason == "note edit"


def test_gitlab_open_without_reviewer_does_not_auto_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    d = decide_gitlab_mr_webhook(
        _mr_lifecycle_payload(action="open", state="opened"),
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.accepted is True
    assert d.event.start_review is False


def test_gitlab_draft_open_skips_auto_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.yaver_review_skip_drafts", True)
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(action="open", state="opened")
    payload["object_attributes"]["draft"] = True
    payload["object_attributes"]["reviewers"] = [
        {"id": 9, "username": "berat_ai"}
    ]
    d = decide_gitlab_mr_webhook(
        payload,
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.event.start_review is False


def test_gitlab_open_with_reviewer_starts_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(action="open", state="opened")
    payload["object_attributes"]["reviewers"] = [
        {"id": 9, "username": "berat_ai", "name": "Berat"}
    ]
    d = decide_gitlab_mr_webhook(
        payload,
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.event.start_review is True
    assert d.event.review_explicit is False


def test_gitlab_update_new_commits_does_not_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(action="update", state="opened")
    payload["changes"] = {"title": {"previous": "a", "current": "b"}}
    d = decide_gitlab_mr_webhook(
        payload,
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.event.start_review is False


def test_gitlab_assign_reviewer_starts_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(action="update", state="opened")
    payload["changes"] = {
        "reviewers": {
            "previous": [],
            "current": [{"id": 9, "username": "berat_ai", "name": "Berat"}],
        }
    }
    d = decide_gitlab_mr_webhook(
        payload,
        headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
        enabled=True,
        secret="s",
    )
    assert d.event.start_review is True
    assert d.event.review_explicit is True


def test_gitlab_assign_matches_reviewer_id_like_amirmini(monkeypatch):
    """aMIR-mini matches the PAT user id, not only GITLAB_TRIGGER_USER text."""
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "yaver_bot")
    payload = _mr_lifecycle_payload(action="update", state="opened")
    payload["changes"] = {"reviewer_ids": {"previous": [1], "current": [1, 42]}}
    payload["reviewers"] = [{"id": 42, "username": "other"}]
    with patch(
        "src.gitlab.client.GitlabClient.current_user",
        return_value={"id": 42, "username": "other", "name": "Other Bot"},
    ):
        d = decide_gitlab_mr_webhook(
            payload,
            headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
            enabled=True,
            secret="s",
        )
    assert d.event.start_review is True
    assert d.event.review_explicit is True


def test_azure_created_with_reviewer_starts_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    payload = _pr_lifecycle_payload(event_type="git.pullrequest.created", status="active")
    payload["resource"]["reviewers"] = [
        {"id": "guid-1", "displayName": "Yaver", "uniqueName": "DOMAIN\\yaver"}
    ]
    d = decide_azure_pr_webhook(payload, enabled=True)
    assert d.accepted is True
    assert d.event.start_review is True
    assert d.event.review_explicit is False


def test_azure_assign_matches_pat_identity_like_amirmini(monkeypatch):
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver_bot")
    from src.review_flow import _AZURE_REVIEWER_CACHE

    _AZURE_REVIEWER_CACHE.clear()
    payload = _pr_lifecycle_payload(
        event_type="git.pullrequest.reviewers.update", status="active"
    )
    payload["message"] = {"text": "Ada changed the reviewer list for PR 4"}
    payload["resource"]["reviewers"] = [
        {
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "displayName": "Yaver Service",
            "uniqueName": "DOMAIN\\svc",
        }
    ]
    with patch(
        "src.azure.identity.fetch_bot_identity",
        return_value={
            "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "names": ["Yaver Service"],
        },
    ), patch(
        "src.azure.client.AzureDevOpsClient.list_pr_reviewers",
        return_value=payload["resource"]["reviewers"],
    ):
        d = decide_azure_pr_webhook(payload, enabled=True)
    assert d.accepted is True
    assert d.event.start_review is True


def test_azure_reviewers_update_event_starts_review(monkeypatch):
    """aMIR-mini: assign fires git.pullrequest.reviewers.update, not .updated."""
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    from src.review_flow import _AZURE_REVIEWER_CACHE

    _AZURE_REVIEWER_CACHE.clear()
    payload = _pr_lifecycle_payload(
        event_type="git.pullrequest.reviewers.update", status="active"
    )
    payload["message"] = {"text": "Ada changed the reviewer list for PR 4"}
    payload["resource"]["reviewers"] = [
        {"id": "guid-1", "displayName": "Yaver", "uniqueName": "DOMAIN\\yaver"}
    ]
    with patch(
        "src.azure.client.AzureDevOpsClient.list_pr_reviewers",
        return_value=payload["resource"]["reviewers"],
    ):
        d = decide_azure_pr_webhook(payload, enabled=True)
    assert d.accepted is True
    assert d.event.start_review is True
    assert d.event.review_explicit is True


def test_azure_update_without_reviewer_change_does_not_review(monkeypatch):
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    from src.review_flow import _AZURE_REVIEWER_CACHE

    _AZURE_REVIEWER_CACHE.clear()
    payload = _pr_lifecycle_payload(event_type="git.pullrequest.updated", status="active")
    payload["resource"]["reviewers"] = [
        {"id": "guid-1", "displayName": "Yaver", "uniqueName": "DOMAIN\\yaver"}
    ]
    decide_azure_pr_webhook(payload, enabled=True)
    d2 = decide_azure_pr_webhook(payload, enabled=True)
    assert d2.event.start_review is False


def test_work_item_review_stays_silent():
    from src.azure.workitems import decide_azure_workitem_comment_webhook
    from tests.test_azure_workitems import _wi_comment_payload

    decision = decide_azure_workitem_comment_webhook(
        _wi_comment_payload(note="@yaver /review the bug"),
        enabled=True,
        bot_mentions=["yaver"],
    )
    assert decision.accepted is False
    assert decision.usage_note is False
    assert decision.reason == REVIEW_HANDOFF_REASON


def test_bare_mention_still_usage_note():
    d = _gl("@berat_ai please look")
    assert d.accepted is False
    assert d.usage_note is True


def test_classify_prefers_yaver_over_review():
    assert (
        classify_comment_command("@yaver /yaver /review also", ["yaver"]) == "yaver"
    )
    assert classify_comment_command("@yaver /review", ["yaver"]) == "review"
    assert classify_comment_command("@yaver /ask hi", ["yaver"]) == "ask"
    assert classify_comment_command("@yaver hello", ["yaver"]) == ""


def test_review_workflow_agent_and_session_kind():
    assert WorkflowRouter.get_agent_for_workflow(WorkflowType.REVIEW) == "derman-reviewer"
    assert normalize_session_kind("review") == "review"
    assert normalize_session_kind("code-reviewer") == "review"
    assert normalize_session_kind("derman-reviewer") == "review"
    assert normalize_session_kind("gitlab-review") == "review"
    assert normalize_session_kind("azure-review") == "review"
    assert normalize_session_kind("gitlab_mr") == "build"
    assert JobProcessor._is_review_comment(MagicMock(command="review")) is True
    assert JobProcessor._is_review_comment(MagicMock(command="ask")) is True
    assert JobProcessor._is_review_comment(MagicMock(command="yaver")) is False
    assert JobProcessor._is_review_comment(MagicMock()) is False


def test_review_prompt_forbids_push_and_edits():
    text = PromptBuilder.build_review_comment_prompt(
        issue_key="KAN-12",
        title="feat(KAN-12): login",
        url="https://gitlab.example.com/g/r/-/merge_requests/3",
        source_branch="feature/KAN-12",
        target_branch="develop",
        author="alice",
        comment="look at auth",
        forge="gitlab",
        command="review",
    )
    low = text.lower()
    assert "do **not** edit" in low or "do not edit" in low
    assert "push" in low
    assert "auth" in text
    assert "KAN-12" in text or "12" in text


@pytest.mark.asyncio
async def test_processor_review_does_not_push(
    tmp_path, monkeypatch, fake_jira, reporter, isolate_jira_agent_artifacts
):
    from src.gitlab.webhook import decide_gitlab_note_webhook
    from src.processor import JobProcessor
    from src.state.models import TaskStatus

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    clone = tmp_path / "clone"
    clone.mkdir()
    git = MagicMock()
    git.remote_url = "https://gitlab.example.com/acme/demo.git"
    git.work_branch = "feature/login"
    git.target_branch = "develop"
    git.get_working_directory.return_value = clone
    git.ensure_feature_branch.return_value = "feature/login"
    git.ensure_on_work_branch.return_value = True
    git.get_last_commit_sha.return_value = "abc123deadbeef"
    git.commits_ahead_of_target.return_value = 0
    git.push.return_value = True
    runner = MagicMock()
    runner.run_agent_with_retry = AsyncMock(
        return_value={
            "returncode": 0,
            "stdout": "### Özet\n\nLooks fine.",
            "stderr": "",
            "session_file": str(tmp_path / "s.log"),
            "opencode_session_id": "ses_rev1",
        }
    )
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 99}

    decision = decide_gitlab_note_webhook(
        _mr_payload(note="@berat_ai /review the auth change"),
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "s"},
        secret="s",
        bot_mentions=["@berat_ai"],
    )
    assert decision.accepted is True
    assert decision.event.command == "review"

    def fake_init(*_a, **_k):
        proc._contexts[decision.event.issue_key] = {"git": git, "runner": runner}
        proc.git_manager = git
        proc.agent_runner = runner
        return git

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira

    with patch.object(proc, "_init_git_manager", side_effect=fake_init), patch.object(
        proc, "_runner_for", return_value=runner
    ), patch.object(
        proc, "_push_and_create_mr", new_callable=AsyncMock
    ) as push_mr, patch(
        "src.gitlab.client.GitlabClient.post_mr_note", fake_post
    ):
        await proc.handle_gitlab_mr_comment(decision.event)

    st = sm.get_state(decision.event.issue_key)
    assert st is not None
    assert st.status == TaskStatus.COMPLETED
    assert (st.metadata or {}).get("workflow_type") == "gitlab-review"
    push_mr.assert_not_awaited()
    git.push.assert_not_called()
    task = runner.run_agent_with_retry.await_args.args[0]
    assert task.agent == "derman-reviewer"
    assert "Review delivery" in task.prompt
    assert posted.get("mr_iid") == 4
    assert "Özet" in (posted.get("body") or "")


def test_http_review_enqueues(fake_jira, monkeypatch):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        from src.processor import JobProcessor

        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": "KAN-12"}
    )
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post(
            "/yaver/webhook/gitlab",
            json=_mr_payload(note="@berat_ai /review the diff"),
            headers={"X-Gitlab-Token": "tok", "X-Gitlab-Event": "Note Hook"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("ok") is True
    proc.enqueue_gitlab_note.assert_called_once()


@pytest.mark.asyncio
async def test_lifecycle_review_reenqueues_after_error(
    tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts
):
    """Re-assign after a failed review must not reuse the error queue row."""
    from src.gitlab.webhook import GitlabMrNoteEvent

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.queue_store = isolate_jira_agent_artifacts["queue_store"]
    proc.dispatch_queue = AsyncMock()
    ev = GitlabMrNoteEvent(
        issue_key="KAN-13351",
        note_id="review-update-570",
        note_body="",
        prompt="Review this merge request.",
        author_username="",
        author_name="",
        project_id=1,
        project_path="tanksb/volkan/volkan",
        repository_url="https://gitlab.example.com/tanksb/volkan/volkan.git",
        host="gitlab.example.com",
        mr_iid=570,
        mr_title="KAN-13351",
        mr_description="",
        source_branch="feature/x",
        target_branch="develop",
        mr_url="https://gitlab.example.com/tanksb/volkan/volkan/-/merge_requests/570",
        command="review",
    )
    first = await proc.enqueue_gitlab_note(ev)
    assert first.get("ok") is True
    proc.queue_store.finish(
        first["queue_id"], status="error", error_message="agent missing"
    )
    second = await proc.enqueue_gitlab_note(ev)
    assert second.get("duplicate") is not True
    assert second["queue_id"] != first["queue_id"]
    assert second.get("status") in {"queued", "running"}


def test_lifecycle_stamp_uses_updated_at():
    ev = MagicMock()
    ev.raw = {"object_attributes": {"updated_at": "2026-09-18T17:46:26.000Z"}}
    stamp = JobProcessor._lifecycle_review_stamp(ev)
    assert "2026-09-18" in stamp

