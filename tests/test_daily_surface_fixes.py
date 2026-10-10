"""UI contracts for the daily-usage fixes. step-finish is not one of them."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SETTINGS = WEB / "src" / "pages" / "settings" / "SettingsPage.tsx"
JOBS = WEB / "src" / "pages" / "jobs" / "JobsPage.tsx"
OVERVIEW = WEB / "src" / "pages" / "jobs" / "JobOverview.tsx"
SCHEDULES = WEB / "src" / "pages" / "schedules" / "SchedulesPage.tsx"


def _run_tsx(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "tsx", str(script.relative_to(WEB))],
        cwd=WEB,
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )


def test_settings_load_failure_is_not_titled_as_a_save():
    text = SETTINGS.read_text(encoding="utf-8")
    assert 'title="Could not load settings"' in text
    assert "setLoadError" in text
    assert 'title="Could not save"' in text
    assert "Code review (below)" not in text
    assert "@name /review and @name /ask start a" in text
    assert "parseSettingsNumber(e.target.value)" in text
    assert "requireSettingsNumber(" in text
    bare = text.replace("parseSettingsNumber(e.target.value)", "")
    assert "Number(e.target.value)" not in bare


def test_blank_settings_numbers_and_jobs_filter_echo():
    numbers = _run_tsx(WEB / "src" / "pages" / "settings" / "settingsNumbers.test.ts")
    assert numbers.returncode == 0, numbers.stdout + numbers.stderr
    assert "ok" in numbers.stdout
    jobs = _run_tsx(WEB / "src" / "util" / "jobs.test.ts")
    assert jobs.returncode == 0, jobs.stdout + jobs.stderr
    assert "ok" in jobs.stdout


def test_queue_failure_and_retry_error_stay_visible():
    jobs = JOBS.read_text(encoding="utf-8")
    load_queue = jobs.split("const loadQueue", 1)[1].split("const load =", 1)[0]
    catch = load_queue.split("} catch", 1)[1].split("} finally", 1)[0]
    assert "setQueueError" in catch
    assert "setQueueReady(true)" not in catch
    assert "jobsFilterEcho(debouncedFilter)" in jobs
    assert "debouncedFilter.toUpperCase()" not in jobs
    overview = OVERVIEW.read_text(encoding="utf-8")
    assert "slice(0, 160)" not in overview
    assert "whitespace-pre-wrap break-words" in overview


def test_schedule_action_errors_replace_the_empty_list():
    text = SCHEDULES.read_text(encoding="utf-8")
    assert "rows.length === 0 && !error" in text
    assert "listPending && !error" in text
    run = text.split('title="Run this job now?"', 1)[1].split(
        'title="Cancel this schedule?"', 1
    )[0]
    run_catch = run.split("} catch", 1)[1].split("} finally", 1)[0]
    assert "setRunId(null)" in run_catch
    assert "Could not run this schedule" in run_catch
    assert "reload()" not in run_catch
    cancel = text.split('title="Cancel this schedule?"', 1)[1].split(
        "function ExistingMr", 1
    )[0]
    cancel_catch = cancel.split("} catch", 1)[1].split("} finally", 1)[0]
    assert "setCancelId(null)" in cancel_catch
    assert "Could not cancel this schedule" in cancel_catch
    assert "reload()" not in cancel_catch
