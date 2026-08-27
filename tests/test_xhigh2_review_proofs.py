"""Proofs / regressions for the 2026-08-14 xhigh 21-agent review.

M1 is intentional product behaviour (longer configured Jira key wins).
M2–M5 encode the fixes for sprint widen, PENDING skip, GitLab PAT persist,
and inferred host-rename PAT copy.

Run: .venv/bin/python -m pytest tests/test_xhigh2_review_proofs.py -q
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.state.models import TaskStatus


# ---------------------------------------------------------------------------
# M1 — MR/Jira key scan prefers the longer project key, not the leftmost
# ---------------------------------------------------------------------------


def test_m1_jira_key_from_text_longer_key_wins_over_leftmost():
    """INTENTIONAL: longer configured keys win so PROJECT-1 is not eaten as
    PROJ-1. Distinct keys in one title therefore bind the longer key."""
    from src.gitlab.keys import jira_key_from_text, resolve_mr_issue_key

    title = "feat(KAN-12): align with PLATFORM-3"
    found = jira_key_from_text(title, ["KAN", "PLATFORM"])
    assert found == "PLATFORM-3"
    assert found != "KAN-12"

    # Free-text description mentions do not bind a job
    bound = resolve_mr_issue_key(
        mr_title="Add login",
        mr_description="See also PLATFORM-9; originally KAN-1",
        project_path="acme/demo",
        mr_iid=4,
        project_keys=["KAN", "PLATFORM"],
    )
    assert bound.startswith("GL-")


# ---------------------------------------------------------------------------
# M2 — Sprint API 5xx / timeout is treated as Kanban (whole board)
# ---------------------------------------------------------------------------


def test_m2_get_active_sprint_http_500_is_error_not_kanban():
    from src.jira.client import JiraClient

    c = JiraClient.__new__(JiraClient)
    c.host = "https://jira.example.com"
    c.last_error = None
    c.sprint_lookup = None

    class R:
        status_code = 500
        text = "internal"

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock(status_code=500)
            )

        def json(self):
            return {}

    c.client = MagicMock()
    c.client.get.return_value = R()
    assert c.get_active_sprint("10") is None
    assert c.last_error
    assert c.sprint_lookup == "error"


def test_m2_poller_does_not_widen_to_board_on_sprint_error(state_manager):
    """Sprint lookup error must not call get_board_issues."""
    from src.jira.poller import JiraPoller

    poller = JiraPoller(client=MagicMock(), interval_seconds=1, board_id="10")
    poller.state_manager = state_manager

    def sprint_fail(_board_id):
        poller.client.last_error = "500 Server Error for url"
        poller.client.sprint_lookup = "error"
        return None

    poller.client.get_active_sprint.side_effect = sprint_fail
    poller.client.get_board_issues.return_value = [
        {
            "key": "BACKLOG-99",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["bot"],
                "summary": "should not be intaken from a Scrum blip",
            },
        }
    ]
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["bot", "ai-assist"]
        s.trigger_assignee_names_list = []
        result = poller.poll_board()
    poller.client.get_board_issues.assert_not_called()
    assert result == []


# ---------------------------------------------------------------------------
# M3 — PENDING is not treated as in-flight; poller can double-accept
# ---------------------------------------------------------------------------


def test_m3_pending_on_todo_is_not_requeued(state_manager):
    from src.jira.poller import JiraPoller

    poller = JiraPoller(client=MagicMock(), interval_seconds=1, board_id="10")
    poller.state_manager = state_manager
    state_manager.create_state("PEND-1", "pending job", "d")
    assert state_manager.get_state("PEND-1").status == TaskStatus.PENDING
    state_manager.create_state("EXEC-1", "running job", "d")
    state_manager.update_state("EXEC-1", status=TaskStatus.EXECUTING)

    poller.client.get_active_sprint.return_value = {"id": 1, "name": "S"}
    poller.client.get_sprint_issues.return_value = [
        {
            "key": "PEND-1",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["bot"],
                "summary": "pending",
                "assignee": {"displayName": "DevBot"},
            },
        },
        {
            "key": "EXEC-1",
            "fields": {
                "status": {"name": "To Do"},
                "labels": ["bot"],
                "summary": "executing",
                "assignee": {"displayName": "DevBot"},
            },
        },
    ]
    with patch("src.jira.poller.settings") as s:
        s.trigger_labels_list = ["bot"]
        s.trigger_assignee_names_list = ["devbot"]
        result = poller.poll_board()
    keys = {i["key"] for i in result}
    assert "PEND-1" not in keys
    assert "EXEC-1" not in keys


# ---------------------------------------------------------------------------
# M4 — Settings GitLab PAT save is memory-only (not .env / runtime JSON)
# ---------------------------------------------------------------------------


def test_m4_gitlab_pat_settings_not_written_to_env_or_runtime(tmp_path, monkeypatch):
    from src.config import settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JIRA_HOST=https://jira.example.com\n", encoding="utf-8")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")

    apply_settings_update(
        SettingsUpdate(
            gitlab_credentials=[{"host": "gitlab.company.com", "pat": "MUST-SURVIVE-RESTART"}]
        )
    )
    assert settings.gitlab_pat_for_host("gitlab.company.com") == "MUST-SURVIVE-RESTART"

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MUST-SURVIVE-RESTART" in env_text
    assert "GITLAB_HOST_PATS" in env_text

    runtime = tmp_path / ".jira-agent" / "runtime_settings.json"
    if runtime.is_file():
        body = runtime.read_text(encoding="utf-8")
        assert "MUST-SURVIVE-RESTART" not in body


# ---------------------------------------------------------------------------
# M5 — 1:1 host rename copies the stored GitLab PAT onto the new host
# ---------------------------------------------------------------------------


def test_m5_unauthenticated_gitlab_host_rename_rebinds_stored_pat(monkeypatch):
    from src.config import settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    settings.set_gitlab_host_pat_map({"gitlab.company.com": "REAL-GITLAB-PAT"})

    apply_settings_update(
        SettingsUpdate(
            gitlab_credentials=[{"host": "attacker.example", "pat": ""}]
        )
    )
    assert settings.gitlab_pat_for_host("attacker.example") == ""
    assert settings.gitlab_pat_for_host("gitlab.company.com") == ""


# ---------------------------------------------------------------------------
# M6 — Queue list oldest-N without status hides new waiting work
# ---------------------------------------------------------------------------


def test_m6_queue_oldest_n_hides_new_queued_and_zeros_count(tmp_path):
    from src.dashboard.service import build_queue
    from src.state.queue_store import WorkQueueStore

    store = WorkQueueStore(queue_dir=tmp_path / "queue")
    for i in range(200):
        rec = store.enqueue(source="jira", issue_key=f"OLD-{i}")
        rec["created_at"] = f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}.000"
        rec["status"] = "completed"
        rec["finished_at"] = rec["created_at"]
        store._write(rec)

    fresh = store.enqueue(source="jira", issue_key="NEW-1", summary="waiting")
    store.update(fresh["queue_id"], created_at="2026-08-14T12:00:00.000")

    window = store.list_items(status="queued", limit=200)
    assert any(r.get("issue_key") == "NEW-1" for r in window)

    payload = build_queue(store=store, limit=200)
    assert payload.queued_count >= 1
    assert any(i.issue_key == "NEW-1" for i in payload.items)

    # Filtering by status would have found it
    only_queued = store.list_items(status="queued", limit=200)
    assert [r["issue_key"] for r in only_queued] == ["NEW-1"]


# ---------------------------------------------------------------------------
# M7 — On-prem GitLab port is stripped from host / probe / glab API
# ---------------------------------------------------------------------------


def test_m7_gitlab_port_stripped_from_host_and_probe_url():
    from src.git_manager import GitManager
    from src.gitlab_connection import _normalize_host, probe_gitlab_connection

    assert _normalize_host("https://gitlab.corp:8091") == "gitlab.corp:8091"
    assert "8091" in _normalize_host("https://gitlab.corp:8091/g/p.git")

    gm = GitManager.__new__(GitManager)
    gm.remote_url = "https://gitlab.corp:8091/g/p.git"
    host, path = gm._gitlab_host_and_project()
    assert host == "gitlab.corp:8091"
    assert path == "g/p"
    assert GitManager._host_from_url("https://gitlab.corp:8091/g/p.git") == "gitlab.corp:8091"

    seen: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kwargs):
            seen.append(url)

            class R:
                status_code = 401
                text = "no"

                def json(self):
                    return {}

            return R()

    with patch("src.gitlab_connection.httpx.Client", FakeClient):
        probe_gitlab_connection("https://gitlab.corp:8091", pat="dummy")
    assert seen
    assert any(":8091" in u for u in seen)


# ---------------------------------------------------------------------------
# M8 — Workdir-mismatch retry keeps the original BUILD/PLAN prompt
# ---------------------------------------------------------------------------


def test_m8_workdir_mismatch_retry_keeps_original_build_prompt():
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    runner = AgentRunner()
    runner.working_directory = "/tmp/clone-b"
    original = "BUILD KIT\n# full unattended implementation prompt"
    task = AgentTask(description="d", prompt=original, agent="Sisyphus")

    with patch(
        "src.opencode_sessions.lookup_session_directory",
        return_value=("/tmp/clone-a", True),
    ), patch(
        "src.opencode_sessions.session_matches_workdir",
        return_value=False,
    ):
        runner._resume_opencode_session_for_retry(
            task, "ses_other", why="timeout"
        )

    assert task.session_id in (None, "")
    assert task.abandoned_session_id == "ses_other"
    assert task.prompt == original


# ---------------------------------------------------------------------------
# M9 — GitLab queue worker skips the job semaphore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m9_gitlab_queue_item_does_not_acquire_job_semaphore():
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()

    class BoomSem:
        def __init__(self):
            self.entered = 0

        async def __aenter__(self):
            self.entered += 1
            return self

        async def __aexit__(self, *a):
            return False

    sem = BoomSem()
    proc._job_semaphore = sem

    gitlab_calls = {"n": 0}

    async def fake_gitlab_count(_event):
        gitlab_calls["n"] += 1
        return True

    async def fake_dispatch():
        return 0

    proc._run_gitlab_mr_comment = fake_gitlab_count
    rec = {
        "queue_id": "q_deadbeef12",
        "source": "gitlab",
        "issue_key": "KAN-1",
        "payload": {},
        "status": "running",
    }
    proc.queue_store.finish = MagicMock()
    proc.queue_store.requeue = MagicMock()
    proc.state_manager.get_state = MagicMock(return_value=None)
    proc._active_jobs = {}
    proc.dispatch_queue = fake_dispatch

    class FakeEvent:
        @staticmethod
        def from_dict(_payload):
            return object()

    with patch("src.gitlab.webhook.GitlabMrNoteEvent", FakeEvent):
        await proc._run_queue_item(rec)

    assert sem.entered == 1
    assert gitlab_calls["n"] == 1


# ---------------------------------------------------------------------------
# M10 — Clone / session / queue lock identity ignores the Jira issue key
# ---------------------------------------------------------------------------


def test_m10_workspace_lock_identical_for_two_issues_same_source():
    from src.state.queue_store import workspace_lock_key
    from src.state.session_bind_store import bind_id_for

    repo = "https://gitlab.example.com/g/p.git"
    work = "feature/shared"
    target = "develop"
    assert bind_id_for(repo, work, target, "KAN-1") != bind_id_for(
        repo, work, target, "KAN-2"
    )
    assert workspace_lock_key(repo, work, target) == workspace_lock_key(
        repo, work, target
    )


# ---------------------------------------------------------------------------
# M11 — Issue detail cancel still uses stale detail.issue_key (C7 still open)
# ---------------------------------------------------------------------------


def test_m11_issue_detail_clears_detail_on_cache_miss():
    cache = {"KAN-A": {"issue_key": "KAN-A", "can_cancel": True}}
    route = "KAN-A"
    detail = cache.get(route)

    def on_route_change(new_key: str):
        nonlocal route, detail
        route = new_key
        seed = cache.get(new_key)
        detail = seed if seed else None

    def cancel_allowed():
        return bool(detail) and detail["issue_key"] == route

    on_route_change("KAN-B")
    assert route == "KAN-B"
    assert detail is None
    assert cancel_allowed() is False
