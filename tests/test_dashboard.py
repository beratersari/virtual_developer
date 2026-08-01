"""Dashboard API and poll snapshot tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view, read_app_version
from src.dashboard.snapshot import PollSnapshotStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


@pytest.fixture
def store():
    return PollSnapshotStore()


def test_snapshot_countdown(store):
    store.begin_poll(board_id="1", interval_seconds=30)
    store.end_poll(
        source="sprint x",
        issues=[
            {
                "key": "A-1",
                "summary": "s",
                "jira_status": "To Do",
                "labels": ["ai-assist"],
                "assignee": "Jira AI Bot",
                "matched_label": True,
                "matched_assignee": True,
                "is_todo": True,
                "will_process": True,
                "matched_labels": ["ai-assist"],
            }
        ],
        interval_seconds=30,
    )
    snap = store.snapshot()
    assert snap["phase"] == "waiting"
    assert snap["matched_count"] == 1
    assert snap["will_process_count"] == 1
    assert snap["seconds_until_next_poll"] is not None
    assert 0 <= snap["seconds_until_next_poll"] <= 30


def test_settings_view_hides_secrets(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_api_token", "super-secret")
    monkeypatch.setattr(settings, "gitlab_pat", "pat-secret")
    view = build_settings_view()
    dumped = view.model_dump()
    assert "super-secret" not in str(dumped)
    assert "pat-secret" not in str(dumped)
    assert view.jira_token_configured is True
    assert view.gitlab_pat_configured is True


def test_apply_settings_update_runtime(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "poll_interval_seconds", 30)
    monkeypatch.setattr(settings, "jira_board_id", "1")
    view = apply_settings_update(
        SettingsUpdate(poll_interval_seconds=45, jira_board_id="99")
    )
    assert view.poll_interval_seconds == 45
    assert view.jira_board_id == "99"
    assert settings.poll_interval_seconds == 45


def test_api_tasks_and_poll(tmp_path, monkeypatch):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("T-1", "summary", "d")
    sm.update_state("T-1", status=TaskStatus.EXECUTING, progress_percentage=40)

    store = PollSnapshotStore()
    store.end_poll(
        source="board 1",
        issues=[
            {
                "key": "T-1",
                "summary": "summary",
                "jira_status": "To Do",
                "labels": ["ai-assist"],
                "assignee": None,
                "matched_label": True,
                "matched_assignee": False,
                "is_todo": True,
                "will_process": False,
                "matched_labels": ["ai-assist"],
            }
        ],
        interval_seconds=30,
    )

    with patch("src.dashboard.api.poll_snapshot_store", store):
        with patch("src.dashboard.service.poll_snapshot_store", store):
            app = create_dashboard_app(processor=None, state_manager=sm)
            client = TestClient(app)
            r = client.get("/api/tasks")
            assert r.status_code == 200
            body = r.json()
            assert body["total"] >= 1
            assert any(t["issue_key"] == "T-1" for t in body["tasks"])

            p = client.get("/api/poll")
            assert p.status_code == 200
            poll = p.json()
            assert poll["issues"][0]["matched_label"] is True
            assert "seconds_until_next_poll" in poll

            d = client.get("/api/dashboard")
            assert d.status_code == 200
            assert "meta" in d.json()
            assert "version" in d.json()["meta"]


def test_api_settings_patch(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "poll_interval_seconds", 30)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)
    r = client.patch("/api/settings", json={"poll_interval_seconds": 60})
    assert r.status_code == 200
    assert r.json()["poll_interval_seconds"] == 60


def test_read_app_version():
    v = read_app_version()
    assert isinstance(v, str)
    assert len(v) > 0


def test_poller_publishes_snapshot(fake_jira, state_manager, monkeypatch):
    from src.jira.poller import JiraPoller
    from src.dashboard import snapshot as snap_mod

    store = PollSnapshotStore()
    monkeypatch.setattr(snap_mod, "poll_snapshot_store", store)
    monkeypatch.setattr("src.jira.poller.poll_snapshot_store", store)

    issue = {
        "key": "P-9",
        "fields": {
            "summary": "fix",
            "labels": ["ai-assist"],
            "assignee": {"displayName": "Alice"},
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
        },
    }
    fake_jira.get_active_sprint = lambda b: None
    fake_jira.get_board_issues = lambda *a, **k: [issue]
    fake_jira.get_issue = lambda k: issue
    fake_jira.transition_to_in_progress = lambda k: False

    p = JiraPoller(client=fake_jira, board_id="1", interval_seconds=10)
    p.state_manager = state_manager
    out = p.poll_board()
    assert len(out) == 1
    snap = store.snapshot()
    assert snap["issues"]
    assert snap["issues"][0]["matched_label"] is True
    assert snap["issues"][0]["will_process"] is True
