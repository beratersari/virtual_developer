"""Opening a scheduled ticket shows the prompt before any run exists."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.service import build_task_detail
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore


def _schedule(store: ScheduleStore, *, issue_key: str, when: str, prompt: str, title: str) -> None:
    store.create(
        title=title,
        description="short operator text",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="main",
        mode="build",
        model="opencode/hy3-free",
        backend="opencode",
        scheduled_at=when,
        issue_key=issue_key,
        issue_description=prompt,
        project_key="KAN",
        source="new",
    )


def test_task_detail_includes_schedule_before_a_run(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    prompt = "Add a health check\n\n{code}\n{params}\nMode: build\n{params}\n{code}"
    _schedule(
        store,
        issue_key="KAN-9",
        when="2026-10-08T09:00:00",
        prompt="older prompt",
        title="Older",
    )
    _schedule(
        store,
        issue_key="KAN-9",
        when="2026-10-09T18:30:00",
        prompt=prompt,
        title="Health endpoint",
    )
    _schedule(
        store,
        issue_key="KAN-10",
        when="2026-10-09T18:30:00",
        prompt="other ticket",
        title="Other",
    )
    monkeypatch.setattr("src.state.schedule_store.schedule_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")

    detail = build_task_detail(
        "kan-9",
        state_manager=sm,
        processor=None,
        include_live_jira=False,
        include_artifacts=False,
    )

    assert detail is not None
    assert detail["summary"] == "Health endpoint"
    assert detail["description"] == prompt
    assert [row["issue_key"] for row in detail["schedules"]] == ["KAN-9", "KAN-9"]
    newest = detail["schedules"][0]
    assert newest["title"] == "Health endpoint"
    assert newest["issue_description"] == prompt
    assert newest["mode"] == "build"
    assert newest["model"] == "opencode/hy3-free"
    assert newest["backend"] == "opencode"
    assert newest["scheduled_at"] == "2026-10-09T18:30:00"
    assert newest["status"] == "scheduled"
    assert newest["repository_refs"] == [
        {
            "url": "https://gitlab.example.com/acme/api.git",
            "source_branch": "develop",
            "target_branch": "main",
        }
    ]
    assert "other ticket" not in detail["description"]


def test_task_detail_lists_every_scheduled_repository(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    store.create(
        title="Two repos",
        description="prompt",
        repository_url="https://gitlab.example.com/acme/api.git",
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at="2026-10-09T18:30:00",
        issue_key="KAN-11",
        issue_description="prompt",
        repository_refs=[
            {
                "url": "https://gitlab.example.com/acme/api.git",
                "source_branch": "develop",
                "target_branch": "main",
            },
            {
                "url": "https://gitlab.example.com/acme/web.git",
                "source_branch": "feature/KAN-11",
                "target_branch": "develop",
            },
        ],
    )
    monkeypatch.setattr("src.state.schedule_store.schedule_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")

    detail = build_task_detail(
        "KAN-11",
        state_manager=sm,
        processor=None,
        include_live_jira=False,
        include_artifacts=False,
    )

    assert detail is not None
    assert detail["schedules"][0]["repository_refs"] == [
        {
            "url": "https://gitlab.example.com/acme/api.git",
            "source_branch": "develop",
            "target_branch": "main",
        },
        {
            "url": "https://gitlab.example.com/acme/web.git",
            "source_branch": "feature/KAN-11",
            "target_branch": "develop",
        },
    ]


def test_task_detail_keeps_issue_description_and_still_lists_the_schedule(
    tmp_path, monkeypatch
):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    _schedule(
        store,
        issue_key="KAN-9",
        when="2026-10-09T18:30:00",
        prompt="scheduled prompt",
        title="Health endpoint",
    )
    monkeypatch.setattr("src.state.schedule_store.schedule_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state(
        "KAN-9",
        "Already named",
        description="live local description",
        triggered_by="test",
    )
    sm.update_state("KAN-9", status=TaskStatus.PLAN_READY)

    detail = build_task_detail(
        "KAN-9",
        state_manager=sm,
        processor=None,
        include_live_jira=False,
        include_artifacts=False,
    )

    assert detail is not None
    assert detail["summary"] == "Already named"
    assert detail["description"] == "live local description"
    assert detail["schedules"][0]["issue_description"] == "scheduled prompt"


def test_task_http_returns_the_schedule(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    _schedule(
        store,
        issue_key="KAN-9",
        when="2026-10-09T18:30:00",
        prompt="Add a health check",
        title="Health endpoint",
    )
    monkeypatch.setattr("src.state.schedule_store.schedule_store", store)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)

    response = client.get("/api/tasks/KAN-9")

    assert response.status_code == 200
    body = response.json()
    assert body["description"] == "Add a health check"
    assert body["schedules"][0]["repository_url"] == "https://gitlab.example.com/acme/api.git"
    assert body["schedules"][0]["source_branch"] == "develop"
    assert body["jobs"] == []


def test_schedule_on_issue_renders():
    web = Path(__file__).resolve().parents[1] / "web"
    page = (web / "src/pages/issues/IssueDetailPage.tsx").read_text(encoding="utf-8")
    assert "ScheduleOnIssue" in page
    assert "jobsEmptyLabel" in page
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to render the scheduled ticket")
    proc = subprocess.run(
        [
            npx,
            "--yes",
            "tsx",
            "--tsconfig",
            "tsconfig.app.json",
            "src/pages/issues/scheduleOnIssue.test.tsx",
        ],
        cwd=web,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "scheduleOnIssue ok" in proc.stdout
