"""Critical regressions in the last 20 commits.

Each test states the safe outcome. They fail on the current tree.
"""

import json
from pathlib import Path

from src.azure.client import AzureDevOpsClient
from src.issue_git_spec import parse_issue_mode
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.work_modes import apply_saved_modes


def _params(mode: str) -> str:
    return (
        "{params}\n"
        f"Mode: {mode}\n"
        "Repository: https://gitlab.example.com/acme/app.git\n"
        "Source branch: feature/x\n"
        "Target branch: develop\n"
        "{params}"
    )


def test_saved_mode_named_implement_is_the_mode_that_runs(monkeypatch):
    """A row saved as implement must run that row, not the old build alias."""
    from src.config import settings
    from src.scheduler.service import _canonical_mode

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [{"name": "implement", "behavior": "plan", "agent": "derman-docs"}]
    )
    text = _params("implement")
    assert parse_issue_mode("", text) == "implement"
    assert _canonical_mode("implement") == "implement"
    assert WorkflowRouter.route_issue("KAN-1", "", text) == WorkflowType.PLANNING
    assert (
        WorkflowRouter.agent_for_issue("", text, WorkflowType.PLANNING)
        == "derman-docs"
    )


def test_job_intake_honors_a_changed_builtin_delivery(monkeypatch):
    """Mode: plan with a saved build delivery must start execution.

    WorkflowRouter already does this. Intake uses JobProcessor._resolve_workflow,
    which returns planning as soon as the mode token is plan.
    """
    from src.config import settings
    from src.processor import JobProcessor

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [
            {"name": "plan", "behavior": "build", "agent": "derman-plan"},
            {"name": "build", "behavior": "build", "agent": "derman-build"},
            {"name": "test", "behavior": "test", "agent": "derman-test"},
        ]
    )
    text = _params("plan")
    assert lookup_behavior_is_build(text)
    resolved = JobProcessor._resolve_workflow(object(), "KAN-1", "", text)
    assert resolved == WorkflowType.EXECUTION


def lookup_behavior_is_build(text: str) -> bool:
    from src.work_modes import lookup, workflow_value

    spec = lookup(parse_issue_mode("", text))
    return workflow_value(str(spec.get("behavior") or "")) == "execution"


def test_modes_save_keeps_a_changed_builtin_delivery():
    """Settings save must send the stored behavior for plan/build/test.

    The API accepts plan with behavior build. The page then has that
    behavior on the draft. The next Modes save must not replace it with
    the mode name.
    """
    page = Path("web/src/pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    start = page.index("if (dirtyKeys.has('work_modes'))")
    block = page[start : page.index("if (dirtyKeys.has('project_repositories'))", start)]
    assert "row.builtin ? row.name" not in block
    assert "behavior: row.behavior" in block


def test_configured_collection_matches_host_case_and_default_port(monkeypatch):
    """The same collection is still configured when the host case or :443 differs."""
    from src.config import Settings

    saved = "https://tfs.example.com/tfs/DefaultCollection"
    settings = Settings(
        azure_collection_pats=json.dumps({saved: "pat-value"}),
    )
    monkeypatch.setattr("src.config.settings", settings)
    monkeypatch.setattr("src.azure.client.settings", settings)

    for variant in (
        "https://TFS.example.com/tfs/DefaultCollection",
        "https://tfs.example.com:443/tfs/DefaultCollection",
    ):
        assert settings.azure_pat_for_collection(variant) == "pat-value"
        client = AzureDevOpsClient(collection_url=variant)
        assert client.pat == "pat-value"
        assert client.api_base
