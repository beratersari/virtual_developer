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


def test_prompt_roles_from_kit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "AGENT_PROMPT.md").write_text(
        "## §role.direct\nFROM_KIT_DIRECT\n\n"
        "## §role.planning\nFROM_KIT_PLAN\n\n"
        "## §role.execution\nFROM_KIT_EXEC\n\n"
        "## §role.oracle\nFROM_KIT_ORACLE\n\n"
        "## §policy.commit\n[{ISSUE_KEY}] fix: x\n",
        encoding="utf-8",
    )
    s = Settings()
    s.prompt_kit_file = Path("agent/AGENT_PROMPT.md")
    assert "FROM_KIT_DIRECT" in s.prompt_direct_execution
    assert "FROM_KIT_PLAN" in s.prompt_planning
    assert "FROM_KIT_EXEC" in s.prompt_execution
    assert "FROM_KIT_ORACLE" in s.prompt_oracle
    assert "ABC-1" in s.prompt_commit_policy("ABC-1")
    assert "{ISSUE_KEY}" not in s.prompt_commit_policy("ABC-1")


def test_prompt_defaults_without_kit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clear_prompt_kit_cache()
    s = Settings()
    s.prompt_kit_file = Path("agent/AGENT_PROMPT.md")
    assert s.prompt_planning
    assert s.prompt_direct_execution
    assert s.prompt_oracle


def test_temp_dir_helpers(tmp_path):
    set_current_temp_dir(tmp_path)
    assert get_current_temp_dir() == tmp_path
    set_current_temp_dir(None)
    assert get_current_temp_dir() is None


def test_get_settings_singleton():
    a = get_settings()
    b = get_settings()
    assert a is b
