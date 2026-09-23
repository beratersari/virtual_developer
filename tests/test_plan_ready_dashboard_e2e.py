"""HTTP flows for plan-ready jobs: revise, list badge, and plan file.

Uses a local Jira server, the real dashboard app, and the real job store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.jira.poller import JiraPoller
from src.paths import plans_dir
from src.state.models import TaskStatus
from tests.test_daily_operator_review import (
    _JiraBoard,
    _close_jira,
    _plan_file,
    _processor_on,
    _serve_jira,
)

ROOT = Path(__file__).resolve().parents[1]
JOB_PAGE = ROOT / "web" / "src" / "pages" / "jobs" / "JobDetailPage.tsx"


def _job(store, key: str, *, status: str, started: str, workflow: str = "planning"):
    job = store.create_job(
        issue_key=key,
        summary=f"{status} {started}",
        workflow_type=workflow,
        status=status,
    )
    stored = store.update_job(job["job_id"], status=status, started_at=started)
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_e2e_revise_after_implement_then_only_latest_job_shows_the_plan(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """Operator revises from the dashboard, then opens the jobs list.

    The next poll must revise. Only the newest job stays Plan ready and
    carries the plan file. The earlier plan row is Superseded.
    """
    board = _JiraBoard(
        status_name="In Progress",
        category="indeterminate",
        labels=["plan_ready", "team"],
        description=(
            "{params}\n"
            "Repository: https://gitlab.example.com/acme/app.git\n"
            "Source branch: feature/login\n"
            "Target branch: develop\n"
            "Mode: plan\n"
            "{params}"
        ),
    )
    board.transitions = [
        {
            "id": "31",
            "name": "Done",
            "to": {"name": "Done", "statusCategory": {"key": "done"}},
        }
    ]
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        sm = proc.state_manager
        sm.create_state("KAN-1", board.summary, board.description)
        sm.update_state("KAN-1", status=TaskStatus.PLAN_READY)
        plan = _plan_file("KAN-1")
        plan.write_text("# Login\n\nUse Redis.\n", encoding="utf-8")
        assert plan == plans_dir() / "KAN-1.md"

        store = proc.job_store
        older = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
        latest = _job(store, "KAN-1", status="plan_ready", started="2026-09-22T11:00:00")
        monkeypatch.setattr("src.dashboard.api.job_store", store)
        monkeypatch.setattr("src.dashboard.service.default_job_store", store)

        app = create_dashboard_app(processor=proc, state_manager=sm)
        client = TestClient(app)

        implemented = client.post("/api/tasks/KAN-1/plan-execute")
        assert implemented.status_code == 200, implemented.text
        assert "plan_execute" in board.labels
        assert "plan_ready" not in board.labels
        assert board.transition_posts == []

        revised = client.post(
            "/api/tasks/KAN-1/plan-refactor",
            json={"prompt": "Use Redis instead of memory"},
        )
        assert revised.status_code == 200, revised.text
        assert revised.json()["ok"] is True
        assert "plan_refactor" in board.labels
        assert "plan_execute" not in board.labels
        assert "plan_ready" not in board.labels
        assert any("Use Redis instead of memory" in c["body"] for c in board.comments)

        poller = JiraPoller(client=proc.jira_client, board_id="1", state_manager=sm)
        poller._seen_issues.add("KAN-1")
        rows = [row for row in poller.poll_board() if row.get("key") == "KAN-1"]
        assert rows and rows[0].get("_plan_handoff") == "refactor"

        listed = {
            item["job_id"]: item["status"]
            for item in client.get("/api/jobs", params={"page_size": 20}).json()["jobs"]
        }
        assert listed[older["job_id"]] == "superseded"
        assert listed[latest["job_id"]] == "plan_ready"

        current = client.get(f"/api/jobs/{latest['job_id']}").json()
        previous = client.get(f"/api/jobs/{older['job_id']}").json()
        assert "Use Redis." in current["plan"]["text"]
        assert current["plan_followup"]["actions"] is True
        assert previous["plan"] is None
        assert previous["job"]["status"] == "superseded"
        assert previous["plan_followup"]["kind"] == "newer"
        assert previous["plan_followup"]["job_id"] == latest["job_id"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


@pytest.mark.asyncio
async def test_e2e_revise_from_the_latest_failed_plan_job(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    """A failed revise leaves the ticket in error. Revise on that job tries again."""
    board = _JiraBoard(
        status_name="In Progress",
        category="indeterminate",
        labels=["plan_refactor"],
        description="Mode: plan",
    )
    httpd = _serve_jira(board)
    proc = _processor_on(httpd, tmp_path, monkeypatch)
    try:
        sm = proc.state_manager
        sm.create_state("KAN-1", "plan login", "Mode: plan")
        sm.update_state("KAN-1", status=TaskStatus.ERROR, error_message="agent hung")
        plan = _plan_file("KAN-1")
        plan.write_text("# Login\n\nKeep Redis.\n", encoding="utf-8")
        store = proc.job_store
        _job(store, "KAN-1", status="plan_ready", started="2026-09-22T10:00:00")
        failed = _job(store, "KAN-1", status="error", started="2026-09-22T12:00:00")
        monkeypatch.setattr("src.dashboard.api.job_store", store)
        monkeypatch.setattr("src.dashboard.service.default_job_store", store)
        app = create_dashboard_app(processor=proc, state_manager=sm)
        client = TestClient(app)

        detail = client.get(f"/api/jobs/{failed['job_id']}").json()
        assert detail["plan_followup"]["revise"] is True
        assert detail["plan_followup"]["actions"] is False
        assert "Keep Redis." in detail["plan"]["text"]

        revised = client.post(
            "/api/tasks/KAN-1/plan-refactor",
            json={"prompt": "Use the database instead"},
        )
        assert revised.status_code == 200, revised.text
        assert sm.get_state("KAN-1").status == TaskStatus.PLAN_READY
        assert "plan_refactor" in board.labels
        assert any("Use the database instead" in c["body"] for c in board.comments)

        poller = JiraPoller(client=proc.jira_client, board_id="1", state_manager=sm)
        poller._seen_issues.add("KAN-1")
        rows = [row for row in poller.poll_board() if row.get("key") == "KAN-1"]
        assert rows and rows[0].get("_plan_handoff") == "refactor"
    finally:
        httpd.shutdown()
        httpd.server_close()
        _close_jira(proc)


def test_e2e_plan_tab_matches_the_prompt_tab():
    """Plan content lives in the same tab row as Prompt, not above the page."""
    page = JOB_PAGE.read_text(encoding="utf-8")
    assert "id: 'plan'" in page or 'id: "plan"' in page
    assert "PromptBlock" in page
    assert 'title="plan"' in page
    assert "followup?.revise" in page
    assert "tab === 'plan'" in page


def test_e2e_running_dashboard_serves_the_plan_tab():
    """The process on :5173 must be serving this Plan tab, when it is up."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:5173/", timeout=3) as resp:
            html = resp.read().decode("utf-8", "replace")
            status = resp.status
    except Exception as exc:
        pytest.skip(f"dashboard SPA is not running: {exc}")
    assert status == 200
    marker = "index-"
    src = ""
    for part in html.split('"'):
        if part.startswith("/assets/index-") and part.endswith(".js"):
            src = part
            break
    assert src.startswith("/assets/") and marker in src
    with urllib.request.urlopen(f"http://127.0.0.1:5173{src}", timeout=5) as resp:
        bundle = resp.read().decode("utf-8", "replace")
    assert "No plan file on disk for this issue." in bundle
