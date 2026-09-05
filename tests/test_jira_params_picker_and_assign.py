"""Lookup without {params} (picker + write-back) and PAT-user assignment."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.issue_git_spec import peek_issue_git_fields
from src.jira.client import assign_ident_from_myself, assign_to_pat_user
from src.jira.webhook import decide_jira_webhook
from src.scheduler.service import preview_existing_issue, schedule_existing_issue
from src.state.schedule_store import ScheduleStore
from tests.test_jira_webhook import (
    MENTIONS,
    NEEDLES,
    SECRET,
    _assignment_payload,
    _created_assigned_payload,
    _headers,
)


def _issue(key: str, description: str, summary: str = "Do work") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
            "assignee": None,
        },
    }


def test_peek_partial_params_and_empty():
    empty = peek_issue_git_fields("s", "no template")
    assert empty["repository_url"] == ""
    assert empty["source_branch"] == ""

    partial = peek_issue_git_fields(
        "s",
        "{params}\nRepository: https://gitlab.com/a/b.git\nMode: plan\n{params}",
    )
    assert partial["repository_url"].endswith("b.git")
    assert partial["mode"] == "plan"
    assert partial["source_branch"] == ""


def test_preview_invalid_returns_partial_for_picker():
    client = MagicMock()
    client.get_issue.return_value = _issue(
        "KAN-PP",
        "{params}\nRepository: https://gitlab.com/org/app.git\n{params}\nFix login.",
    )
    out = preview_existing_issue("KAN-PP", jira_client=client)
    assert out["ok"] is False
    assert out["template_valid"] is False
    assert out["title"] == "Do work"
    assert out["prompt"] == "Fix login."
    assert out["repository_url"].endswith("app.git")
    assert out["source_branch"] == ""


def test_schedule_existing_picker_writes_params(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-NP", "just a ticket body")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = True
    client.get_myself.return_value = {
        "name": "devbot",
        "key": "devbot",
        "displayName": "DevBot",
    }
    client.assign_issue.return_value = True
    client.is_cloud = False

    out = schedule_existing_issue(
        "KAN-NP",
        scheduled_at="2026-12-01T10:00:00",
        description="just a ticket body",
        repository_url="https://gitlab.com/org/app.git",
        source_branch_mode="issue_key",
        target_branch="develop",
        mode="build",
        model="opencode/hy3-free",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["issue_key"] == "KAN-NP"
    assert rec["source_branch"] == "feature/KAN-NP"
    assert rec["target_branch"] == "develop"
    assert rec["mode"] == "build"
    written = client.update_issue.call_args.kwargs["fields"]["description"]
    assert "{params}" in written
    assert "Repository: https://gitlab.com/org/app.git" in written
    assert "Source branch: feature/KAN-NP" in written
    assert "Target branch: develop" in written
    assert "Mode: build" in written
    assert "just a ticket body" in written
    client.assign_issue.assert_called()
    assert client.assign_issue.call_args.args[0] == "KAN-NP"
    assert client.assign_issue.call_args.args[1] == "devbot"


def test_schedule_existing_invalid_without_picker_still_fails(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-NO", "no params here")
    out = schedule_existing_issue(
        "KAN-NO",
        scheduled_at="2026-12-01T10:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is False
    assert out.get("template_valid") is False
    client.update_issue.assert_not_called()
    assert store.list_schedules() == []


def test_assign_ident_from_myself_server_and_cloud():
    assert (
        assign_ident_from_myself(
            {"name": "devbot", "accountId": "557058:x"}, is_cloud=False
        )
        == "devbot"
    )
    assert (
        assign_ident_from_myself(
            {"name": "devbot", "accountId": "557058:x"}, is_cloud=True
        )
        == "557058:x"
    )
    assert assign_ident_from_myself(None) == ""
    assert assign_ident_from_myself("not-a-dict") == ""


def test_assign_to_pat_user_never_writes_for_gitlab_key():
    client = MagicMock()
    client.get_myself.return_value = {"name": "devbot"}
    client.assign_issue.return_value = True
    assert assign_to_pat_user(client, "GL-ACME-DEMO-4") is False
    client.assign_issue.assert_not_called()
    client.get_myself.assert_not_called()


def test_assign_to_pat_user_never_writes_for_gitlab_source():
    client = MagicMock()
    client.get_myself.return_value = {"name": "devbot"}
    client.assign_issue.return_value = True
    assert (
        assign_to_pat_user(client, "KAN-12", source="gitlab") is False
    )
    assert (
        assign_to_pat_user(
            client, "KAN-12", issue={"metadata": {"source": "gitlab"}}
        )
        is False
    )
    client.assign_issue.assert_not_called()


def test_assign_to_pat_user_skips_when_already_set():
    client = MagicMock()
    client.is_cloud = False
    client.get_myself.return_value = {"name": "devbot", "key": "devbot"}
    client.get_issue.return_value = {
        "fields": {"assignee": {"name": "devbot", "displayName": "DevBot"}}
    }
    assert assign_to_pat_user(client, "KAN-1") is True
    client.assign_issue.assert_not_called()


def test_assign_to_pat_user_writes_when_unassigned():
    client = MagicMock()
    client.is_cloud = False
    client.get_myself.return_value = {"name": "devbot", "key": "devbot"}
    client.get_issue.return_value = {"fields": {"assignee": None}}
    client.assign_issue.return_value = True
    assert assign_to_pat_user(client, "KAN-2") is True
    client.assign_issue.assert_called_once_with("KAN-2", "devbot")


def test_assign_to_pat_user_uses_issue_arg_without_refetch():
    client = MagicMock()
    client.is_cloud = False
    client.get_myself.return_value = {"name": "devbot"}
    client.assign_issue.return_value = True
    issue = {"key": "KAN-3", "fields": {"assignee": None}}
    assert assign_to_pat_user(client, "KAN-3", issue=issue) is True
    client.get_issue.assert_not_called()
    client.assign_issue.assert_called_once_with("KAN-3", "devbot")


def test_webhook_ignores_self_assignment():
    payload = _assignment_payload()
    payload["user"] = {
        "name": "devbot",
        "key": "devbot",
        "displayName": "DevBot",
    }
    d = decide_jira_webhook(
        payload,
        headers=_headers(),
        query={"token": SECRET},
        secret=SECRET,
        intake_mode="webhook",
        assignee_needles=NEEDLES,
        mention_tokens=MENTIONS,
    )
    assert not d.accepted
    assert "self-assignment" in d.reason


def test_webhook_still_accepts_human_assignment():
    payload = _assignment_payload()
    payload["user"] = {"name": "alice", "displayName": "Alice"}
    d = decide_jira_webhook(
        payload,
        headers=_headers(),
        query={"token": SECRET},
        secret=SECRET,
        intake_mode="webhook",
        assignee_needles=NEEDLES,
        mention_tokens=MENTIONS,
    )
    assert d.accepted
    assert d.trigger == "assignment"


def test_api_from_issue_picker_writes_params(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.state.manager import JiraStateManager

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    client = MagicMock()
    client.get_issue.return_value = _issue("KAN-API", "operator ticket")
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = True
    client.get_myself.return_value = {"name": "devbot", "key": "devbot"}
    client.assign_issue.return_value = True
    client.is_cloud = False

    monkeypatch.setattr("src.dashboard.api.schedule_store", store)
    monkeypatch.setattr(
        "src.dashboard.api.preview_existing_issue",
        lambda issue_key: preview_existing_issue(issue_key, jira_client=client),
    )
    monkeypatch.setattr(
        "src.dashboard.api.schedule_existing_issue",
        lambda issue_key, scheduled_at, store=None, **kw: schedule_existing_issue(
            issue_key,
            scheduled_at=scheduled_at,
            jira_client=client,
            store=store,
            **kw,
        ),
    )
    app = create_dashboard_app(processor=None, state_manager=sm)
    tc = TestClient(app)
    r_prev = tc.get("/api/schedules/preview", params={"issue_key": "KAN-API"})
    assert r_prev.status_code == 200
    body = r_prev.json()
    assert body["template_valid"] is False
    assert body["title"] == "Do work"

    r = tc.post(
        "/api/schedules/from-issue",
        json={
            "issue_key": "KAN-API",
            "scheduled_at": "2026-12-26T08:00:00",
            "description": "operator ticket",
            "repository_url": "https://gitlab.com/org/app.git",
            "target_branch": "develop",
            "mode": "plan",
            "source_branch_mode": "issue_key",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    written = client.update_issue.call_args.kwargs["fields"]["description"]
    assert "Mode: plan" in written
    assert "Source branch: feature/KAN-API" in written
    client.assign_issue.assert_called_with("KAN-API", "devbot")


def test_webhook_ignores_bot_created_issue():
    payload = _created_assigned_payload()
    payload["user"] = {
        "name": "devbot",
        "key": "devbot",
        "displayName": "DevBot",
    }
    d = decide_jira_webhook(
        payload,
        headers=_headers(),
        query={"token": SECRET},
        secret=SECRET,
        intake_mode="webhook",
        assignee_needles=NEEDLES,
        mention_tokens=MENTIONS,
    )
    assert not d.accepted
    assert "bot-created" in d.reason
