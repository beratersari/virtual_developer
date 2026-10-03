"""Dashboard pages must not fill the browser connection cap.

Chrome allows six HTTP/1.1 connections per host. The live socket uses one.
Stacked page reads used the rest, so the next page stayed on its empty state
until a refresh aborted them. These checks run the slot and page-load
modules and pin the call sites that keep a connection free.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def test_page_load_modules_match_their_tsx_checks() -> None:
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to run the dashboard load checks")
    for script in (
        "src/api/pageLoad.test.ts",
        "src/api/getSlots.test.ts",
        "src/util/artifacts.test.ts",
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


def test_gets_wait_for_a_slot_and_release_it() -> None:
    client = _read("src/api/client.ts")
    assert "await acquireGetSlot(ctrl.signal, kind)" in client
    assert "releaseGetSlot(held)" in client
    assert "method === 'GET'" in client
    live = _read("src/app/LiveProvider.tsx")
    assert "slot: 'background'" in live
    assert "ac.abort()" in live


def test_list_pages_abort_on_leave_and_keep_loading_until_this_view() -> None:
    for rel in (
        "src/pages/jobs/JobsPage.tsx",
        "src/pages/sessions/SessionsPage.tsx",
        "src/pages/sessions/SessionWorkspacePage.tsx",
        "src/pages/schedules/SchedulesPage.tsx",
        "src/pages/storage/StoragePage.tsx",
        "src/pages/jobs/JobDetailPage.tsx",
        "src/pages/issues/IssueDetailPage.tsx",
    ):
        assert "usePageLoad()" in _read(rel), rel
    jobs = _read("src/pages/jobs/JobsPage.tsx")
    assert "shownFor !== viewKey" in jobs
    assert "Loading jobs…" in jobs
    sessions = _read("src/pages/sessions/SessionsPage.tsx")
    assert "shownFor !== viewKey" in sessions
    assert "Loading sessions…" in sessions
    schedules = _read("src/pages/schedules/SchedulesPage.tsx")
    assert "shownPage !== page" in schedules
    assert "Loading schedules…" in schedules
    assert "fetchIssueTypes(undefined, ac.signal)" in schedules
    assert "fetchAzureProjects(collection.trim(), ac.signal)" in schedules
    detail = _read("src/pages/jobs/JobDetailPage.tsx")
    assert "artifactFetchDecision(" in detail
    assert "fetchJobArtifacts(id, ac.signal)" in detail
