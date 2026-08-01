"""PromptBuilder + unified prompt kit assembly for each workflow path."""

from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.prompt_kit import (
    clear_prompt_kit_cache,
    parse_prompt_kit,
    substitute_issue_key,
)


def test_parse_prompt_kit_sections():
    text = """# Title

## §policy.commit
Commit rules here.

## §role.direct
Do the work.
"""
    sections = parse_prompt_kit(text)
    assert sections["policy.commit"] == "Commit rules here."
    assert sections["role.direct"] == "Do the work."


def test_substitute_issue_key():
    assert substitute_issue_key("[{ISSUE_KEY}] fix: x", "KAN-9") == "[KAN-9] fix: x"


def test_prometheus_planning_path():
    """Planning: §role.planning + Jira, no git policy."""
    p = PromptBuilder.build_prometheus_prompt("A-1", "sum", "desc")
    assert "A-1" in p and "sum" in p and "desc" in p
    assert "## Role" in p
    assert "Jira issue" in p
    assert p.count("## Git policy") == 0
    assert "Acceptance criteria" not in p


def test_prometheus_with_acceptance():
    p = PromptBuilder.build_prometheus_prompt(
        "A-1", "s", "d", acceptance_criteria="must pass"
    )
    assert "Acceptance criteria" in p
    assert "must pass" in p


def test_atlas_execution_path():
    """Execution: §role.execution + git policy + plan path."""
    p = PromptBuilder.build_atlas_prompt("A-1", "/plans/A-1.md")
    assert "A-1" in p and "/plans/A-1.md" in p
    assert "Previous learnings" not in p
    assert "[A-1]" in p
    assert "## Git policy" in p
    assert "## Role" in p
    assert p.count("## Git policy") == 1
    assert "feature/A-1" in p


def test_atlas_with_learnings():
    p = PromptBuilder.build_atlas_prompt("A-1", "p.md", previous_learnings=["l1", "l2"])
    assert "Previous learnings" in p
    assert "l1" in p and "l2" in p
    assert "[A-1]" in p


def test_sisyphus_direct_path():
    """Direct: §role.direct + git policy + summary/description."""
    p = PromptBuilder.build_sisyphus_prompt(
        "A-1", "do thing", summary="Fix the bug"
    )
    assert "do thing" in p
    assert "Fix the bug" in p
    assert "### Context" not in p
    assert "[A-1]" in p
    assert "feature/A-1" in p
    assert "## Git policy" in p
    assert p.count("## Git policy") == 1
    assert "Yeni özellik eklendi" not in p
    # No separate comment-response template language
    assert "Comment response request" not in p
    assert "User comment" not in p


def test_sisyphus_with_files_and_patterns():
    p = PromptBuilder.build_sisyphus_prompt(
        "A-1",
        "do",
        context={"files": ["a.py", "b.py"], "patterns": ["singleton"]},
    )
    assert "a.py" in p and "b.py" in p and "singleton" in p
    assert "[A-1]" in p
    assert "Relevant files" in p


def test_commit_message_block_filled_issue_key():
    clear_prompt_kit_cache()
    block = PromptBuilder.commit_message_block("KAN-42")
    assert "KAN-42" in block
    assert "[KAN-42]" in block
    assert "feature/KAN-42" in block
    assert "fix:" in block
    assert "{ISSUE_KEY}" not in block


def test_sisyphus_context_empty_keys():
    p_empty = PromptBuilder.build_sisyphus_prompt("A-1", "do", context={})
    assert "Context" not in p_empty
    p = PromptBuilder.build_sisyphus_prompt("A-1", "do", context={"other": 1})
    assert "Context" in p


def test_no_comment_response_builder():
    assert not hasattr(PromptBuilder, "build_comment_response_prompt")


def test_oracle_path():
    """Oracle: §role.oracle + question, no git policy."""
    p1 = PromptBuilder.build_oracle_consult_prompt("why?")
    assert "Context files" not in p1
    assert "## Role" in p1
    assert p1.count("## Git policy") == 0

    p2 = PromptBuilder.build_oracle_consult_prompt(
        "why design X?",
        context_files=["a.py"],
        issue_key="A-1",
        summary="Arch Q",
    )
    assert "a.py" in p2
    assert "A-1" in p2
    assert "Arch Q" in p2
    assert "why design X?" in p2
    assert p2.count("## Git policy") == 0


def test_each_path_uses_distinct_role_heading():
    plan = PromptBuilder.build_prometheus_prompt("K-1", "s", "d")
    direct = PromptBuilder.build_sisyphus_prompt("K-1", "d", summary="s")
    exec_ = PromptBuilder.build_atlas_prompt("K-1", "/p.md")
    oracle = PromptBuilder.build_oracle_consult_prompt("q", issue_key="K-1")
    assert "Task planning request" in plan
    assert "Direct task execution" in direct
    assert "Task execution request" in exec_
    assert "Architecture consultation" in oracle
