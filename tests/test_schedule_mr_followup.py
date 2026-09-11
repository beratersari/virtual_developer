"""Scheduled follow-up prompt on an existing GitLab MR.

Uses the in-repo simulated GitLab HTTP server (real notes API) and a real
schedule store. The agent run is a processor subclass that posts the answer
note — no MagicMock.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from werkzeug.serving import make_server

from src.dashboard.api import create_dashboard_app
from src.gitlab.client import GitlabClient, project_from_repo_url
from src.processor import JobProcessor
from src.scheduler.service import (
    dispatch_schedule_now,
    format_dashboard_mr_prompt_note,
    preview_mr_followup,
    schedule_mr_followup,
    wait_inflight_dispatches,
)
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore


def test_dashboard_prompt_note_is_marked_from_ui():
    body = format_dashboard_mr_prompt_note("  Please add a log line.  ")
    assert body.startswith("*Yaver* — written in the ops dashboard")
    assert body.endswith("Please add a log line.")
    from src.gitlab.webhook import decide_gitlab_note_webhook

    decision = decide_gitlab_note_webhook(
        {
            "object_kind": "note",
            "object_attributes": {
                "noteable_type": "MergeRequest",
                "note": body,
                "id": 9,
            },
            "merge_request": {"iid": 1, "title": "feat(KAN-1): x"},
            "project": {"path_with_namespace": "acme/demo"},
        },
        headers={"X-Gitlab-Event": "Note Hook", "X-Gitlab-Token": "tok"},
        enabled=True,
        secret="tok",
        bot_mentions=["yaver"],
    )
    assert decision.accepted is False
    assert "bot reply" in (decision.reason or "")


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


class _SimGitlab:
    def __init__(self, port: int) -> None:
        import simulated_gitlab_server as sim

        self._mod = sim
        sim.SIM_PORT = port
        sim.reset_demo_data()
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.repo = f"{self.url}/acme/demo.git"
        self._httpd = make_server("127.0.0.1", port, sim.app)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"simulated GitLab did not listen on {self.port}")

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        self._thread.join(timeout=2)
        self._mod.reset_demo_data()


@pytest.fixture
def sim_gl():
    srv = _SimGitlab(_free_port())
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_project_from_repo_url_variants():
    assert project_from_repo_url("https://gitlab.com/group/sub/repo.git") == (
        "gitlab.com",
        "group/sub/repo",
    )
    assert project_from_repo_url("git@gitlab.com:acme/demo.git") == (
        "gitlab.com",
        "acme/demo",
    )
    assert project_from_repo_url(
        "http://127.0.0.1:8091/acme/demo/-/merge_requests/12"
    ) == ("127.0.0.1:8091", "acme/demo")


def test_preview_and_schedule_mr_against_sim(tmp_path, monkeypatch, sim_gl):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    preview = preview_mr_followup(sim_gl.repo, 1)
    assert preview["ok"] is True
    assert preview["gitlab_project"] == "acme/demo"
    assert preview["mr_iid"] == 1
    assert preview["source_branch"] == "feature/login"
    assert preview["target_branch"] == "develop"
    assert "login" in (preview.get("title") or "").lower()

    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")
    result = schedule_mr_followup(
        repository_url=sim_gl.repo,
        mr_iid=1,
        prompt="Please add a log line when login fails.",
        scheduled_at=when,
        store=store,
    )
    assert result["ok"] is True
    rec = result["schedule"]
    assert rec["source"] == "gitlab_mr"
    assert rec["mr_iid"] == 1
    assert rec["gitlab_project"] == "acme/demo"
    assert rec["status"] == "scheduled"
    assert "log line" in rec["description"]
    # Prompt is not posted until fire time
    client = GitlabClient(host=f"127.0.0.1:{sim_gl.port}")
    listed = client.get_merge_request("acme/demo", 1)
    assert listed is not None
    notes = _list_notes(sim_gl)
    assert notes == []


def _list_notes(sim_gl: _SimGitlab) -> list:
    import httpx

    r = httpx.get(
        f"{sim_gl.url}/api/v4/projects/1/merge_requests/1/notes",
        timeout=5.0,
        verify=False,
    )
    r.raise_for_status()
    return list(r.json() or [])


class _AnswerProcessor(JobProcessor):
    """Skip OpenCode; post a canned answer on the MR like a finished job."""

    async def _run_gitlab_mr_comment(self, event) -> bool:
        from src.state.models import JiraAgentState

        st = self.state_manager.get_state(event.issue_key)
        if st is None:
            st = self.state_manager.create_state(
                event.issue_key, event.mr_title, event.prompt
            )
        self.state_manager.update_state(
            event.issue_key,
            force=True,
            status=TaskStatus.COMPLETED,
            metadata={
                "source": "gitlab",
                "gitlab_host": event.host,
                "gitlab_project": event.project_path,
                "gitlab_project_id": event.project_id or None,
                "gitlab_mr_iid": event.mr_iid,
                "merge_request_url": event.mr_url,
                "gitlab_discussion_id": event.discussion_id,
                "gitlab_note_id": event.note_id,
                "workflow_type": "gitlab_mr",
            },
        )
        live = self.state_manager.get_state(event.issue_key) or st
        assert isinstance(live, JiraAgentState)
        posted = self._post_gitlab_mr_reply(live, "*Yaver*\n\nAdded the log line.")
        return bool(posted)


@pytest.mark.asyncio
async def test_dispatch_posts_prompt_then_answer(tmp_path, monkeypatch, sim_gl):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_projects", "KAN")
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    proc = _AnswerProcessor()
    proc.state_manager = sm

    when = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    created = schedule_mr_followup(
        repository_url=sim_gl.repo,
        mr_iid=1,
        prompt="Please add a log line when login fails.",
        scheduled_at=when,
        store=store,
    )
    sid = created["schedule"]["schedule_id"]
    launched = dispatch_schedule_now(sid, processor=proc, store=store)
    assert launched.get("ok") is True
    await wait_inflight_dispatches()

    notes = _list_notes(sim_gl)
    bodies = [str(n.get("body") or "") for n in notes]
    prompt_notes = [
        n
        for n in notes
        if "Please add a log line" in str(n.get("body") or "")
        and "written in the ops dashboard" in str(n.get("body") or "")
    ]
    answer_notes = [
        n for n in notes if "Added the log line" in str(n.get("body") or "")
    ]
    assert prompt_notes, bodies
    assert prompt_notes[0].get("body", "").lstrip().startswith("*Yaver*")
    assert answer_notes, bodies
    prompt_did = str(prompt_notes[0].get("discussion_id") or "")
    answer_did = str(answer_notes[0].get("discussion_id") or "")
    assert prompt_did
    assert answer_did == prompt_did
    live = store.get(sid)
    assert live is not None
    assert live.get("status") == "dispatched"


def test_api_preview_and_create_mr_schedule(tmp_path, monkeypatch, sim_gl):
    from src.config import settings
    from src.dashboard import api as api_mod
    from src.scheduler import service as sched_mod

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    monkeypatch.setattr(api_mod, "schedule_store", store)
    monkeypatch.setattr(sched_mod, "schedule_store", store)
    monkeypatch.setattr(settings, "jira_projects", "KAN")

    app = create_dashboard_app(processor=None)
    client = TestClient(app)
    preview = client.get(
        "/api/schedules/mr-preview",
        params={"repository_url": sim_gl.repo, "mr_iid": 1},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["ok"] is True
    assert body["mr_iid"] == 1

    when = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    created = client.post(
        "/api/schedules/mr",
        json={
            "repository_url": sim_gl.repo,
            "mr_iid": 1,
            "prompt": "Explain the login change.",
            "scheduled_at": when,
        },
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["ok"] is True
    rec = payload["schedule"]
    assert rec["source"] == "gitlab_mr"
    assert rec["mr_iid"] == 1
    listed = client.get("/api/schedules").json()["schedules"]
    assert any(r.get("schedule_id") == rec["schedule_id"] for r in listed)


def test_preview_missing_mr_is_hard_fail(sim_gl):
    result = preview_mr_followup(sim_gl.repo, 99)
    assert result["ok"] is False
    assert "99" in (result.get("error") or "")
