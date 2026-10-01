"""Copied dashboard links use the page name, not a bare Yaver title."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.page_title import (
    apply_document_title,
    document_title,
    job_page_name,
)
from src.state.job_store import job_store

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def test_page_title_script():
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to run the dashboard title checks")
    proc = subprocess.run(
        [npx, "--yes", "tsx", "src/util/pageTitle.test.ts"],
        cwd=WEB,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


def test_document_title_matches_the_open_page():
    assert document_title("/jobs") == "Yaver - Jobs"
    assert document_title("/jobs/in-flight/2") == "Yaver - Jobs - In flight"
    assert document_title("/jobs/queue") == "Yaver - Jobs - Queue"
    assert document_title("/jobs/plan-ready") == "Yaver - Jobs - Plan ready"
    assert document_title("/scheduled/new/jira") == "Yaver - New issue"
    assert document_title("/scheduled/new/azure") == "Yaver - New issue - Azure work item"
    assert document_title("/scheduled/mr") == "Yaver - Existing MR"
    assert document_title("/scheduled/pr") == "Yaver - Existing PR"
    assert document_title("/settings/gitlab") == "Yaver - Settings - GitLab"
    assert document_title("/settings/agent") == "Yaver - Settings - Agent"
    assert document_title("/analytics/7d") == "Yaver - Analytics - 7 days"
    assert document_title("/analytics") == "Yaver - Analytics"
    assert document_title("/poll") == "Yaver - Board"
    assert document_title("/storage") == "Yaver - Storage"
    assert document_title("/sessions") == "Yaver - Sessions"
    assert (
        document_title("/analytics/reviews", "origin=ours&state=opened")
        == "Yaver - Opened by us · open"
    )
    assert (
        document_title("/jobs/job_1/transcript", record_name="Fix login")
        == "Yaver - Fix login - Transcript"
    )
    assert document_title("/jobs/job_1", record_name="Fix login") == "Yaver - Fix login"
    assert job_page_name("  Fix\nlogin  ", "daemon", "KAN-1") == "Fix login - Daemon"
    assert document_title("/tasks/KAN-9/logs", record_name="Board bug") == (
        "Yaver - Board bug - System logs"
    )
    assert document_title("/tasks/KAN-9") == "Yaver - KAN-9"


def test_apply_document_title_escapes_the_job_name():
    raw = "<html><head><title>Yaver</title></head><body>ok</body></html>"
    updated = apply_document_title(raw, 'Yaver - a <b> & "c"')
    assert "<title>Yaver - a &lt;b&gt; &amp; &quot;c&quot;</title>" in updated
    assert 'property="og:title" content="Yaver - a &lt;b&gt; &amp; &quot;c&quot;"' in updated
    assert apply_document_title("<html>ok</html>", "Yaver - Jobs") == "<html>ok</html>"


def test_spa_html_title_follows_the_path(tmp_path, monkeypatch):
    dist = tmp_path / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>Yaver</title></head><body>ok</body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.dashboard.api._static_dir", lambda: dist)

    def _job(job_id: str):
        if job_id == "job_1":
            return {"summary": "Fix login", "issue_key": "KAN-1"}
        if job_id == "job_blank":
            return {"summary": "  ", "issue_key": "KAN-2"}
        return None

    monkeypatch.setattr(job_store, "get_job", _job)

    class _State:
        issue_summary = "Board bug"

    class _States:
        def get_state(self, key: str):
            return _State() if key == "KAN-9" else None

    app = create_dashboard_app(state_manager=_States())  # type: ignore[arg-type]
    client = TestClient(app)

    new_issue = client.get("/scheduled/new/jira")
    assert new_issue.status_code == 200
    assert "<title>Yaver - New issue</title>" in new_issue.text
    assert 'property="og:title" content="Yaver - New issue"' in new_issue.text

    job = client.get("/jobs/job_1/transcript")
    assert "<title>Yaver - Fix login - Transcript</title>" in job.text

    blank = client.get("/jobs/job_blank")
    assert "<title>Yaver - KAN-2</title>" in blank.text

    issue = client.get("/tasks/KAN-9/logs")
    assert "<title>Yaver - Board bug - System logs</title>" in issue.text

    settings = client.get("/settings/projects")
    assert "<title>Yaver - Settings - Projects</title>" in settings.text
    assert "ok" in settings.text


def test_pages_publish_the_title_they_show():
    shell = (WEB / "src" / "app" / "Shell.tsx").read_text(encoding="utf-8")
    assert "PageTitleRoot" in shell
    job = (WEB / "src" / "pages" / "jobs" / "JobDetailPage.tsx").read_text(encoding="utf-8")
    assert "usePageTitle(jobPageName(" in job
    issue = (WEB / "src" / "pages" / "issues" / "IssueDetailPage.tsx").read_text(encoding="utf-8")
    assert "usePageTitle(" in issue and "issuePageName(null, null, section)" in issue
    session = (WEB / "src" / "pages" / "sessions" / "SessionWorkspacePage.tsx").read_text(
        encoding="utf-8"
    )
    assert "usePageTitle(workspacePageName(" in session
    gate = (WEB / "src" / "auth" / "DashboardAuthGate.tsx").read_text(encoding="utf-8")
    assert "applyDocumentTitle('Sign in')" in gate
    reviews = (WEB / "src" / "pages" / "analytics" / "AnalyticsReviewsPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "reviewsPageName(origin, state)" in reviews
