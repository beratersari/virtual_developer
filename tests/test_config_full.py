"""Settings / prompt kit loading coverage."""

from pathlib import Path

from src.config import Settings, get_current_temp_dir, get_settings, set_current_temp_dir
from src.orchestrator.prompt_kit import clear_prompt_kit_cache


def test_is_configured_and_validate():
    s = Settings(jira_host="", jira_api_token="")
    assert s.is_configured() is False
    s2 = Settings(jira_host="https://j.example", jira_api_token="")
    assert s2.is_configured() is False
    s3 = Settings(jira_host="https://j.example", jira_api_token="tok")
    assert s3.is_configured() is True
    s3.validate_or_raise()


def test_prompt_roles_from_mode_files(tmp_path, monkeypatch):
    """Settings expose plan/build mode files only."""
    from src.orchestrator.prompt_builder import PromptBuilder

    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    PromptBuilder.clear_prompt_file_cache()
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "PLAN_PROMPT.md").write_text(
        "# Plan mode\nFROM_PLAN_FILE\n[{ISSUE_KEY}]\n",
        encoding="utf-8",
    )
    (agent / "BUILD_PROMPT.md").write_text(
        "# Build mode\nFROM_BUILD_FILE\n## Git policy\n[{ISSUE_KEY}] fix: x\n",
        encoding="utf-8",
    )
    s = Settings()
    s.agent_prompts_dir = Path("agent")
    assert "FROM_PLAN_FILE" in s.prompt_planning
    assert "FROM_BUILD_FILE" in s.prompt_execution
    assert "FROM_BUILD_FILE" in s.prompt_execution
    assert "FROM_PLAN_FILE" in s.prompt_planning
    assert "ABC-1" in s.prompt_commit_policy("ABC-1")
    assert "{ISSUE_KEY}" not in s.prompt_commit_policy("ABC-1")


def test_prompt_defaults_without_mode_files(tmp_path, monkeypatch):
    from src.orchestrator.prompt_builder import PromptBuilder

    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    PromptBuilder.clear_prompt_file_cache()
    s = Settings()
    s.agent_prompts_dir = Path("agent")
    # Missing files → non-empty stub still returned
    assert s.prompt_planning
    assert s.prompt_execution
    assert s.prompt_planning


def test_temp_dir_helpers(tmp_path):
    set_current_temp_dir(tmp_path)
    assert get_current_temp_dir() == tmp_path
    set_current_temp_dir(None)
    assert get_current_temp_dir() is None


def test_get_settings_singleton():
    a = get_settings()
    b = get_settings()
    assert a is b
