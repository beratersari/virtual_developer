"""PromptBuilder: only PLAN_PROMPT.md + BUILD_PROMPT.md + Jira fields."""

from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.prompt_kit import substitute_issue_key, substitute_placeholders


def test_substitute_issue_key():
    assert substitute_issue_key("[{ISSUE_KEY}] fix: x", "KAN-9") == "[KAN-9] fix: x"


def test_substitute_plan_path():
    out = substitute_placeholders(
        "Write to {PLAN_PATH}",
        issue_key="K-1",
        plan_path=".sisyphus/plans/K-1.md",
    )
    assert out == "Write to .sisyphus/plans/K-1.md"


def test_plan_path_includes_title_and_description():
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_plan_prompt("A-1", "sum title", "full description body")
    assert "A-1" in p
    assert "Jira title" in p and "sum title" in p
    assert "Jira description" in p and "full description body" in p
    assert "Plan mode" in p or "planning" in p.lower()
    assert ".sisyphus/plans/A-1.md" in p


def test_plan_with_acceptance():
    p = PromptBuilder.build_plan_prompt(
        "A-1", "s", "d", acceptance_criteria="must pass"
    )
    assert "Acceptance criteria" in p
    assert "must pass" in p


def test_build_path_includes_title_description_and_plan():
    PromptBuilder.clear_prompt_file_cache()
    p = PromptBuilder.build_build_prompt(
        "A-1",
        "Build the feature",
        "Do the work carefully",
        plan_path="/plans/A-1.md",
        work_branch="feature/A-1",
    )
    assert "Build mode" in p or "delivery" in p.lower()
    assert "Jira title" in p and "Build the feature" in p
    assert "Jira description" in p and "Do the work carefully" in p
    assert "/plans/A-1.md" in p
    assert "feature/A-1" in p
    assert "[A-1]" in p
    assert "Git policy" in p


def test_plan_path_requires_commit_todo_in_plan_file_instructions():
    p = PromptBuilder.build_plan_prompt("KAN-7", "title", "desc")
    assert "Commit with the conventional format" in p
    assert "[KAN-7]" in p


def test_build_without_plan_still_has_description():
    p = PromptBuilder.build_build_prompt(
        "BUG-9",
        "Fix login",
        "Users cannot login after password reset",
    )
    assert "Fix login" in p
    assert "Users cannot login after password reset" in p
    assert "Jira description" in p


def test_params_block_excluded_from_both_paths():
    desc = (
        "Implement 10 + 1021\n\n"
        "{params}\n"
        "Repository: https://gitlab.com/user/repo.git\n"
        "Source branch: feature/KAN-8\n"
        "Target branch: feature/KAN-4\n"
        "{params}\n"
    )
    for p in (
        PromptBuilder.build_plan_prompt("KAN-8", "Task", desc),
        PromptBuilder.build_build_prompt("KAN-8", "Task", desc),
    ):
        assert "{params}" not in p
        assert "Repository:" not in p
        assert "Source branch:" not in p
        assert "gitlab.com" not in p
        assert "10 + 1021" in p


def test_agent_task_always_strips_params():
    from src.orchestrator.agent_runner import AgentTask

    raw = (
        "Do the work\n"
        "{params}\nRepository: https://evil.example/r.git\n"
        "Source branch: a\nTarget branch: b\n{params}\n"
    )
    task = AgentTask(description="d", prompt=raw, agent="sisyphus", issue_key="K-1")
    assert "{params}" not in task.prompt
    assert "evil.example" not in task.prompt
    assert "Do the work" in task.prompt


def test_commit_message_block_filled_issue_key():
    PromptBuilder.clear_prompt_file_cache()
    block = PromptBuilder.commit_message_block("KAN-42")
    assert "KAN-42" in block
    assert "[KAN-42]" in block
    assert "{ISSUE_KEY}" not in block


def test_only_two_primary_paths():
    plan = PromptBuilder.build_plan_prompt("K-1", "s", "d")
    build = PromptBuilder.build_build_prompt("K-1", "s", "d")
    assert "Jira title" in plan and "Jira title" in build
    assert plan != build


def test_compat_aliases_map_to_two_paths():
    plan = PromptBuilder.build_prometheus_prompt("A-1", "title", "body text")
    assert "body text" in plan and "title" in plan

    build = PromptBuilder.build_atlas_prompt(
        "A-1",
        "/p.md",
        summary="sum",
        description="desc body",
        work_branch="feature/x",
    )
    assert "sum" in build and "desc body" in build
    assert "/p.md" in build

    direct = PromptBuilder.build_sisyphus_prompt(
        "A-1", "do thing", summary="Fix bug"
    )
    assert "do thing" in direct and "Fix bug" in direct

    oracle = PromptBuilder.build_oracle_consult_prompt(
        "why design X?", issue_key="A-1", summary="Arch"
    )
    assert "why design X?" in oracle and "Arch" in oracle


def test_agent_name_does_not_change_prompt_shape():
    a = PromptBuilder.build_atlas_prompt(
        "X-1", summary="s", description="d", work_branch="feature/x"
    )
    b = PromptBuilder.build_sisyphus_prompt(
        "X-1", "d", summary="s", work_branch="feature/x"
    )
    assert a == b


def test_no_comment_response_builder():
    assert not hasattr(PromptBuilder, "build_comment_response_prompt")


def test_only_two_prompt_files_exist():
    from pathlib import Path

    agent = Path("agent")
    names = {p.name for p in agent.iterdir() if p.is_file()}
    assert names == {"PLAN_PROMPT.md", "BUILD_PROMPT.md"}
