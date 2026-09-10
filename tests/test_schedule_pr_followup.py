"""Scheduled follow-up prompt on an existing Azure DevOps PR."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.processor import JobProcessor
from src.scheduler.service import (
    _dispatch_pr_followup,
    format_dashboard_mr_prompt_note,
    preview_pr_followup,
    schedule_pr_followup,
)
from src.state.schedule_store import ScheduleStore


AZURE_REPO = "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
AZURE_PR = {
    "pullRequestId": 4,
    "title": "feat(KAN-12): add login",
    "description": "desc",
    "status": "active",
    "sourceRefName": "refs/heads/feature/login",
    "targetRefName": "refs/heads/develop",
    "repository": {
        "id": "repo-guid",
        "name": "demo",
        "remoteUrl": AZURE_REPO,
        "project": {"name": "Demo"},
    },
    "_links": {
        "web": {
            "href": (
                "https://tfs.example.com/tfs/DefaultCollection/"
                "Demo/_git/demo/pullrequest/4"
            )
        }
    },
}


def _patch_get_pr(monkeypatch, payload=None, missing=False):
    def fake(self, project, repository, pr_id):
        if missing:
            return None
        return payload if payload is not None else AZURE_PR

    monkeypatch.setattr(
        "src.azure.client.AzureDevOpsClient.get_pull_request", fake
    )
    monkeypatch.setattr(
        "src.azure_connection.remember_azure_collection", lambda *a, **k: None
    )


def test_preview_pr_followup_from_git_url(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    _patch_get_pr(monkeypatch)
    preview = preview_pr_followup(AZURE_REPO, 4)
    assert preview["ok"] is True
    assert preview["pr_id"] == 4
    assert preview["azure_project"] == "Demo"
    assert preview["azure_repository"] == "demo"
    assert preview["source_branch"] == "feature/login"
    assert preview["target_branch"] == "develop"
    assert preview["issue_key"] == "KAN-12"
    assert preview["azure_host"] == "tfs.example.com"


def test_preview_pr_followup_from_pr_page_url(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    _patch_get_pr(monkeypatch)
    preview = preview_pr_followup(
        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/4",
        0,
    )
    assert preview["ok"] is True
    assert preview["pr_id"] == 4


def test_preview_pr_followup_missing_pr(monkeypatch):
    _patch_get_pr(monkeypatch, missing=True)
    preview = preview_pr_followup(AZURE_REPO, 99)
    assert preview["ok"] is False
    assert "Could not load" in (preview.get("error") or "")


def test_preview_pr_followup_needs_azure_url():
    preview = preview_pr_followup("https://gitlab.com/acme/demo.git", 4)
    assert preview["ok"] is False
    assert "Azure" in (preview.get("error") or "")


def test_schedule_pr_followup_persists_azure_source(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    _patch_get_pr(monkeypatch)
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    result = schedule_pr_followup(
        repository_url=AZURE_REPO,
        pr_id=4,
        prompt="Please add a log line when login fails.",
        scheduled_at=when,
        store=store,
    )
    assert result["ok"] is True
    rec = result["schedule"]
    assert rec["source"] == "azure_pr"
    assert rec["pr_id"] == 4
    assert rec["azure_project"] == "Demo"
    assert rec["azure_repository"] == "demo"
    assert rec["status"] == "scheduled"
    assert "log line" in rec["description"]


@pytest.mark.asyncio
async def test_dispatch_pr_followup_posts_overview_and_enqueues(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    _patch_get_pr(monkeypatch)
    posted = {}

    def fake_post(self, **kwargs):
        posted.update(kwargs)
        return {"id": 8, "comments": [{"id": 1, "content": kwargs.get("body")}]}

    monkeypatch.setattr(
        "src.azure.client.AzureDevOpsClient.post_pr_comment", fake_post
    )
    proc = MagicMock()
    proc.enqueue_azure_comment = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": "KAN-12"}
    )
    rec = {
        "issue_description": "Please add a log line.",
        "azure_host": "tfs.example.com",
        "azure_collection_url": "https://tfs.example.com/tfs/DefaultCollection",
        "azure_project": "Demo",
        "azure_repository": "demo",
        "azure_repository_id": "repo-guid",
        "pr_id": 4,
        "repository_url": AZURE_REPO,
        "source_branch": "feature/login",
        "target_branch": "develop",
        "merge_request_url": (
            "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo/pullrequest/4"
        ),
        "issue_key": "KAN-12",
        "model": "x",
        "backend": "opencode",
    }
    out = await _dispatch_pr_followup(proc, rec)
    assert out["ok"] is True
    assert posted.get("allow_new_thread") is True
    assert posted.get("pr_id") == 4
    assert "*Yaver* — written in the ops dashboard" in (posted.get("body") or "")
    assert "Please add a log line." in (posted.get("body") or "")
    proc.enqueue_azure_comment.assert_awaited_once()
    event = proc.enqueue_azure_comment.await_args.args[0]
    assert event.pr_id == 4
    assert event.thread_id == "8"
    assert event.comment_id == "1"
    assert event.prompt == "Please add a log line."
    assert event.raw.get("backend") == "opencode"


def test_dashboard_pr_schedule_endpoints(tmp_path, monkeypatch, fake_jira):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    _patch_get_pr(monkeypatch)
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    monkeypatch.setattr("src.dashboard.api.schedule_store", store)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    app = create_dashboard_app(processor=proc)
    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    with TestClient(app) as client:
        preview = client.get(
            "/api/schedules/pr-preview",
            params={"repository_url": AZURE_REPO, "pr_id": 4},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["ok"] is True
        assert body["pr_id"] == 4
        created = client.post(
            "/api/schedules/pr",
            json={
                "repository_url": AZURE_REPO,
                "pr_id": 4,
                "prompt": "Please add a log line.",
                "scheduled_at": when,
            },
        )
    assert created.status_code == 200
    payload = created.json()
    assert payload["ok"] is True
    assert payload["schedule"]["source"] == "azure_pr"
    assert payload["schedule"]["pr_id"] == 4


def test_dashboard_prompt_note_skipped_as_bot_reply():
    from src.azure.webhook import decide_azure_comment_webhook

    body = format_dashboard_mr_prompt_note("Please add a log line.")
    d = decide_azure_comment_webhook(
        {
            "eventType": "ms.vss-code.git-pullrequest-comment-event",
            "resource": {
                "comment": {"id": 1, "content": body, "author": {"displayName": "Yaver"}},
                "pullRequest": {
                    "pullRequestId": 4,
                    "title": "x",
                    "sourceRefName": "refs/heads/a",
                    "targetRefName": "refs/heads/b",
                    "repository": {
                        "id": "r",
                        "name": "demo",
                        "remoteUrl": AZURE_REPO,
                        "project": {"name": "Demo"},
                    },
                },
            },
        },
        bot_mentions=["yaver"],
    )
    assert d.accepted is False
    assert "bot reply" in (d.reason or "")
