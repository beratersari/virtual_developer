"""Placeholder helpers for plan/build prompt files."""

from src.orchestrator.prompt_kit import (
    clear_prompt_kit_cache,
    substitute_issue_key,
    substitute_placeholders,
)


def test_substitute_issue_key():
    assert substitute_issue_key("hi {ISSUE_KEY}", "P-1") == "hi P-1"
    assert substitute_issue_key("", "X") == ""


def test_substitute_placeholders_defaults():
    out = substitute_placeholders(
        "key={ISSUE_KEY} br={WORK_BRANCH} plan={PLAN_PATH}",
        issue_key="T-2",
    )
    assert "T-2" in out
    assert "feature/T-2" in out
    assert ".sisyphus/plans/T-2.md" in out


def test_clear_prompt_kit_cache_noop_safe():
    clear_prompt_kit_cache()
