"""Agent Ticket / {ISSUE_KEY} is numeric for work items; local key stays WIT-…"""

from __future__ import annotations

import pytest

from src.azure.keys import (
    azure_work_item_key,
    is_azure_work_item_key,
    parse_azure_work_item_key,
    prompt_ticket_label,
)
from src.orchestrator.prompt_builder import PromptBuilder


@pytest.fixture(autouse=True)
def _clear_prompt_cache():
    PromptBuilder.clear_prompt_file_cache()
    yield
    PromptBuilder.clear_prompt_file_cache()


def test_label_wit_project_id_is_number_only():
    assert prompt_ticket_label("WIT-BETA-42") == "42"
    assert prompt_ticket_label("WIT-DEMO-42") == "42"
    assert prompt_ticket_label("wit-beta-42") == "42"


def test_label_multi_segment_project():
    assert prompt_ticket_label("WIT-FOO-BAR-9") == "9"
    assert parse_azure_work_item_key("WIT-FOO-BAR-9") == ("FOO-BAR", 9)


def test_label_legacy_bare_numeric_key():
    assert is_azure_work_item_key("42")
    assert prompt_ticket_label("42") == "42"


def test_label_generated_key_roundtrip():
    key = azure_work_item_key("Beta", 42)
    assert key == "WIT-BETA-42"
    assert prompt_ticket_label(key) == "42"


def test_label_jira_unchanged():
    assert prompt_ticket_label("KAN-12") == "KAN-12"
    assert prompt_ticket_label("PROJ-1") == "PROJ-1"


def test_label_pr_and_mr_fallbacks_unchanged():
    assert prompt_ticket_label("AZ-DEFAULTCOLLECTION-DEMO-APP-9") == (
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9"
    )
    assert prompt_ticket_label("GL-ACME-DEMO-4") == "GL-ACME-DEMO-4"


def test_label_empty_and_whitespace():
    assert prompt_ticket_label("") == ""
    assert prompt_ticket_label("   ") == ""
    assert prompt_ticket_label(None) == ""  # type: ignore[arg-type]


def test_label_zero_and_invalid_wit():
    assert prompt_ticket_label("WIT-DEMO-0") == "WIT-DEMO-0"
    assert prompt_ticket_label("WIT-DEMO") == "WIT-DEMO"
    assert prompt_ticket_label("XWIT-DEMO-42") == "XWIT-DEMO-42"


def test_build_prompt_ticket_heading_is_number(tmp_path):
    plan = tmp_path / "WIT-BETA-42.md"
    plan.write_text("# plan\n", encoding="utf-8")
    p = PromptBuilder.build_build_prompt(
        "WIT-BETA-42",
        "Do the thing",
        "Implement login",
        plan_path=str(plan),
        work_branch="feature/WIT-BETA-42",
    )
    assert "## Ticket: 42" in p
    assert "## Ticket: WIT-BETA-42" not in p
    assert "Ticket: 42" in p
    assert "feature/WIT-BETA-42" in p
    assert str(plan) in p
    assert "implement the plan WIT-BETA-42.md" in p


def test_plan_prompt_ticket_heading_is_number():
    p = PromptBuilder.build_plan_prompt(
        "WIT-DEMO-7",
        "Plan login",
        "Write a plan",
        plan_path="C:/vd/yaver/plans/WIT-DEMO-7.md",
    )
    assert "## Ticket: 7" in p
    assert "## Ticket: WIT-DEMO-7" not in p
    assert "WIT-DEMO-7.md" in p


def test_test_prompt_ticket_heading_is_number():
    p = PromptBuilder.build_test_prompt(
        "WIT-ALPHA-3",
        "Cover login",
        "Add tests",
    )
    assert "## Ticket: 3" in p
    assert "## Ticket: WIT-ALPHA-3" not in p


def test_jira_build_and_plan_keep_full_key():
    build = PromptBuilder.build_build_prompt("KAN-12", "T", "D")
    plan = PromptBuilder.build_plan_prompt("KAN-12", "T", "D")
    assert "## Ticket: KAN-12" in build
    assert "## Ticket: KAN-12" in plan


def test_az_and_gl_keys_not_stripped_in_ticket_heading():
    az = PromptBuilder.build_build_prompt(
        "AZ-DEFAULTCOLLECTION-DEMO-APP-9", "T", "D"
    )
    gl = PromptBuilder.build_build_prompt("GL-ACME-DEMO-4", "T", "D")
    assert "## Ticket: AZ-DEFAULTCOLLECTION-DEMO-APP-9" in az
    assert "## Ticket: GL-ACME-DEMO-4" in gl


def test_gitlab_comment_prompt_uses_numeric_ticket():
    p = PromptBuilder.build_gitlab_comment_prompt(
        issue_key="WIT-DEMO-42",
        mr_title="Add login",
        mr_url="https://gitlab.example.com/g/r/-/merge_requests/1",
        source_branch="feature/WIT-DEMO-42",
        target_branch="develop",
        author="alice",
        comment="@yaver /yaver go",
        work_branch="feature/WIT-DEMO-42",
    )
    assert "## GitLab merge request: 42" in p
    assert "## GitLab merge request: WIT-DEMO-42" not in p
    assert "feature/WIT-DEMO-42" in p


def test_azure_comment_prompt_uses_numeric_ticket():
    p = PromptBuilder.build_azure_comment_prompt(
        issue_key="WIT-BETA-42",
        pr_title="Add login #42",
        pr_url="https://tfs.example.com/pr/1",
        source_branch="feature/WIT-BETA-42",
        target_branch="develop",
        author="alice",
        comment="@yaver /yaver go",
        work_branch="feature/WIT-BETA-42",
    )
    assert "## Azure DevOps pull request: 42" in p
    assert "## Azure DevOps pull request: WIT-BETA-42" not in p
    assert "feature/WIT-BETA-42" in p


def test_gitlab_comment_jira_key_unchanged():
    p = PromptBuilder.build_gitlab_comment_prompt(
        issue_key="KAN-12",
        mr_title="feat(KAN-12): x",
        mr_url="https://gitlab.example.com/g/r/-/merge_requests/1",
        source_branch="feature/KAN-12",
        target_branch="develop",
        author="alice",
        comment="go",
    )
    assert "## GitLab merge request: KAN-12" in p


def test_commit_block_uses_number_for_work_item():
    body = PromptBuilder.commit_message_block(
        "WIT-BETA-42", work_branch="feature/WIT-BETA-42"
    )
    assert "WIT-BETA-42" in body or "42" in body
    kit = PromptBuilder._load_mode_prompt(
        PromptBuilder.build_prompt_path(),
        issue_key="WIT-BETA-42",
        work_branch="feature/WIT-BETA-42",
    )
    assert "42" in kit
    assert "WIT-BETA-42" not in kit.split("feature/")[0]
    assert "feature/WIT-BETA-42" in kit


def test_plan_execute_filename_keeps_local_key():
    p = PromptBuilder.build_plan_execute_prompt(
        r"C:\vd\yaver\plans\WIT-BETA-42.md",
        issue_key="WIT-BETA-42",
    )
    assert p.startswith("implement the plan WIT-BETA-42.md")


def test_two_collections_same_id_same_prompt_number():
    a = PromptBuilder.build_build_prompt("WIT-ALPHA-42", "A", "x")
    b = PromptBuilder.build_build_prompt("WIT-BETA-42", "B", "x")
    assert "## Ticket: 42" in a
    assert "## Ticket: 42" in b
    assert "## Ticket: WIT-ALPHA-42" not in a
    assert "## Ticket: WIT-BETA-42" not in b


def test_title_and_description_still_present():
    p = PromptBuilder.build_build_prompt(
        "WIT-DEMO-42",
        "Login work",
        "Users cannot sign in",
    )
    assert "## Title" in p and "Login work" in p
    assert "## Description" in p and "Users cannot sign in" in p
    assert "## Ticket: 42" in p
