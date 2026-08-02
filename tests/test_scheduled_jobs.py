"""Scheduled jobs: store, create (Jira hard-fail), dispatch."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.scheduler.service import (
    build_issue_description,
    cancel_scheduled_job,
    create_scheduled_job,
    dispatch_due_schedules,
    parse_schedule_at,
)
from src.state.manager import JiraStateManager
from src.state.schedule_store import SCHEDULE_LABEL, ScheduleStore


def test_build_issue_description_includes_params():
    text = build_issue_description(
        description="Do the thing",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="build",
    )
    assert "Do the thing" in text
    assert "{params}" in text
    assert "Repository: https://gitlab.com/a/b.git" in text
    assert "Source branch: feature/x" in text
    assert "Target branch: develop" in text
    assert "Mode: build" in text


def test_parse_schedule_at_variants():
    assert parse_schedule_at("2026-08-03T15:00:00").year == 2026
    assert parse_schedule_at("2026-08-03 15:00:00").hour == 15
    with pytest.raises(ValueError):
        parse_schedule_at("")


def test_schedule_store_claim_and_due(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    future = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    a = store.create(
        title="past",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-1",
        issue_description="x",
    )
    store.create(
        title="future",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        scheduled_at=future,
        issue_key="KAN-2",
        issue_description="y",
    )
    due = store.list_due()
    assert len(due) == 1
    assert due[0]["schedule_id"] == a["schedule_id"]

    claimed = store.claim_due(a["schedule_id"])
    assert claimed is not None
    assert claimed["status"] == "dispatching"
    assert store.claim_due(a["schedule_id"]) is None


def test_create_scheduled_job_hard_fails_without_issue(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = None
    out = create_scheduled_job(
        title="T",
        description="D",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is False
    assert "Failed to create Jira issue" in out["error"]
    assert "Schedule was not saved" in out["error"]
    assert store.list_schedules() == []
    client.transition_to_in_progress.assert_not_called()


def test_create_scheduled_job_soft_transition_and_saves(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-99", "id": "1"}
    client.transition_to_in_progress.side_effect = RuntimeError("jira down")

    out = create_scheduled_job(
        title="Title",
        description="Body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="feature/x",
        target_branch="develop",
        mode="plan",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["issue_key"] == "KAN-99"
    assert rec["mode"] == "plan"
    assert rec["status"] == "scheduled"
    kwargs = client.create_issue.call_args.kwargs
    assert SCHEDULE_LABEL in (kwargs.get("labels") or [])


@pytest.mark.asyncio
async def test_dispatch_due_schedules(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    past = (datetime.now() - timedelta(seconds=30)).isoformat(timespec="seconds")
    rec = store.create(
        title="Go",
        description="d",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=past,
        issue_key="KAN-10",
        issue_description=build_issue_description(
            description="d",
            repository_url="https://gitlab.com/a/b.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
        ),
    )
    processor = MagicMock()
    processor.process_event = AsyncMock()

    result = await dispatch_due_schedules(
        processor=processor,
        store=store,
        jira_client=None,
    )
    assert result["started"] == 1
    processor.process_event.assert_awaited_once()
    event = processor.process_event.await_args.args[0]
    assert event["webhookEvent"] == "jira:issue_created"
    assert event["issue"]["key"] == "KAN-10"
    assert event["scheduled_job"] is True

    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "dispatched"


def test_cancel_scheduled_job(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="c",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2026-12-01T00:00:00",
        issue_key="KAN-3",
        issue_description="x",
    )
    out = cancel_scheduled_job(rec["schedule_id"], store=store)
    assert out["ok"] is True
    assert store.get(rec["schedule_id"])["status"] == "cancelled"


def test_preview_existing_issue_valid_template():
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-20",
        "fields": {
            "summary": "Existing feature",
            "description": (
                "Do work\n\n"
                "{params}\n"
                "Repository: https://gitlab.com/a/b.git\n"
                "Source branch: develop\n"
                "Target branch: main\n"
                "Mode: build\n"
                "{params}"
            ),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Story"},
            "labels": [],
        },
    }
    from src.scheduler.service import preview_existing_issue

    out = preview_existing_issue("kan-20", jira_client=client)
    assert out["ok"] is True
    assert out["template_valid"] is True
    assert out["mode"] == "build"
    assert out["repository_url"].endswith("b.git")
    assert out["issue_type"] == "Story"


def test_preview_existing_issue_invalid_template():
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-21",
        "fields": {
            "summary": "No params",
            "description": "just text",
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
        },
    }
    from src.scheduler.service import preview_existing_issue

    out = preview_existing_issue("KAN-21", jira_client=client)
    assert out["ok"] is False
    assert "params" in out["error"].lower() or "template" in out["error"].lower() or "could not" in out["error"].lower()


def test_schedule_existing_issue(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.get_issue.return_value = {
        "key": "KAN-22",
        "fields": {
            "summary": "Existing",
            "description": (
                "{params}\n"
                "Repository: https://gitlab.com/a/b.git\n"
                "Source branch: develop\n"
                "Target branch: develop\n"
                "Mode: plan\n"
                "{params}"
            ),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": ["ai-assist"],
        },
    }
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True

    from src.scheduler.service import schedule_existing_issue

    out = schedule_existing_issue(
        "KAN-22",
        scheduled_at="2026-12-01T10:00:00",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["source"] == "existing"
    assert rec["issue_key"] == "KAN-22"
    assert rec["mode"] == "plan"
    client.create_issue.assert_not_called()
    client.transition_to_in_progress.assert_called()
    client.add_labels.assert_called()

    # Duplicate pending rejected
    out2 = schedule_existing_issue(
        "KAN-22",
        scheduled_at="2026-12-02T10:00:00",
        jira_client=client,
        store=store,
    )
    assert out2["ok"] is False
    assert "already has a pending schedule" in out2["error"]


def test_create_with_custom_issue_type(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-77"}
    client.transition_to_in_progress.return_value = True

    out = create_scheduled_job(
        title="Bugfix",
        description="x",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at="2026-12-01T10:00:00",
        project_key="KAN",
        issue_type="ExtBug",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["issue_type"] == "ExtBug"
    assert client.create_issue.call_args.kwargs["issue_type"] == "ExtBug"


def test_api_schedules(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")

    client_mock = MagicMock()
    client_mock.create_issue.return_value = {"key": "KAN-55"}
    client_mock.transition_to_in_progress.return_value = True
    client_mock.get_project_issue_types.return_value = [
        {"id": "1", "name": "Task", "subtask": False},
        {"id": "2", "name": "Story", "subtask": False},
        {"id": "3", "name": "ExtBug", "subtask": False},
        {"id": "4", "name": "Sub-task", "subtask": True},
    ]

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.schedule_store", store)
        m.setattr(
            "src.scheduler.service.create_jira_client",
            lambda: client_mock,
            raising=False,
        )
        # create_scheduled_job imports create_jira_client inside when client None —
        # pass via patching create_scheduled_job path by injecting client through
        # the module used by API (it calls create_scheduled_job without client).
        m.setattr(
            "src.dashboard.api.create_scheduled_job",
            lambda **kwargs: create_scheduled_job(
                **kwargs, jira_client=client_mock, store=store
            ),
        )
        m.setattr(
            "src.dashboard.api.list_project_issue_types",
            lambda **kwargs: {
                "ok": True,
                "project_key": "KAN",
                "issue_types": [
                    {"id": "1", "name": "Task", "subtask": False},
                    {"id": "3", "name": "ExtBug", "subtask": False},
                ],
            },
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)

        r_types = tc.get("/api/jira/issue-types")
        assert r_types.status_code == 200
        assert any(t["name"] == "ExtBug" for t in r_types.json()["issue_types"])

        r_list = tc.get("/api/schedules")
        assert r_list.status_code == 200
        assert r_list.json()["total"] == 0

        r = tc.post(
            "/api/schedules",
            json={
                "title": "From API",
                "description": "desc",
                "repository_url": "https://gitlab.com/org/repo.git",
                "source_branch": "develop",
                "target_branch": "main",
                "mode": "build",
                "issue_type": "ExtBug",
                "scheduled_at": "2026-12-25T12:00:00",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        sid = body["schedule"]["schedule_id"]
        assert body["issue_key"] == "KAN-55"
        assert body["schedule"]["issue_type"] == "ExtBug"

        r2 = tc.get("/api/schedules")
        assert r2.json()["total"] == 1

        r3 = tc.post(f"/api/schedules/{sid}/cancel")
        assert r3.status_code == 200
        assert r3.json()["schedule"]["status"] == "cancelled"

        # Preview + schedule existing
        client_mock.get_issue.return_value = {
            "key": "KAN-88",
            "fields": {
                "summary": "From board",
                "description": (
                    "{params}\n"
                    "Repository: https://gitlab.com/org/repo.git\n"
                    "Source branch: develop\n"
                    "Target branch: develop\n"
                    "Mode: build\n"
                    "{params}"
                ),
                "status": {"name": "To Do"},
                "issuetype": {"name": "ExtBug"},
                "labels": [],
            },
        }
        m.setattr(
            "src.dashboard.api.preview_existing_issue",
            lambda issue_key: __import__(
                "src.scheduler.service", fromlist=["preview_existing_issue"]
            ).preview_existing_issue(issue_key, jira_client=client_mock),
        )
        m.setattr(
            "src.dashboard.api.schedule_existing_issue",
            lambda issue_key, scheduled_at, store=None: __import__(
                "src.scheduler.service", fromlist=["schedule_existing_issue"]
            ).schedule_existing_issue(
                issue_key,
                scheduled_at=scheduled_at,
                jira_client=client_mock,
                store=store,
            ),
        )
        r_prev = tc.get("/api/schedules/preview", params={"issue_key": "KAN-88"})
        assert r_prev.status_code == 200, r_prev.text
        assert r_prev.json()["template_valid"] is True

        r_ex = tc.post(
            "/api/schedules/from-issue",
            json={"issue_key": "KAN-88", "scheduled_at": "2026-12-26T08:00:00"},
        )
        assert r_ex.status_code == 200, r_ex.text
        assert r_ex.json()["schedule"]["source"] == "existing"
