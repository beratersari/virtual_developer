"""Daily-usage behavior the operator notes require.

A process stop still posts the re-queue comment and then resumes the
running queue row. That pair is intentional and is not asserted here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx"
JOBS = ROOT / "web" / "src" / "pages" / "jobs" / "JobsPage.tsx"

_BUILD = (
    "{params}\n"
    "Repository: https://gitlab.example.com/acme/app.git\n"
    "Source branch: develop\n"
    "Target branch: main\n"
    "*Mode:* *build*\n"
    "{params}\n"
)


def test_wiki_bold_mode_value_is_still_that_mode():
    """Jira bolds the value as well as the label: ``*Mode:* *build*``."""
    from src.issue_git_spec import parse_issue_git_spec, parse_issue_mode
    from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType

    spec, err = parse_issue_git_spec("Fix login", _BUILD)
    assert err is None
    assert spec is not None
    assert spec.mode == "build"
    assert parse_issue_mode("", _BUILD) == "build"
    assert (
        WorkflowRouter.route_issue("KAN-1", "Fix login", _BUILD)
        == WorkflowType.EXECUTION
    )


def test_settings_load_failure_dialog_is_on_screen():
    """A failed settings load mounts the load dialog before the form."""
    text = SETTINGS.read_text(encoding="utf-8")
    early = text.split("if (!settings || !draft)", 1)[1].split(
        "const saveButton", 1
    )[0]
    assert 'title="Could not load settings"' in early


def test_jobs_fetch_failure_does_not_say_the_filter_is_empty():
    """A failed jobs request must not replace the page with an empty list."""
    text = JOBS.read_text(encoding="utf-8")
    load = text.split("const load = useCallback", 1)[1]
    catch = load.split("} catch (e) {", 1)[1].split("} finally", 1)[0]
    assert "jobs: []" not in catch
    before_table = text.split("shownFor !== viewKey && !error", 1)[1].split(
        "<JobsTable", 1
    )[0]
    assert "error && filteredJobs.length === 0" in before_table


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def test_poller_off_still_assigns_the_trigger_user(processor, monkeypatch):
    """Job start still assigns when the board poller is off and Jira is configured."""
    from src.config import settings

    monkeypatch.setattr(settings, "jira_enabled", False)
    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")
    monkeypatch.setattr(settings, "jira_api_token", "token")
    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    monkeypatch.setattr(settings, "trigger_assignee_names", "")
    monkeypatch.setattr(settings, "trigger_mentions", "")
    calls: list[tuple[str, str]] = []

    def _assign(key, name):
        calls.append((key, name))
        return True

    processor.jira_client.assign_issue = _assign
    processor.jira_client.get_issue = lambda key, fields=None: {
        "fields": {"assignee": None}
    }
    processor.jira_client.is_cloud = False
    assert processor._assign_jira_to_pat_user("KAN-50") is True
    assert calls == [("KAN-50", "devbot")]


def test_poller_off_without_jira_does_not_assign(processor, monkeypatch):
    """No host or token still skips assign when the poller is off."""
    from src.config import settings

    monkeypatch.setattr(settings, "jira_enabled", False)
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    called = False

    def _assign(key, name):
        nonlocal called
        called = True
        return True

    processor.jira_client.assign_issue = _assign
    assert processor._assign_jira_to_pat_user("KAN-51") is False
    assert called is False
