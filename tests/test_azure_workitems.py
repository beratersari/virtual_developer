"""Azure Boards work-item webhook intake (Server 2022.2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.azure.keys import (
    azure_work_item_key,
    is_azure_issue_key,
    is_azure_work_item_key,
    parse_azure_work_item_key,
)
from src.azure.workitems import (
    azure_html_to_text,
    classify_workitem_changes,
    decide_azure_workitem_comment_webhook,
    decide_azure_workitem_webhook,
    evaluate_work_item_intake,
    format_workitem_plan_usage_note,
    is_azure_workitem_comment_event,
    lookup_work_item_view,
    normalize_work_item,
    parse_azure_tags,
    clear_workitem_comment_claims,
    parse_workitem_payload,
    workitem_plan_command,
)
from src.state.models import TaskStatus


@pytest.fixture(autouse=True)
def _clear_azure_comment_claims():
    clear_workitem_comment_claims()
    yield
    clear_workitem_comment_claims()


def _wi_payload(
    *,
    event="workitem.updated",
    work_item_id=42,
    title="Do the thing",
    description="<div>{params}<br/>Repository: https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app<br/>Source branch: feature/a<br/>Target branch: develop<br/>Mode: build<br/>{params}</div>",
    state="New",
    assignee="Yaver",
    unique="DOMAIN\\yaver",
    tags="",
    changed=None,
    rev=3,
    project="Demo",
):
    fields = {
        "System.Title": title,
        "System.Description": description,
        "System.State": state,
        "System.TeamProject": project,
        "System.WorkItemType": "User Story",
        "System.Tags": tags,
        "System.AssignedTo": {
            "displayName": assignee,
            "uniqueName": unique,
            "id": "guid-1",
        }
        if assignee
        else None,
    }
    if fields["System.AssignedTo"] is None:
        fields.pop("System.AssignedTo")
    changed_fields = changed
    if changed_fields is None:
        changed_fields = {
            "System.AssignedTo": {
                "oldValue": None,
                "newValue": fields.get("System.AssignedTo"),
            }
        }
    return {
        "eventType": event,
        "resource": {
            "id": work_item_id,
            "workItemId": work_item_id,
            "rev": rev,
            "fields": changed_fields,
            "revision": {"id": work_item_id, "rev": rev, "fields": fields},
            "url": (
                "https://tfs.example.com/tfs/DefaultCollection/"
                f"_apis/wit/workItems/{work_item_id}"
            ),
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            },
            "project": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/Demo/"
            },
        },
    }


def _wi_comment_payload(
    *,
    note: str = "@yaver /planExecute",
    work_item_id: int = 42,
    event: str = "workitem.commented",
    project: str = "Demo",
    state: str = "Active",
    assignee: str = "Yaver",
    unique: str = "DOMAIN\\yaver",
    rev: int = 8,
) -> dict:
    fields = {
        "System.Title": "Do the thing",
        "System.Description": "desc",
        "System.State": state,
        "System.TeamProject": project,
        "System.WorkItemType": "User Story",
        "System.AssignedTo": {
            "displayName": assignee,
            "uniqueName": unique,
            "id": "guid-1",
        },
        "System.History": note,
    }
    changed = {"System.History": {"oldValue": "", "newValue": note}}
    return {
        "eventType": event,
        "resource": {
            "id": work_item_id,
            "workItemId": work_item_id,
            "rev": rev,
            "revisedBy": {
                "displayName": "Alice",
                "uniqueName": "DOMAIN\\alice",
                "id": "user-1",
            },
            "fields": changed,
            "revision": {"id": work_item_id, "rev": rev, "fields": fields},
            "url": (
                "https://tfs.example.com/tfs/DefaultCollection/"
                f"_apis/wit/workItems/{work_item_id}"
            ),
        },
        "resourceContainers": {
            "collection": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/"
            },
            "project": {
                "baseUrl": "https://tfs.example.com/tfs/DefaultCollection/Demo/"
            },
        },
    }


def test_client_loads_pat_from_collection_url_only(monkeypatch):
    from src.azure.client import AzureDevOpsClient
    from src.config import Settings

    s = Settings()
    s.azure_collection_pats = ""
    s.azure_pat = ""
    s.set_azure_collection_pat_map(
        {"https://tfs.example.com/tfs/DefaultCollection": "COL-PAT"}
    )
    monkeypatch.setattr("src.azure.client.settings", s)
    ado = AzureDevOpsClient(
        collection_url="https://tfs.example.com/tfs/DefaultCollection"
    )
    assert ado.host == "tfs.example.com"
    assert ado.pat == "COL-PAT"
    assert ado.api_base == "https://tfs.example.com/tfs/DefaultCollection"


def test_create_scheduled_azure_work_item(tmp_path, monkeypatch):
    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    created = {"id": 99, "rev": 1, "fields": {"System.Title": "New WI"}}
    captured: dict = {}
    assign_calls: list = []

    def _create(self, project, wtype, fields):
        captured["fields"] = dict(fields)
        return created

    monkeypatch.setattr(
        "src.azure.client.AzureDevOpsClient.create_work_item",
        _create,
    )
    monkeypatch.setattr(
        "src.azure.client.AzureDevOpsClient.update_work_item_fields",
        lambda *a, **k: {"ok": True},
    )
    monkeypatch.setattr(
        "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
        lambda *a, **k: assign_calls.append(a) or True,
    )
    monkeypatch.setattr(
        "src.azure.tracker.fetch_pat_myself",
        lambda **_k: {
            "name": "CORP\\Yaver",
            "uniqueName": "CORP\\Yaver",
            "displayName": "Yaver Bot",
            "key": "guid-1",
            "names": ["Yaver Bot", "CORP\\Yaver"],
        },
    )
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    out = create_scheduled_job(
        title="New WI",
        description="do it",
        repository_url="https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2099-01-01T10:00:00",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        azure_project="Demo",
        issue_type="Task",
        store=store,
    )
    assert out["ok"] is True
    assert out["issue_key"] == "WIT-DEMO-99"
    assert captured["fields"]["System.AssignedTo"] == "CORP\\Yaver"
    assert assign_calls


def test_parse_tfs_collection_url():
    from src.azure.urls import parse_tfs_collection_url, require_tfs_collection_url

    assert (
        parse_tfs_collection_url(
            "https://tfs.example.com/tfs/DefaultCollection"
        )
        == "https://tfs.example.com/tfs/DefaultCollection"
    )
    assert (
        parse_tfs_collection_url(
            "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app"
        )
        == "https://tfs.example.com/tfs/DefaultCollection"
    )
    assert parse_tfs_collection_url("tfs.example.com") == ""
    assert parse_tfs_collection_url("https://tfs.example.com") == ""
    assert parse_tfs_collection_url("https://tfs.example.com/tfs") == ""
    assert parse_tfs_collection_url("https://tfs.example.com/tfs/") == ""
    try:
        require_tfs_collection_url("tfs.example.com")
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "collection" in str(exc).lower()


def test_settings_reject_hostname_only_azure(monkeypatch):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    s = Settings()
    s.set_azure_host_pat_map({})
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr(
        "src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None
    )
    try:
        apply_settings_update(
            SettingsUpdate(
                azure_credentials=[{"host": "tfs.example.com", "pat": "x"}]
            )
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "collection" in str(exc).lower()


def test_schedule_preview_azure_work_item():
    from src.scheduler.service import preview_existing_issue

    issue = normalize_work_item(
        project="Demo",
        work_item_id=42,
        fields={
            "System.Title": "Do the thing",
            "System.Description": (
                "{params}\nRepository: https://tfs.example.com/tfs/DefaultCollection/Demo/_git/app\n"
                "Source branch: feature/a\nTarget branch: develop\nMode: build\n{params}"
            ),
            "System.State": "New",
            "System.AssignedTo": {"displayName": "Yaver"},
            "System.WorkItemType": "User Story",
        },
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        host="tfs.example.com",
    )
    with patch(
        "src.azure.workitems.fetch_work_item_issue",
        return_value=issue,
    ):
        out = preview_existing_issue(
            "",
            collection_url="https://tfs.example.com/tfs/DefaultCollection",
            work_item_id=42,
        )
    assert out["ok"] is True
    assert out["issue_key"] == "WIT-DEMO-42"
    assert out["template_valid"] is True
    assert out["mode"] == "build"


def test_schedule_preview_azure_requires_collection():
    from src.azure import workitems as wit
    from src.scheduler.service import preview_existing_issue

    wit._COORDS.pop("WIT-NOCOORDS-1", None)
    out = preview_existing_issue("WIT-NOCOORDS-1")
    assert out["ok"] is False
    assert "collection" in (out.get("error") or "").lower()


def test_schedule_existing_azure_moves_and_assigns(tmp_path):
    from src.scheduler.service import schedule_existing_issue
    from src.state.schedule_store import ScheduleStore

    issue = normalize_work_item(
        project="Demo",
        work_item_id=42,
        fields={
            "System.Title": "Do the thing",
            "System.Description": (
                "{params}\nRepository: https://x/r.git\n"
                "Source branch: a\nTarget branch: develop\nMode: build\n{params}"
            ),
            "System.State": "New",
            "System.WorkItemType": "Bug",
        },
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
    )
    tracker = MagicMock()
    tracker.get_issue.return_value = issue
    tracker.transition_to_in_progress.return_value = True
    tracker.assign_to_pat_user.return_value = True
    tracker.update_issue.return_value = True
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    with patch(
        "src.azure.workitems.fetch_work_item_issue",
        return_value=issue,
    ), patch(
        "src.azure.tracker.azure_tracker_for",
        return_value=tracker,
    ), patch(
        "src.jira.client.assign_to_pat_user",
    ) as assign:
        out = schedule_existing_issue(
            "WIT-DEMO-42",
            scheduled_at="2099-01-01T10:00:00",
            store=store,
        )
    assert out["ok"] is True
    assert out["issue_key"] == "WIT-DEMO-42"
    tracker.transition_to_in_progress.assert_called()
    tracker.assign_to_pat_user.assert_called()
    assign.assert_not_called()
    tracker.add_labels.assert_not_called()


def test_parse_authenticated_user_without_id_uses_unique_name():
    from src.azure.identity import parse_authenticated_user

    ident = parse_authenticated_user(
        {
            "authenticatedUser": {
                "providerDisplayName": "Yaver Bot",
                "uniqueName": r"DOMAIN\yaver",
            }
        }
    )
    assert ident is not None
    assert ident["id"] == ""
    assert r"DOMAIN\yaver" in ident["names"]


def test_parse_authenticated_user_authorized_user():
    from src.azure.identity import parse_authenticated_user

    ident = parse_authenticated_user(
        {
            "authorizedUser": {
                "id": "guid-2",
                "providerDisplayName": "Yaver Bot",
            }
        }
    )
    assert ident is not None
    assert ident["id"] == "guid-2"


def test_resolve_tracker_collection_uses_single_saved_url(monkeypatch):
    from src.azure.tracker import resolve_tracker_collection
    from src.config import Settings

    s = Settings()
    s.azure_collection_pats = (
        '{"https://tfs.example.com/tfs/DefaultCollection":"tok"}'
    )
    monkeypatch.setattr("src.config.settings", s)
    assert (
        resolve_tracker_collection(host="tfs.example.com", collection_url="")
        == "https://tfs.example.com/tfs/DefaultCollection"
    )


def test_fetch_pat_myself_uses_settings_probe_as_last_resort():
    from src.azure.tracker import fetch_pat_myself

    client = MagicMock()
    client.pat = "tok"
    client.connection_user.return_value = None
    client.identity_aliases.return_value = []
    with patch("src.azure.identity.fetch_bot_identity", return_value=None), patch(
        "src.azure_connection.probe_azure_connection",
        return_value={
            "ok": True,
            "user": {
                "id": "guid-9",
                "username": r"DOMAIN\yaver",
                "name": "Yaver Bot",
            },
        },
    ) as probe:
        me = fetch_pat_myself(
            host="tfs.example.com",
            collection_url="https://tfs.example.com/tfs/DefaultCollection",
            pat="tok",
            client=client,
        )
    assert me is not None
    assert me["uniqueName"] == r"DOMAIN\yaver"
    probe.assert_called_once()


def test_azure_client_sends_tfs_fedauth_suppress():
    from src.azure.client import AzureDevOpsClient

    headers = AzureDevOpsClient(host="tfs.example.com", pat="tok")._headers()
    assert headers.get("X-TFS-FedAuthRedirect") == "Suppress"


def test_fetch_pat_myself_falls_back_to_client_connection_user():
    from src.azure.tracker import fetch_pat_myself

    client = MagicMock()
    client.pat = "tok"
    client.connection_user.return_value = {
        "id": "guid-1",
        "names": ["Yaver Bot", r"DOMAIN\yaver"],
    }
    client.identity_aliases.return_value = []
    with patch("src.azure.identity.fetch_bot_identity", return_value=None):
        me = fetch_pat_myself(
            host="tfs.example.com",
            collection_url="https://tfs.example.com/tfs/DefaultCollection",
            pat="tok",
            client=client,
        )
    assert me is not None
    assert me["uniqueName"] == r"DOMAIN\yaver"
    client.connection_user.assert_called_once()


def test_azure_assign_to_pat_user_writes_unique_name():
    from src.azure.tracker import AzureWorkItemTracker

    client = MagicMock()
    client.pat = "tok"
    client.get_work_item.return_value = {"id": 42, "fields": {}}
    client.update_work_item_fields.return_value = {"id": 42}
    client.identity_aliases.return_value = []
    tracker = AzureWorkItemTracker(
        issue_key="42",
        host="tfs.example.com",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project="Demo",
        work_item_id=42,
        client=client,
    )
    with patch(
        "src.azure.identity.fetch_bot_identity",
        return_value={"id": "guid-1", "names": ["Yaver Bot", "CORP\\Yaver"]},
    ):
        assert tracker.assign_to_pat_user("42") is True
    client.update_work_item_fields.assert_called_once()
    fields = client.update_work_item_fields.call_args.args[2]
    assert fields["System.AssignedTo"] == "CORP\\Yaver"


def test_azure_assign_to_pat_user_falls_back_to_identity_id():
    from src.azure.tracker import AzureWorkItemTracker

    client = MagicMock()
    client.pat = "tok"
    client.get_work_item.return_value = {"id": 42, "fields": {}}
    client.update_work_item_fields.side_effect = [None, None, {"id": 42}]
    client.identity_aliases.return_value = []
    tracker = AzureWorkItemTracker(
        issue_key="42",
        host="tfs.example.com",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project="Demo",
        work_item_id=42,
        client=client,
    )
    with patch(
        "src.azure.identity.fetch_bot_identity",
        return_value={"id": "guid-1", "names": ["Yaver Bot"]},
    ):
        assert tracker.assign_to_pat_user("42") is True
    values = [
        call.args[2]["System.AssignedTo"]
        for call in client.update_work_item_fields.call_args_list
    ]
    assert "Yaver Bot" in values
    assert "guid-1" in values


def test_azure_assign_to_pat_user_skips_when_already_pat():
    from src.azure.tracker import AzureWorkItemTracker

    client = MagicMock()
    client.pat = "tok"
    client.get_work_item.return_value = {
        "id": 42,
        "fields": {
            "System.AssignedTo": {
                "displayName": "Yaver Bot",
                "uniqueName": "CORP\\Yaver",
            }
        },
    }
    tracker = AzureWorkItemTracker(
        issue_key="42",
        host="tfs.example.com",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        project="Demo",
        work_item_id=42,
        client=client,
    )
    with patch(
        "src.azure.identity.fetch_bot_identity",
        return_value={"id": "guid-1", "names": ["Yaver Bot", "CORP\\Yaver"]},
    ):
        assert tracker.assign_to_pat_user("42") is True
    client.update_work_item_fields.assert_not_called()


def test_work_item_key_not_pr_key():
    key = azure_work_item_key("Demo", 42)
    assert key == "WIT-DEMO-42"
    assert is_azure_work_item_key(key)
    assert is_azure_work_item_key("WIT-DEMO-42")
    assert is_azure_work_item_key("42")
    assert not is_azure_issue_key(key)
    assert parse_azure_work_item_key(key) == ("DEMO", 42)
    assert parse_azure_work_item_key("WIT-DEMO-42") == ("DEMO", 42)
    assert parse_azure_work_item_key("42") == ("", 42)
    assert not is_azure_work_item_key("AZ-DEMO-42")
    assert not is_azure_work_item_key("0")


def test_work_item_comment_html_jira_wiki():
    from src.azure.comment_html import work_item_comment_html
    from src.brand import USAGE_MARKER, is_yaver_reply

    html = work_item_comment_html(
        "**Yaver 0.9.18 — Plan** · `grok` · `job_1`\n\n"
        "h3. AI Agent — Plan Ready\n\n"
        "*Issue:* WIT-DEMO-1\n"
        "{{noformat}}Mode: build{{noformat}}\n"
        "{{code:markdown}}\n# title\n{{code}}\n"
        "* Review the plan\n"
        "----\n"
        "_Plan generated by the planning agent._\n"
    )
    assert "<h3>AI Agent — Plan Ready</h3>" in html
    assert "<strong>Yaver 0.9.18 — Plan</strong>" in html
    assert "<code>grok</code>" in html
    assert "<strong>Issue:</strong>" in html
    assert "<code>Mode: build</code>" in html
    assert "<pre><code>" in html
    assert "<ul>" in html and "<li>Review the plan</li>" in html
    assert "<hr/>" in html
    assert "<em>Plan generated by the planning agent.</em>" in html
    assert "{{code" not in html
    assert "h3." not in html
    marked = work_item_comment_html(USAGE_MARKER + "\n**Yaver — how to run a command**\n")
    assert marked.startswith(USAGE_MARKER)
    assert is_yaver_reply(marked)
    assert is_yaver_reply(html)


def test_html_and_tags():
    text = azure_html_to_text("<div>Hello<br/>world</div>")
    assert "Hello" in text
    assert "world" in text
    assert parse_azure_tags("plan_ready; bot") == ["plan_ready", "bot"]
    assert classify_workitem_changes(
        {"system.assignedto", "system.watermark"}
    ) == {"assignee"}


def test_parse_assignee_change_payload():
    parsed = parse_workitem_payload(_wi_payload())
    assert parsed is not None
    assert parsed.work_item_id == 42
    assert parsed.project == "Demo"
    assert parsed.issue_key == "WIT-DEMO-42"
    assert "assignee" in parsed.change_kinds
    fields = parsed.issue["fields"]
    assert fields["summary"] == "Do the thing"
    assert "{params}" in fields["description"]
    assert fields["assignee"]["displayName"] == "Yaver"
    assert fields["status"]["name"] == "New"


def test_ignore_pat_user_state_and_assign_updates():
    payload = _wi_payload(
        changed={
            "System.State": {"oldValue": "New", "newValue": "Active"},
            "System.AssignedTo": {
                "oldValue": None,
                "newValue": {
                    "displayName": "Yaver Bot",
                    "uniqueName": "DOMAIN\\yaver",
                    "id": "pat-guid",
                },
            },
        },
        state="Active",
        assignee="Yaver Bot",
        unique="DOMAIN\\yaver",
    )
    payload["resource"]["revisedBy"] = {
        "displayName": "Yaver Bot",
        "uniqueName": "DOMAIN\\yaver",
        "id": "pat-guid",
    }
    with patch(
        "src.azure.workitems.fetch_bot_identity",
        return_value={"id": "pat-guid", "names": ["Yaver Bot", r"DOMAIN\yaver"]},
    ):
        decision = decide_azure_workitem_webhook(payload, enabled=True)
    assert decision.accepted is False
    assert "PAT" in decision.reason


def test_operator_assign_still_accepted():
    payload = _wi_payload()
    payload["resource"]["revisedBy"] = {
        "displayName": "Alice",
        "uniqueName": "DOMAIN\\alice",
        "id": "user-alice",
    }
    with patch(
        "src.azure.workitems.fetch_bot_identity",
        return_value={"id": "pat-guid", "names": ["Yaver Bot", r"DOMAIN\yaver"]},
    ):
        decision = decide_azure_workitem_webhook(payload, enabled=True)
    assert decision.accepted is True


def test_fetch_bot_identity_is_per_collection_pat(monkeypatch):
    from src.azure.identity import fetch_bot_identity, reset_identity_cache
    from src.config import Settings

    reset_identity_cache()
    s = Settings()
    s.set_azure_host_pat_map({})
    s.azure_collection_pats = (
        '{"https://tfs.example.com/tfs/CollA":"pat-a",'
        '"https://tfs.example.com/tfs/CollB":"pat-b"}'
    )
    monkeypatch.setattr("src.config.settings", s)
    seen: list[str] = []

    class _Resp:
        def __init__(self, uid: str):
            self.status_code = 200
            self.content = b"{}"
            self._uid = uid

        def json(self):
            return {
                "authenticatedUser": {
                    "id": self._uid,
                    "providerDisplayName": self._uid,
                    "uniqueName": self._uid,
                }
            }

    class _Client:
        def __init__(self, *a, **k):
            headers = k.get("headers") or {}
            self.auth = headers.get("Authorization") or ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            from src.azure.auth import azure_basic_auth

            if self.auth == azure_basic_auth("pat-a"):
                seen.append("a")
                return _Resp("user-a")
            if self.auth == azure_basic_auth("pat-b"):
                seen.append("b")
                return _Resp("user-b")
            seen.append("other")
            return _Resp("other")

    monkeypatch.setattr("src.azure.identity.httpx.Client", _Client)
    a = fetch_bot_identity(
        collection_url="https://tfs.example.com/tfs/CollA",
        host="tfs.example.com",
    )
    b = fetch_bot_identity(
        collection_url="https://tfs.example.com/tfs/CollB",
        host="tfs.example.com",
    )
    a2 = fetch_bot_identity(
        collection_url="https://tfs.example.com/tfs/CollA",
        host="tfs.example.com",
    )
    reset_identity_cache()
    assert a and a["id"] == "user-a"
    assert b and b["id"] == "user-b"
    assert a2 and a2["id"] == "user-a"
    assert seen == ["a", "b"]


def test_pat_ignore_uses_that_collection_identity(monkeypatch):
    from src.azure.workitems import actor_is_pat_user

    def _ident(*, host="", collection_url="", pat=None):
        if "CollB" in (collection_url or ""):
            return {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "names": ["CollB Bot", r"DOMAIN\collb"],
            }
        return {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "names": ["CollA Bot", r"DOMAIN\colla"],
        }

    monkeypatch.setattr("src.azure.workitems.fetch_bot_identity", _ident)
    actor_b = {
        "key": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "displayName": "CollB Bot",
        "uniqueName": r"DOMAIN\collb",
    }
    assert (
        actor_is_pat_user(
            actor_b,
            host="tfs.example.com",
            collection_url="https://tfs.example.com/tfs/CollB",
        )
        is True
    )
    assert (
        actor_is_pat_user(
            actor_b,
            host="tfs.example.com",
            collection_url="https://tfs.example.com/tfs/CollA",
        )
        is False
    )


def test_ignore_watermark_only_update():
    payload = _wi_payload(
        changed={"System.Watermark": {"oldValue": 1, "newValue": 2}}
    )
    decision = decide_azure_workitem_webhook(payload, enabled=True)
    assert decision.accepted is False
    assert "noise" in decision.reason or "no assignee" in decision.reason


def test_disabled_flags():
    payload = _wi_payload()
    off = decide_azure_workitem_webhook(payload, enabled=False)
    assert off.accepted is False
    assert "disabled" in off.reason


def test_description_and_tag_changes_do_not_start_a_job():
    desc = decide_azure_workitem_webhook(
        _wi_payload(
            changed={
                "System.Description": {"oldValue": "old", "newValue": "new"}
            }
        ),
        enabled=True,
    )
    assert desc.accepted is False
    assert "assignee" in desc.reason
    tags = decide_azure_workitem_webhook(
        _wi_payload(
            tags="plan_execute",
            state="Active",
            changed={
                "System.Tags": {
                    "oldValue": "plan_ready",
                    "newValue": "plan_execute",
                }
            },
        ),
        enabled=True,
    )
    assert tags.accepted is False
    title = decide_azure_workitem_webhook(
        _wi_payload(
            changed={"System.Title": {"oldValue": "a", "newValue": "b"}}
        ),
        enabled=True,
    )
    assert title.accepted is False


def test_state_and_kanban_column_changes_do_not_start_a_job():
    state = decide_azure_workitem_webhook(
        _wi_payload(
            state="Active",
            changed={"System.State": {"oldValue": "New", "newValue": "Active"}},
        ),
        enabled=True,
    )
    assert state.accepted is False
    assert "assignee" in state.reason
    column = decide_azure_workitem_webhook(
        _wi_payload(
            changed={
                "WEF_ABC_Kanban.Column": {
                    "oldValue": "New",
                    "newValue": "Active",
                }
            }
        ),
        enabled=True,
    )
    assert column.accepted is False


def test_intake_new_assigned_todo():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=7,
        fields={
            "System.Title": "X",
            "System.Description": "{params}\nRepository: https://x/r.git\nSource branch: a\nTarget branch: develop\nMode: build\n{params}",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.Tags": "",
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(
        issue, trigger_needles=["yaver"]
    )
    assert decision.action == "accept"
    assert decision.will_process is True
    assert decision.matched_assignee is True
    assert decision.is_todo is True


def test_intake_accepts_new_assigned():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=14,
        fields={
            "System.Title": "X",
            "System.State": "New",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "accept"
    assert decision.will_process is True
    assert decision.is_todo is True


def test_intake_accepts_in_progress_assigned():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=12,
        fields={
            "System.Title": "X",
            "System.Description": (
                "{params}\nRepository: https://x/r.git\n"
                "Source branch: a\nTarget branch: develop\nMode: build\n{params}"
            ),
            "System.State": "In Progress",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "accept"
    assert decision.will_process is True
    assert decision.is_todo is False
    assert decision.is_done is False


def test_intake_accepts_active_assigned():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=15,
        fields={
            "System.Title": "X",
            "System.State": "Active",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "accept"
    assert decision.will_process is True
    assert decision.is_todo is False
    assert decision.is_done is False


def test_intake_skips_resolved_assigned():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=16,
        fields={
            "System.Title": "X",
            "System.State": "Resolved",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "skip"
    assert decision.reason == "not todo or in progress"
    assert decision.is_done is False


def test_intake_skips_done_assigned():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=13,
        fields={
            "System.Title": "X",
            "System.State": "Done",
            "System.AssignedTo": {"displayName": "Yaver", "uniqueName": "yaver"},
            "System.WorkItemType": "Bug",
        },
    )
    decision = evaluate_work_item_intake(issue, trigger_needles=["yaver"])
    assert decision.action == "skip"
    assert decision.is_done is True
    assert decision.reason == "done"


def test_intake_skips_in_flight_and_plan_ready():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=8,
        fields={
            "System.Title": "X",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
        },
    )
    flying = MagicMock()
    flying.status = TaskStatus.EXECUTING
    flying.metadata = {}
    skip = evaluate_work_item_intake(
        issue, state=flying, trigger_needles=["yaver"]
    )
    assert skip.action == "skip"
    assert "in-flight" in skip.reason

    waiting = MagicMock()
    waiting.status = TaskStatus.PLAN_READY
    waiting.metadata = {}
    wait = evaluate_work_item_intake(
        issue, state=waiting, trigger_needles=["yaver"]
    )
    assert wait.action == "skip"
    assert "plan_ready" in wait.reason

    tagged = normalize_work_item(
        project="Demo",
        work_item_id=8,
        fields={
            "System.Title": "X",
            "System.State": "In Progress",
            "System.AssignedTo": {"displayName": "Yaver"},
            "System.Tags": "plan_execute",
            "System.WorkItemType": "User Story",
        },
        state_category="indeterminate",
    )
    handoff = evaluate_work_item_intake(
        tagged, state=waiting, trigger_needles=["yaver"]
    )
    assert handoff.action == "skip"
    assert handoff.plan_handoff == ""


def test_intake_does_not_requeue_active_to_new():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=9,
        fields={
            "System.Title": "X",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
        },
    )
    done = MagicMock()
    done.status = TaskStatus.COMPLETED
    done.metadata = {"last_board_status": "active"}
    skip_done = evaluate_work_item_intake(
        issue, state=done, prev_status="active", trigger_needles=["yaver"]
    )
    assert skip_done.action == "skip"

    err = MagicMock()
    err.status = TaskStatus.ERROR
    err.metadata = {"requeue_eligible": True}
    skip_err = evaluate_work_item_intake(
        issue, state=err, prev_status="active", trigger_needles=["yaver"]
    )
    assert skip_err.action == "skip"

    err.metadata = {
        "requeue_eligible": True,
        "last_intake_fingerprint": "old-hash",
    }
    changed = normalize_work_item(
        project="Demo",
        work_item_id=9,
        fields={
            "System.Title": "Fixed title",
            "System.Description": "{params}\nMode: build\n{params}",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
        },
    )
    text = evaluate_work_item_intake(
        changed, state=err, trigger_needles=["yaver"]
    )
    assert text.action == "accept"
    assert text.reason == "issue text changed"


def test_intake_trigger_label_and():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=10,
        fields={
            "System.Title": "X",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
            "System.Tags": "other",
        },
    )
    decision = evaluate_work_item_intake(
        issue, trigger_needles=["yaver"], required_labels=["bot"]
    )
    assert decision.action == "skip"
    tagged = normalize_work_item(
        project="Demo",
        work_item_id=10,
        fields={
            "System.Title": "X",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
            "System.Tags": "bot",
        },
    )
    ok = evaluate_work_item_intake(
        tagged, trigger_needles=["yaver"], required_labels=["bot"]
    )
    assert ok.action == "accept"


def test_lookup_view_flags():
    issue = normalize_work_item(
        project="Demo",
        work_item_id=11,
        fields={
            "System.Title": "Lookup me",
            "System.State": "To Do",
            "System.AssignedTo": {"displayName": "Yaver"},
            "System.Tags": "",
        },
    )
    view = lookup_work_item_view(issue, trigger_needles=["yaver"])
    assert view["ok"] is True
    assert view["issue_key"] == "WIT-DEMO-11"
    assert view["will_process"] is True
    assert view["matched_assignee"] is True


def test_http_workitem_webhook_enqueues(tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr(
        "src.config.settings.azure_trigger_user", "yaver"
    )
    app = create_dashboard_app()
    proc = MagicMock()
    proc.ingest_azure_work_item = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "42",
            "queue_id": "q1",
            "status": "queued",
            "reason": "new",
        }
    )
    app.state.processor = proc
    client = TestClient(app)
    resp = client.post("/yaver/webhook/azure", json=_wi_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "work_item"
    assert body["issue_key"] == "42"
    proc.ingest_azure_work_item.assert_awaited_once()


def test_http_workitem_follows_azure_webhook_enabled(tmp_path, monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", False)
    app = create_dashboard_app()
    app.state.processor = MagicMock()
    client = TestClient(app)
    resp = client.post("/yaver/webhook/azure", json=_wi_payload())
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "disabled" in resp.json()["reason"]


def test_workitem_client_uses_7_1_then_7_0(monkeypatch):
    from src.azure.client import AzureDevOpsClient

    calls = []

    class _Resp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self.content = b"{}" if payload is not None else b""
            self.text = ""
            self._payload = payload or {}

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            calls.append(params.get("api-version") if params else None)
            if params and params.get("api-version") == "7.1":
                return _Resp(404)
            return _Resp(
                200,
                {"id": 5, "rev": 2, "fields": {"System.Title": "Hi"}},
            )

    monkeypatch.setattr("src.azure.client.httpx.Client", _Client)
    ado = AzureDevOpsClient(
        host="tfs.example.com",
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        pat="tok",
    )
    got = ado.get_work_item("Demo", 5)
    assert got and got["id"] == 5
    assert calls == ["7.1", "7.0"]


def test_ingest_uses_jira_event_path():
    from src.azure.workitems import parse_workitem_payload
    from src.processor import JobProcessor

    parsed = parse_workitem_payload(_wi_payload(state="To Do"))
    assert parsed is not None
    proc = JobProcessor.__new__(JobProcessor)
    proc.state_manager = MagicMock()
    proc.state_manager.get_state.return_value = None
    proc.enqueue_jira_event = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "WIT-DEMO-42",
            "status": "queued",
        }
    )

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ) as moved, patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=True,
        ) as assigned, patch(
            "src.config.settings.azure_trigger_user",
            "yaver",
        ):
            out = await proc.ingest_azure_work_item(parsed)
            return out, moved, assigned

    import asyncio

    result, moved, assigned = asyncio.run(_run())
    assert result["ok"] is True
    assert result["kind"] == "work_item"
    moved.assert_called()
    assigned.assert_called()
    proc.enqueue_jira_event.assert_awaited_once()
    event = proc.enqueue_jira_event.await_args.args[0]
    assert event["webhookEvent"] == "jira:issue_created"
    assert event["issue"]["key"] == "WIT-DEMO-42"


def test_settings_save_ignores_azure_trigger_label(monkeypatch):
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    s = Settings()
    s.azure_trigger_label = "leftover"
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr(
        "src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None
    )
    view = apply_settings_update(SettingsUpdate(azure_trigger_user="yaver"))
    assert s.azure_trigger_label_list == []
    assert view.azure_trigger_label == ""


def test_lookup_endpoint(monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr(
        "src.azure.workitems.lookup_azure_work_item",
        lambda **kwargs: {
            "ok": True,
            "issue_key": "WIT-DEMO-12",
            "summary": "Looked up",
            "will_process": True,
            "matched_assignee": True,
            "is_todo": True,
        },
    )
    app = create_dashboard_app()
    app.state.processor = MagicMock()
    client = TestClient(app)
    resp = client.post(
        "/api/azure/work-item",
        json={
            "collection_url": "https://tfs.example.com/tfs/DefaultCollection",
            "work_item_id": 12,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["issue_key"] == "WIT-DEMO-12"


def test_http_schedule_preview_azure_work_item(monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr(
        "src.dashboard.api.preview_existing_issue",
        lambda issue_key="", collection_url="", work_item_id=0, **_k: {
            "ok": True,
            "issue_key": "WIT-DEMO-12",
            "title": "Looked up",
            "template_valid": True,
            "repository_url": "https://x/r.git",
            "source_branch": "a",
            "target_branch": "develop",
            "mode": "build",
            "azure": True,
        },
    )
    app = create_dashboard_app()
    app.state.processor = MagicMock()
    client = TestClient(app)
    resp = client.get(
        "/api/schedules/preview",
        params={
            "collection_url": "https://tfs.example.com/tfs/DefaultCollection",
            "work_item_id": 12,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["issue_key"] == "WIT-DEMO-12"


def test_plan_commands_only_on_work_item_comments():
    assert workitem_plan_command("@yaver /planExecute", ["yaver"]) == "execute"
    assert (
        workitem_plan_command("@yaver /planRefactor tighten the API", ["yaver"])
        == "refactor"
    )
    assert workitem_plan_command("@yaver /yaver do it", ["yaver"]) == ""
    assert workitem_plan_command("@yaver please", ["yaver"]) == ""
    note = format_workitem_plan_usage_note("yaver")
    assert "/planExecute" in note
    assert "/planRefactor" in note
    assert "/yaver" not in note


def test_comment_event_plan_execute():
    payload = _wi_comment_payload()
    assert is_azure_workitem_comment_event(payload) is True
    decision = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert decision.accepted is True
    assert decision.event.plan_handoff == "execute"


def test_ingest_plan_execute_assigns_pat_user():
    from src.azure.workitems import parse_workitem_payload
    from src.processor import JobProcessor
    from src.state.models import TaskStatus

    parsed = parse_workitem_payload(_wi_comment_payload())
    assert parsed is not None
    parsed.plan_handoff = "execute"
    proc = JobProcessor.__new__(JobProcessor)
    proc.state_manager = MagicMock()
    ready = MagicMock()
    ready.status = TaskStatus.PLAN_READY
    ready.metadata = {}
    proc.state_manager.get_state.return_value = ready
    proc.enqueue_jira_event = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "42",
            "status": "queued",
        }
    )

    async def _run():
        with patch(
            "src.azure.workitems.fetch_work_item_issue",
            return_value=parsed.issue,
        ), patch(
            "src.azure.tracker.AzureWorkItemTracker.transition_to_in_progress",
            return_value=True,
        ) as moved, patch(
            "src.azure.tracker.AzureWorkItemTracker.assign_to_pat_user",
            return_value=True,
        ) as assigned:
            out = await proc.ingest_azure_work_item(parsed)
            return out, moved, assigned

    import asyncio

    result, moved, assigned = asyncio.run(_run())
    assert result["ok"] is True
    moved.assert_called()
    assigned.assert_called()


def test_comment_event_plan_refactor_prompt():
    payload = _wi_comment_payload(note="@yaver /planRefactor drop the cache")
    decision = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert decision.accepted is True
    assert decision.event.plan_handoff == "refactor"
    assert "drop the cache" in decision.event.plan_comment


def test_comment_and_history_update_post_usage_once():
    payload_comment = _wi_comment_payload(
        note="@yaver please", event="workitem.commented", rev=8
    )
    payload_hist = _wi_comment_payload(
        note="@yaver please", event="workitem.updated", rev=8
    )
    first = decide_azure_workitem_comment_webhook(
        payload_comment, enabled=True, bot_mentions=["yaver"]
    )
    second = decide_azure_workitem_comment_webhook(
        payload_hist, enabled=True, bot_mentions=["yaver"]
    )
    assert first.usage_note is True
    assert second.accepted is False
    assert second.usage_note is False
    assert "duplicate" in second.reason


def test_comment_mention_without_command_usage_note():
    payload = _wi_comment_payload(note="@yaver please run this")
    decision = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert decision.accepted is False
    assert decision.usage_note is True
    assert "planRefactor" in decision.reason or "planExecute" in decision.reason


def test_comment_ask_is_silent():
    payload = _wi_comment_payload(note="@yaver /ask what is this")
    decision = decide_azure_workitem_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver"]
    )
    assert decision.accepted is False
    assert decision.usage_note is False


def test_history_only_update_is_comment_not_description():
    payload = _wi_comment_payload(event="workitem.updated")
    assert is_azure_workitem_comment_event(payload) is True
    update = decide_azure_workitem_webhook(payload, enabled=True)
    assert update.accepted is False
    assert "comment" in update.reason


def test_http_workitem_comment_usage_note(monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    app = create_dashboard_app()
    app.state.processor = MagicMock()
    posted = {"n": 0}

    def _post(event, bot_name=""):
        posted["n"] += 1
        assert "/planExecute" in format_workitem_plan_usage_note(bot_name)
        return True

    monkeypatch.setattr(
        "src.azure.workitems.post_azure_workitem_usage_note", _post
    )
    client = TestClient(app)
    resp = client.post(
        "/yaver/webhook/azure",
        json=_wi_comment_payload(note="@yaver hello"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body.get("usage_note") is True
    assert posted["n"] == 1


def test_http_workitem_plan_execute_enqueues(monkeypatch):
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_trigger_user", "yaver")
    app = create_dashboard_app()
    proc = MagicMock()
    proc.ingest_azure_work_item = AsyncMock(
        return_value={
            "ok": True,
            "queued": True,
            "started": False,
            "issue_key": "WIT-DEMO-42",
            "queue_id": "q2",
            "status": "queued",
            "reason": "execute",
        }
    )
    app.state.processor = proc
    client = TestClient(app)
    resp = client.post("/yaver/webhook/azure", json=_wi_comment_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["kind"] == "work_item_comment"
    proc.ingest_azure_work_item.assert_awaited_once()


def test_work_item_description_html_keeps_params_newlines():
    from src.azure.comment_html import work_item_description_html
    from src.scheduler.service import build_issue_description

    text = build_issue_description(
        description="Do it",
        repository_url="https://gitlab.com/a/b",
        source_branch="develop",
        target_branch="develop",
        mode="build",
    )
    html = work_item_description_html(text)
    assert "<pre>" in html
    assert "{params}" in html
    assert "Repository:" in html
    assert "<br/>" in html or "\n" in html


def test_list_projects_pages_until_exhausted():
    from src.azure.client import AzureDevOpsClient

    first = [{"name": f"P{i}"} for i in range(200)]
    second = [{"name": "Last"}]

    def _get(self, url, params=None, **_k):
        skip = int((params or {}).get("$skip") or 0)
        return {"value": first} if skip == 0 else {"value": second}

    client = AzureDevOpsClient(
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        pat="tok",
    )
    with patch.object(AzureDevOpsClient, "_get_json", _get):
        names = client.list_projects()
    assert names[0] == "P0"
    assert names[-1] == "Last"
    assert len(names) == 201
