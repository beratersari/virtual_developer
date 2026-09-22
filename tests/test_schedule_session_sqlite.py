"""Schedule and session lists read SQLite, including rows that already exist as JSON."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from src.state.schedule_store import ScheduleStore
from src.state.session_bind_store import SessionBindStore


def _schedule(store: ScheduleStore, *, issue_key: str, when: str, title: str) -> dict:
    return store.create(
        title=title,
        description="body",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=when,
        issue_key=issue_key,
        issue_description="body",
    )


def test_schedule_list_and_count_use_sqlite(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    _schedule(store, issue_key="KAN-1", when="2026-12-01T10:00:00", title="later")
    _schedule(store, issue_key="KAN-2", when="2026-12-03T10:00:00", title="newest")
    _schedule(store, issue_key="KAN-3", when="2026-12-02T10:00:00", title="middle")
    assert (tmp_path / "schedules.sqlite").is_file()
    assert store.count_schedules() == 3
    assert store.count_schedules(status="scheduled") == 3
    page = store.list_schedules(limit=2, offset=0)
    assert [row["title"] for row in page] == ["newest", "middle"]
    assert store.has_open_for_issue("KAN-1") is True
    assert store.has_open_for_issue("KAN-9") is False
    newest = page[0]
    updated = store.update(newest["schedule_id"], status="cancelled")
    assert updated is not None
    assert updated["status"] == "cancelled"
    assert store.count_schedules(status="scheduled") == 2
    assert store.has_open_for_issue(newest["issue_key"]) is False
    assert store.list_schedules(status="cancelled", limit=10)[0]["schedule_id"] == (
        newest["schedule_id"]
    )


def test_schedule_index_backfills_existing_json(tmp_path):
    folder = tmp_path / "schedules"
    folder.mkdir()
    rec = {
        "schedule_id": "sched_legacy01",
        "title": "already on disk",
        "description": "d",
        "repository_url": "https://gitlab.example.com/acme/app.git",
        "source_branch": "develop",
        "target_branch": "develop",
        "mode": "build",
        "scheduled_at": "2026-11-01T09:00:00",
        "status": "scheduled",
        "issue_key": "OLD-1",
        "issue_description": "d",
        "created_at": "2026-11-01T08:00:00",
        "updated_at": "2026-11-01T08:00:00",
    }
    (folder / "sched_legacy01.json").write_text(
        json.dumps(rec), encoding="utf-8"
    )
    store = ScheduleStore(schedules_dir=folder)
    assert store.ensure_index() == 1
    assert store.count_schedules(status="scheduled") == 1
    listed = store.list_schedules(status="scheduled", limit=10)
    assert listed[0]["issue_key"] == "OLD-1"
    assert listed[0]["title"] == "already on disk"
    assert store.has_open_for_issue("OLD-1") is True


def test_session_workspaces_come_from_sqlite(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-1",
        target_branch="develop",
        session_id="ses_plan",
        issue_key="KAN-1",
        kind="plan",
        working_directory=str(tmp_path / "clone"),
    )
    store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-1",
        target_branch="develop",
        session_id="ses_build",
        issue_key="KAN-1",
        kind="build",
    )
    assert (tmp_path / "opencode-binds.sqlite").is_file()
    rows = store.list_workspaces(limit=None)
    assert len(rows) == 1
    assert rows[0]["session_count"] == 2
    assert set(rows[0]["kinds"]) == {"plan", "build"}
    assert store.find_by_issue_key("KAN-1")["session_id"] in {"ses_plan", "ses_build"}
    live = store.list_binds(limit=10)
    assert {row["session_id"] for row in live} == {"ses_plan", "ses_build"}
    plan = next(row for row in live if row["session_id"] == "ses_plan")
    assert store.delete(plan["bind_id"]) is True
    assert store.find_by_issue_key("KAN-1")["session_id"] == "ses_build"
    left = store.list_workspaces(limit=None)
    assert left[0]["session_count"] == 1
    assert left[0]["kinds"] == ["build"]


def test_session_index_backfills_existing_json(tmp_path):
    folder = tmp_path / "binds"
    folder.mkdir()
    rec = {
        "bind_id": "osb_legacybind01",
        "repository_url": "https://gitlab.example.com/acme/app.git",
        "repository_key": "gitlab.example.com/acme/app",
        "branch": "feature/OLD-2",
        "target_branch": "develop",
        "session_id": "ses_old",
        "kind": "build",
        "issue_key": "OLD-2",
        "job_id": None,
        "working_directory": str(tmp_path / "clone"),
        "forgotten_session_ids": [],
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-02T00:00:00",
    }
    (folder / "osb_legacybind01.json").write_text(json.dumps(rec), encoding="utf-8")
    store = SessionBindStore(binds_dir=folder)
    assert store.ensure_index() == 1
    rows = store.list_workspaces(limit=None)
    assert len(rows) == 1
    assert rows[0]["issue_key"] == "OLD-2"
    assert store.find_by_issue_key("OLD-2")["session_id"] == "ses_old"


def test_schedule_index_drops_row_when_json_file_is_gone(tmp_path):
    folder = tmp_path / "schedules"
    first = ScheduleStore(schedules_dir=folder)
    rec = _schedule(first, issue_key="GONE-1", when="2026-12-01T10:00:00", title="gone")
    first._path(rec["schedule_id"]).unlink()
    first._index.close()
    restarted = ScheduleStore(schedules_dir=folder)
    assert restarted.ensure_index() == 0
    assert restarted.count_schedules() == 0
    assert restarted.list_schedules() == []
    assert restarted.has_open_for_issue("GONE-1") is False


def test_session_index_drops_row_when_json_file_is_gone(tmp_path):
    folder = tmp_path / "binds"
    first = SessionBindStore(binds_dir=folder)
    rec = first.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/GONE-1",
        target_branch="develop",
        session_id="ses_gone",
        issue_key="GONE-1",
        kind="build",
    )
    first._path(rec["bind_id"]).unlink()
    first._index.close()
    restarted = SessionBindStore(binds_dir=folder)
    assert restarted.ensure_index() == 0
    assert restarted.list_binds(limit=None) == []
    assert restarted.list_workspaces(limit=None) == []
    assert restarted.find_by_issue_key("GONE-1") is None


def test_schedule_queries_fall_back_to_json_when_sqlite_fails(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = _schedule(store, issue_key="KAN-4", when="2026-12-01T10:00:00", title="file")

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("locked")

    store._index.list_ids = boom
    store._index.count = boom
    store._index.has_issue_status = boom
    listed = store.list_schedules()
    assert [row["schedule_id"] for row in listed] == [rec["schedule_id"]]
    assert store.count_schedules() == 1
    assert store.count_schedules(status="scheduled") == 1
    assert store.has_open_for_issue("KAN-4") is True


def test_session_queries_fall_back_to_json_when_sqlite_fails(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    store.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-4",
        target_branch="develop",
        session_id="ses_file",
        issue_key="KAN-4",
        kind="build",
    )

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("locked")

    store._index.list_live = boom
    store._index.newest_live_for_issue = boom
    listed = store.list_binds(limit=None)
    assert [row["session_id"] for row in listed] == ["ses_file"]
    assert store.find_by_issue_key("KAN-4")["session_id"] == "ses_file"
    rows = store.list_workspaces(limit=None)
    assert rows[0]["issue_key"] == "KAN-4"


def test_forgotten_session_id_is_read_from_sqlite_not_a_directory_scan(tmp_path):
    store = SessionBindStore(binds_dir=tmp_path / "binds")
    repo = "https://gitlab.example.com/acme/app.git"
    branch = "feature/shared"
    target = "develop"
    store.upsert(
        repository_url=repo,
        branch=branch,
        target_branch=target,
        session_id="ses_live",
        issue_key="KAN-1",
    )
    other = store.upsert(
        repository_url=repo,
        branch=branch,
        target_branch=target,
        session_id="ses_drop",
        issue_key="KAN-2",
    )
    store.forget_session(other["bind_id"], session_id="ses_drop", reason="reset")
    # Index is already warm, the way it is after daemon start. Removing the
    # JSON afterwards must not make forgotten_ids_for rescan the directory.
    assert store.ensure_index() == 2
    store._path(other["bind_id"]).unlink()
    scanned: list[str] = []
    real_glob = Path.glob

    def tracking(self: Path, pattern: str):
        if self == store.binds_dir:
            scanned.append(pattern)
        return real_glob(self, pattern)

    with patch("pathlib.Path.glob", tracking):
        found = store.forgotten_ids_for(repo, branch, target, issue_key="KAN-1")
    assert "ses_drop" in found
    assert scanned == []


def test_http_lists_legacy_schedule_and_session_after_restart(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Dashboard routes build the index themselves. The test does not call ensure_index."""
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    import src.dashboard.api as api
    from src.state.manager import JiraStateManager

    schedules = isolate_jira_agent_artifacts["schedule_store"]
    monkeypatch.setattr(api, "schedule_store", schedules)
    sched_dir = isolate_jira_agent_artifacts["schedules_dir"]
    (sched_dir / "sched_legacyhttp.json").write_text(
        json.dumps(
            {
                "schedule_id": "sched_legacyhttp",
                "title": "from an old file",
                "description": "d",
                "repository_url": "https://gitlab.example.com/acme/app.git",
                "source_branch": "develop",
                "target_branch": "develop",
                "mode": "build",
                "scheduled_at": "2026-11-01T09:00:00",
                "status": "scheduled",
                "issue_key": "OLD-HTTP",
                "issue_description": "d",
                "created_at": "2026-11-01T08:00:00",
                "updated_at": "2026-11-01T08:00:00",
            }
        ),
        encoding="utf-8",
    )
    binds_dir = isolate_jira_agent_artifacts["binds_dir"]
    (binds_dir / "osb_legacyhttp1.json").write_text(
        json.dumps(
            {
                "bind_id": "osb_legacyhttp1",
                "repository_url": "https://gitlab.example.com/acme/legacy.git",
                "repository_key": "gitlab.example.com/acme/legacy",
                "branch": "feature/OLD-HTTP",
                "target_branch": "develop",
                "session_id": "ses_legacyhttp",
                "kind": "build",
                "issue_key": "OLD-HTTP",
                "forgotten_session_ids": [],
                "created_at": "2026-10-01T00:00:00",
                "updated_at": "2026-10-02T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    app = create_dashboard_app(
        processor=None,
        state_manager=JiraStateManager(state_dir=tmp_path / "state"),
    )
    client = TestClient(app)
    listed = client.get("/api/schedules").json()
    assert listed["total"] == 1
    assert listed["schedules"][0]["issue_key"] == "OLD-HTTP"
    assert listed["schedules"][0]["title"] == "from an old file"
    workspaces = client.get("/api/opencode-workspaces").json()
    assert workspaces["total"] == 1
    assert workspaces["workspaces"][0]["issue_key"] == "OLD-HTTP"
    assert workspaces["workspaces"][0]["session_count"] == 1


def test_sessions_page_counts_more_than_500_binds(
    isolate_jira_agent_artifacts, tmp_path
):
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_dashboard_app
    from src.state.manager import JiraStateManager

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    repo = "https://gitlab.example.com/acme/app.git"
    for i in range(501):
        binds.upsert(
            repository_url=repo,
            branch=f"feature/N-{i}",
            target_branch="develop",
            session_id=f"ses_{i}",
            issue_key=f"KAN-{i}",
            kind="build",
        )
    app = create_dashboard_app(
        processor=None,
        state_manager=JiraStateManager(state_dir=tmp_path / "state-many"),
    )
    body = TestClient(app).get(
        "/api/opencode-workspaces", params={"page": 1, "page_size": 25}
    ).json()
    assert body["total"] == 501
    assert len(body["workspaces"]) == 25
    last = TestClient(app).get(
        "/api/opencode-workspaces", params={"page": 21, "page_size": 25}
    ).json()
    assert last["total"] == 501
    assert len(last["workspaces"]) == 1
