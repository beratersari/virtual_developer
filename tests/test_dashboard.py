"""Dashboard API and poll snapshot tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from unittest.mock import MagicMock

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
    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "gitlab.com")
    view = build_settings_view()
    dumped = view.model_dump()
    assert "super-secret" not in str(dumped)
    assert "pat-secret" not in str(dumped)
    assert "jira_api_token" not in dumped
    assert "gitlab_pat" not in dumped or dumped.get("gitlab_pat") in (None, "", False)
    assert view.jira_token_configured is True
    assert view.gitlab_pat_configured is True
    assert view.jira_host == "https://jira.example.com"
    assert view.gitlab_allowed_hosts == "gitlab.com"
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


def test_apply_settings_connection_and_write_only_secrets(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_host", "https://old.example.com")
    monkeypatch.setattr(settings, "jira_email", "old@ex.com")
    monkeypatch.setattr(settings, "jira_api_token", "old-token")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_pat", "old-pat")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "old.gitlab")

    view = apply_settings_update(
        SettingsUpdate(
            jira_host="https://new.example.com/",
            jira_email="new@ex.com",
            jira_api_token="new-secret-token",
            gitlab_credentials=[
                {"host": "gitlab.com", "pat": "pat-cloud"},
                {"host": "GitLab.Example.COM", "pat": "pat-onprem"},
            ],
        )
    )
    assert settings.jira_host == "https://new.example.com"
    assert settings.jira_email == "new@ex.com"
    assert settings.jira_api_token == "new-secret-token"
    assert settings.gitlab_pat_for_host("gitlab.com") == "pat-cloud"
    assert settings.gitlab_pat_for_host("gitlab.example.com") == "pat-onprem"
    assert settings.gitlab_pat_for_host("api.gitlab.com") == "pat-cloud"
    assert view.jira_token_configured is True
    assert view.gitlab_pat_configured is True
    assert {c.host for c in view.gitlab_credentials} == {
        "gitlab.com",
        "gitlab.example.com",
    }
    # Secrets never in view dump
    dumped = view.model_dump()
    assert "new-secret-token" not in str(dumped)
    assert "pat-cloud" not in str(dumped)
    assert "pat-onprem" not in str(dumped)

    # Empty PAT keeps existing; omit host removes it
    apply_settings_update(
        SettingsUpdate(
            jira_api_token="",
            gitlab_credentials=[
                {"host": "gitlab.com", "pat": ""},  # keep
            ],
        )
    )
    assert settings.jira_api_token == "new-secret-token"
    assert settings.gitlab_pat_for_host("gitlab.com") == "pat-cloud"
    assert settings.gitlab_pat_for_host("gitlab.example.com") == ""


def test_refresh_runtime_jira_clients(monkeypatch):
    from src.dashboard.service import refresh_runtime_jira_clients

    proc = MagicMock()
    old_client = MagicMock()
    proc.jira_client = old_client
    proc.reporter = MagicMock()
    poller = MagicMock()
    poller.client = MagicMock()

    new_client = MagicMock()
    monkeypatch.setattr(
        "src.dashboard.service.create_jira_client",
        lambda simulated=False: new_client,
        raising=False,
    )
    # create is imported inside function from src.jira.client
    monkeypatch.setattr(
        "src.jira.client.create_jira_client",
        lambda simulated=False: new_client,
    )
    from src.config import settings

    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok")

    refresh_runtime_jira_clients(processor=proc, poller=poller)
    old_client.close.assert_called()
    assert proc.jira_client is new_client
    assert proc.reporter.client is new_client
    assert poller.client is new_client


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


def test_task_detail_without_local_state(tmp_path):
    """Poll-opened issues with no agent state still get a detail payload."""
    from src.dashboard.service import build_task_detail
    from src.dashboard.snapshot import PollSnapshotStore

    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = PollSnapshotStore()
    store.end_poll(
        source="board",
        issues=[
            {
                "key": "BARE-1",
                "summary": "from poll",
                "jira_status": "To Do",
                "labels": ["ai-assist"],
                "matched_label": True,
                "matched_assignee": False,
                "is_todo": True,
                "will_process": False,
                "matched_labels": ["ai-assist"],
            }
        ],
        interval_seconds=30,
    )
    with patch("src.dashboard.service.poll_snapshot_store", store):
        with patch(
            "src.dashboard.service._fetch_live_jira_fields",
            return_value={},
        ):
            detail = build_task_detail("BARE-1", state_manager=sm, processor=None)
    assert detail is not None
    assert detail["issue_key"] == "BARE-1"
    assert detail["summary"] == "from poll"
    assert detail["can_cancel"] is False


def test_git_deliveries_aggregate_from_jobs_and_meta(tmp_path, monkeypatch):
    """Task detail exposes all commits/MRs across re-triggered runs."""
    from src.dashboard.service import _collect_git_deliveries, build_jobs
    from src.state.job_store import JobStore
    from src.state.manager import JiraStateManager
    from src.state.models import TaskStatus

    monkeypatch.chdir(tmp_path)
    jobs = JobStore(jobs_dir=tmp_path / "jobs")
    j1 = jobs.create_job(issue_key="GIT-1", summary="run1", workflow_type="execution")
    jobs.update_job(
        j1["job_id"],
        status="completed",
        feature_branch="feature/GIT-1",
        merge_request_url="https://gitlab.example.com/g/r/-/merge_requests/1",
        commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        commit_subject="feat(GIT-1): first",
        commit_url="https://gitlab.example.com/g/r/-/commit/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    j2 = jobs.create_job(issue_key="GIT-1", summary="run2", workflow_type="execution")
    jobs.update_job(
        j2["job_id"],
        status="completed",
        feature_branch="feature/GIT-1",
        merge_request_url="https://gitlab.example.com/g/r/-/merge_requests/2",
        commit_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        commit_subject="feat(GIT-1): second",
        commit_url="https://gitlab.example.com/g/r/-/commit/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )

    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("GIT-1", "s", description="d", triggered_by="t")
    sm.update_state(
        "GIT-1",
        status=TaskStatus.COMPLETED,
        metadata={
            "merge_request_url": "https://gitlab.example.com/g/r/-/merge_requests/2",
            "feature_branch": "feature/GIT-1",
        },
    )

    deliveries = _collect_git_deliveries(
        issue_key="GIT-1",
        meta=sm.get_state("GIT-1").metadata or {},
        store=jobs,
    )
    assert len(deliveries) >= 2
    mrs = {d["merge_request_url"] for d in deliveries if d.get("merge_request_url")}
    assert "https://gitlab.example.com/g/r/-/merge_requests/1" in mrs
    assert "https://gitlab.example.com/g/r/-/merge_requests/2" in mrs

    listed = build_jobs(issue_key="GIT-1", page=1, page_size=10, store=jobs, state_manager=sm)
    by_id = {j.job_id: j for j in listed.jobs}
    assert by_id[j1["job_id"]].merge_request_url.endswith("/merge_requests/1")
    assert by_id[j2["job_id"]].commit_sha.startswith("bbbb")


def test_build_jobs_pagination(tmp_path):
    from src.dashboard.service import build_jobs
    from src.state.job_store import JobStore

    jobs = JobStore(jobs_dir=tmp_path / "jobs")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    for i in range(7):
        jobs.create_job(
            issue_key="PAG-1",
            summary=f"run {i}",
            description="d",
            workflow_type="execution",
            agent="sisyphus",
        )
    page1 = build_jobs(issue_key="PAG-1", page=1, page_size=3, store=jobs, state_manager=sm)
    assert page1.total == 7
    assert page1.page == 1
    assert page1.page_size == 3
    assert len(page1.jobs) == 3
    page2 = build_jobs(issue_key="PAG-1", page=2, page_size=3, store=jobs, state_manager=sm)
    assert len(page2.jobs) == 3
    page3 = build_jobs(issue_key="PAG-1", page=3, page_size=3, store=jobs, state_manager=sm)
    assert len(page3.jobs) == 1
    ids = {j.job_id for j in page1.jobs + page2.jobs + page3.jobs}
    assert len(ids) == 7


def test_poll_api_hides_unmatched_board_issues(tmp_path):
    """Poll DTO lists only bot-eligible issues (label or assignee match)."""
    from src.dashboard.service import build_poll_status

    sm = JiraStateManager(state_dir=tmp_path / "state")
    store = PollSnapshotStore()
    store.end_poll(
        source="board 1",
        issues=[
            {
                "key": "MATCH-1",
                "summary": "has trigger",
                "jira_status": "To Do",
                "labels": ["ai-assist"],
                "assignee": None,
                "matched_label": True,
                "matched_assignee": False,
                "is_todo": True,
                "will_process": True,
                "matched_labels": ["ai-assist"],
            },
            {
                "key": "NOISE-9",
                "summary": "unrelated ticket",
                "jira_status": "To Do",
                "labels": ["other"],
                "assignee": "Alice",
                "matched_label": False,
                "matched_assignee": False,
                "is_todo": True,
                "will_process": False,
                "matched_labels": [],
            },
            {
                "key": "BOT-2",
                "summary": "assigned to bot",
                "jira_status": "In Progress",
                "labels": [],
                "assignee": "Jira AI Bot",
                "matched_label": False,
                "matched_assignee": True,
                "is_todo": False,
                "will_process": False,
                "matched_labels": [],
            },
        ],
        interval_seconds=30,
    )
    poll = build_poll_status(store, sm)
    keys = {i.key for i in poll.issues}
    assert keys == {"MATCH-1", "BOT-2"}
    assert "NOISE-9" not in keys
    # Counts still reflect the full board snapshot
    assert poll.matched_count == 2
    assert poll.will_process_count == 1


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
        metadata={"workflow_type": "execution"},
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
        workflow_type="execution",
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


def test_task_detail_without_state_is_stub_not_404(tmp_path):
    """Unknown keys still return 200 so Poll monitor can open any key."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    app = create_dashboard_app(processor=None, state_manager=sm)
    client = TestClient(app)
    with patch(
        "src.dashboard.service._fetch_live_jira_fields",
        return_value={},
    ):
        r = client.get("/api/tasks/NOPE-1")
    assert r.status_code == 200
    body = r.json()
    assert body["issue_key"] == "NOPE-1"
    assert body["can_cancel"] is False
    assert body.get("jobs") == []


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
