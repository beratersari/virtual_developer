"""Dashboard tab URLs stay in the address bar.

The path rules live in web/src and are executed by the tsx checks.
This test also pins the routes and the click handlers that call them,
so a helper can stay green while a page goes back to local state.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
APP = (WEB / "src" / "app" / "App.tsx").read_text(encoding="utf-8")


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def test_section_url_modules_match_their_tsx_checks() -> None:
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to run the dashboard section URL checks")
    for script in (
        "src/pages/jobs/jobTabUrl.test.ts",
        "src/pages/pageSectionUrl.test.ts",
    ):
        proc = subprocess.run(
            [npx, "--yes", "tsx", script],
            cwd=WEB,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, (
            f"{script} failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


def test_routes_keep_a_section_slot_for_each_tabbed_page() -> None:
    for route in (
        'path="/jobs/in-flight/:page?"',
        'path="/jobs/queue/:page?"',
        'path="/jobs/error/:page?"',
        'path="/jobs/completed/:page?"',
        'path="/jobs/cancelled/:page?"',
        'path="/jobs/plan-ready/:page?"',
        'path="/jobs/:jobId/:section?"',
        'path="/analytics/reviews/:page?"',
        'path="/analytics/:period"',
        'path="/tasks/:issueKey/:section?"',
        'path="/scheduled/:mode?/:tracker?/:page?"',
        'path="/sessions/:workspaceId"',
        'path="/settings/:section?"',
    ):
        assert route in APP, route
    shell = _read("src/app/Shell.tsx")
    assert "to: '/scheduled/jira'" in shell
    assert "to: '/settings/jira'" in shell


def test_tab_clicks_write_the_section_into_the_url() -> None:
    job = _read("src/pages/jobs/JobDetailPage.tsx")
    assert "navigate(jobTabPath(jobId, next))" in job
    assert "jobTabFromSection(section)" in job

    issue = _read("src/pages/issues/IssueDetailPage.tsx")
    assert "navigate(issueTabPath(issueKey, next))" in issue
    assert "issueTabFromSection(section)" in issue

    jobs = _read("src/pages/jobs/JobsPage.tsx")
    assert "navigate(jobsFilterPath(f.id))" in jobs
    assert "jobsFilterFromPath(pathname)" in jobs
    assert "navigate(withListPage(jobsFilterPath(statusFilter), currentPage + 1))" in jobs

    sessions = _read("src/pages/sessions/SessionsPage.tsx")
    assert "navigate(withListPage('/sessions', currentPage + 1))" in sessions

    reviews = _read("src/pages/analytics/AnalyticsReviewsPage.tsx")
    assert "withListPage('/analytics/reviews', nextPage)" in reviews

    analytics = _read("src/pages/analytics/AnalyticsPage.tsx")
    assert "navigate(analyticsPeriodPath(p.id))" in analytics
    assert "analyticsPeriodFromParam(periodParam)" in analytics
    assert "jobsFilterPath('completed')" in analytics
    assert "jobsFilterPath('error')" in analytics
    assert "jobsFilterPath('cancelled')" in analytics

    settings = _read("src/pages/settings/SettingsPage.tsx")
    assert "navigate(settingsSectionPath(id))" in settings
    assert "canonicalSettingsPath(sectionParam)" in settings
    assert "settingsHere(sectionParam)" in settings

    scheduled = _read("src/pages/schedules/SchedulesPage.tsx")
    assert "navigate(schedulePath('new'))" in scheduled
    assert "navigate(schedulePath('new', 'jira'))" in scheduled
    assert "navigate(schedulePath('new', 'azure'))" in scheduled
    assert "navigate(schedulePath('existing', 'jira'))" in scheduled
    assert "canonicalSchedulePath(modeParam, trackerParam, pageParam)" in scheduled
    assert "scheduleHere(modeParam, trackerParam, pageParam)" in scheduled
    assert "navigate(withListPage(schedulePath(mode, tracker), currentPage + 1))" in scheduled
