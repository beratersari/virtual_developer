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
    assert "default_model" in dumped
    # Inventory is GET /api/models only — not embedded in settings/WS
    assert "available_models" not in dumped
    assert "models" not in dumped


def test_apply_settings_update_runtime(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "poll_interval_seconds", 30)
    monkeypatch.setattr(settings, "jira_board_id", "1")
    monkeypatch.setattr(settings, "default_model", "old/m")
    view = apply_settings_update(
        SettingsUpdate(
            poll_interval_seconds=45,
            jira_board_id="99",
            default_model="opencode/new-model",
        )
    )
    assert view.poll_interval_seconds == 45
    assert view.jira_board_id == "99"
    assert settings.poll_interval_seconds == 45
    assert settings.default_model == "opencode/new-model"
    assert view.default_model == "opencode/new-model"


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
    monkeypatch.setattr(settings, "default_model", "before/m")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)
    r = client.patch(
        "/api/settings",
        json={
            "poll_interval_seconds": 60,
            "default_model": "opencode/deepseek-v4-flash-free",
        },
    )
    assert r.status_code == 200
    assert r.json()["poll_interval_seconds"] == 60
    assert r.json()["default_model"] == "opencode/deepseek-v4-flash-free"
    assert "models" not in r.json()
    assert settings.default_model == "opencode/deepseek-v4-flash-free"

    from src.opencode_models import ModelInfo

    with patch(
        "src.dashboard.service.list_available_models",
        return_value=(
            [
                ModelInfo(
                    id="opencode/deepseek-v4-flash-free",
                    name="DeepSeek",
                    provider="opencode",
                    source="cli",
                )
            ],
            None,
            "/tmp/oc.json",
            "oc/m",
        ),
    ):
        m = client.get("/api/models")
    assert m.status_code == 200
    body = m.json()
    assert "models" in body
    assert body["default_model"] == "opencode/deepseek-v4-flash-free"
    assert body["models"][0]["id"] == "opencode/deepseek-v4-flash-free"
    assert body["models"][0]["label"]
    assert body["opencode_config_path"] == "/tmp/oc.json"


def test_read_app_version():
    v = read_app_version()
    assert isinstance(v, str)
    assert len(v) > 0


def test_task_detail_and_cancel(tmp_path, monkeypatch, fake_jira, isolate_jira_agent_artifacts):
    from src.processor import JobProcessor
    from src.dashboard.issue_logs import issue_log_ring

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("DET-1", "summary here", "do the thing")
    sm.update_state(
        "DET-1",
        status=TaskStatus.EXECUTING,
        progress_percentage=10,
        metadata={"workflow_type": "direct"},
        current_task_id="task-abc",
    )
    issue_log_ring.append("Working on DET-1 something")
    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    (sessions / "DET-1_20260101_120000_0.log").write_text("opencode output line\n")
    (sessions / "DET-1_20260101_120000_0.prompt.txt").write_text("full prompt body")

    # Live Jira returns updated description/status (not frozen local state)
    fake_jira.get_issue = MagicMock(
        return_value={
            "key": "DET-1",
            "fields": {
                "summary": "summary from jira live",
                "description": "description updated in jira",
                "status": {"name": "In Progress"},
            },
        }
    )

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = MagicMock()
    proc.jira_client = fake_jira
    runner = MagicMock()
    runner.cancel_task = MagicMock(return_value=True)
    runner.cancel_all_tasks = MagicMock(return_value=1)
    proc._contexts["DET-1"] = {"git": MagicMock(), "runner": runner}

    app = create_dashboard_app(processor=proc, state_manager=sm)
    client = TestClient(app)

    r = client.get("/api/tasks/DET-1")
    assert r.status_code == 200
    body = r.json()
    assert body["issue_key"] == "DET-1"
    assert body["can_cancel"] is True
    assert body["live"] is True
    assert body["description"] == "description updated in jira"
    assert body["summary"] == "summary from jira live"
    assert body["jira_status"] == "In Progress"
    assert body["jira_live"] is True
    assert "agent" in body["prompts"]
    assert "assembled_prompt" not in body["prompts"]
    assert "system_rules" not in body["prompts"]
    assert any("opencode output" in (s.get("content") or "") for s in body["session_logs"])
    assert any("DET-1" in (line.get("message") or "") for line in body["system_logs"])

    c = client.post("/api/tasks/DET-1/cancel")
    assert c.status_code == 200
    assert c.json()["ok"] is True
    st = sm.get_state("DET-1")
    assert st.status == TaskStatus.CANCELLED
    assert st.current_task_id is None
    # Preserved for dashboard display
    assert (st.metadata or {}).get("last_task_id") == "task-abc"
    assert "DET-1" not in proc._contexts

    detail_after = client.get("/api/tasks/DET-1").json()
    assert detail_after["current_task_id"] == "task-abc"
    # Jobs embedded on detail (legacy session-derived when no JobStore rows)
    assert "jobs" in detail_after
    assert any(j["issue_key"] == "DET-1" for j in detail_after["jobs"])

    # Terminal cannot cancel again
    c2 = client.post("/api/tasks/DET-1/cancel")
    assert c2.status_code == 400


def test_api_jobs_filter_and_legacy_sessions(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Jobs list supports issue_key filter; session logs become legacy jobs."""
    from src.state.job_store import JobStore

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("JOB-1", "first issue", "desc live latest")
    sm.create_state("JOB-2", "second", "d")

    sessions = isolate_jira_agent_artifacts["sessions_dir"]
    (sessions / "JOB-1_20260101_100000_0.log").write_text("run a\n")
    (sessions / "JOB-1_20260101_100000_0.log.session_id").write_text("ses_aaa")
    (sessions / "JOB-1_20260101_100000_0.prompt.txt").write_text(
        "# Direct\n\n## Task\ndesc from first prompt\n\n# X\n",
        encoding="utf-8",
    )
    (sessions / "JOB-1_20260102_110000_0.log").write_text("run b\n")
    (sessions / "JOB-1_20260102_110000_0.prompt.txt").write_text(
        "# Direct\n\n## Task\ndesc from second prompt\n\n# X\n",
        encoding="utf-8",
    )
    (sessions / "JOB-2_20260101_120000_0.log").write_text("other\n")

    store = isolate_jira_agent_artifacts["job_store"]
    stored = store.create_job(
        issue_key="JOB-1",
        summary="first issue",
        description="desc frozen on job store",
        workflow_type="direct",
        agent="sisyphus",
        status="completed",
    )
    store.update_job(
        stored["job_id"],
        session_log_path=str((sessions / "JOB-1_20260102_110000_0.log").resolve()),
        prompt_path=str((sessions / "JOB-1_20260102_110000_0.prompt.txt").resolve()),
        opencode_session_id="ses_bbb",
    )

    monkeypatch.chdir(tmp_path)
    with patch("src.dashboard.api.job_store", store):
        with patch("src.dashboard.service.default_job_store", store):
            app = create_dashboard_app(processor=None, state_manager=sm)
            client = TestClient(app)

            all_jobs = client.get("/api/jobs").json()
            assert all_jobs["total"] >= 2
            keys = {j["issue_key"] for j in all_jobs["jobs"]}
            assert "JOB-1" in keys
            assert "JOB-2" in keys

            filtered = client.get("/api/jobs", params={"issue_key": "job-1"}).json()
            assert filtered["issue_key_filter"] == "job-1" or filtered["issue_key_filter"] == "JOB-1"
            assert filtered["total"] >= 1
            assert all(j["issue_key"] == "JOB-1" for j in filtered["jobs"])
            # Stored job + legacy for the other session
            job_ids = {j["job_id"] for j in filtered["jobs"]}
            assert stored["job_id"] in job_ids
            assert any(jid.startswith("legacy_") for jid in job_ids)

            detail = client.get("/api/tasks/JOB-1").json()
            assert len(detail["jobs"]) >= 1
            assert all(j["issue_key"] == "JOB-1" for j in detail["jobs"])
            # Live issue description must not overwrite per-job snapshots
            assert detail["description"] == "desc live latest"
            by_id = {j["job_id"]: j for j in detail["jobs"]}
            assert by_id[stored["job_id"]]["description"] == "desc frozen on job store"
            legacy = [j for j in detail["jobs"] if j["job_id"].startswith("legacy_")]
            assert legacy, "expected legacy job from first session"
            assert any(
                j["description"] == "desc from first prompt" for j in legacy
            ), [j["description"] for j in legacy]
            # Distinct job descriptions must not all equal live issue text
            descs = {j["description"] for j in detail["jobs"] if j.get("description")}
            assert len(descs) >= 2, descs


def test_task_detail_404(tmp_path):
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)
    assert client.get("/api/tasks/NOPE-1").status_code == 404


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
