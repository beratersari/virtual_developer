"""A saved custom mode must pass the same git template check as plan/build/test."""

from src.issue_git_spec import require_issue_git_spec
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


def test_saved_custom_mode_is_accepted_by_the_git_template(monkeypatch):
    """Settings can add Mode: docs. Preparing the clone must not reject it."""
    from src.config import settings

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [
            {"name": "plan", "behavior": "plan", "agent": "derman-plan"},
            {"name": "build", "behavior": "build", "agent": "derman-build"},
            {"name": "test", "behavior": "test", "agent": "derman-test"},
            {"name": "docs", "behavior": "build", "agent": "derman-docs"},
        ]
    )
    spec = require_issue_git_spec("Add docs", _params("docs"))
    assert spec.mode == "docs"
    assert spec.repository_url == "https://gitlab.example.com/acme/app.git"
    assert spec.source_branch == "feature/x"
    assert spec.target_branch == "develop"


def test_schedule_accepts_a_saved_custom_mode(monkeypatch):
    from src.config import settings
    from src.scheduler.service import _canonical_mode

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [{"name": "docs", "behavior": "build", "agent": "derman-docs"}]
    )
    assert _canonical_mode("docs") == "docs"
    assert _canonical_mode("implement") == "build"
