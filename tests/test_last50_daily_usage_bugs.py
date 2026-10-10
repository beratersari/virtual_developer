"""Daily-usage behavior the operator notes require.

A process stop still posts the re-queue comment and then resumes the
running queue row. That pair is intentional and is not asserted here.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx"

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
