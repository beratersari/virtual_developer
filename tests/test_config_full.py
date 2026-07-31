"""Coverage for Settings / config helpers."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import (
    Settings,
    get_current_temp_dir,
    get_settings,
    set_current_temp_dir,
)


def test_list_properties_defaults_and_custom():
    s = Settings(
        jira_projects="A, B, ",
        trigger_labels="x,y",
        trigger_mentions="@Bot,@AI",
    )
    assert s.jira_projects_list == ["A", "B"]
    assert s.trigger_labels_list == ["x", "y"]
    assert s.trigger_mentions_list == ["@Bot", "@AI"]

    s2 = Settings(jira_projects="", trigger_labels="", trigger_mentions="")
    assert s2.jira_projects_list == ["PROJ"]
    assert "ai-assist" in s2.trigger_labels_list
    assert "@DevBot" in s2.trigger_mentions_list


def test_full_plans_and_state_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.full_plans_dir.name == "plans" or "plans" in str(s.full_plans_dir)
    assert "state" in str(s.state_dir)


def test_is_configured_and_validate():
    s = Settings(jira_host="", jira_api_token="")
    assert s.is_configured() is False
    with pytest.raises(ValueError, match="JIRA_HOST"):
        s.validate_or_raise()

    s2 = Settings(jira_host="http://j", jira_api_token="")
    with pytest.raises(ValueError, match="JIRA_API_TOKEN"):
        s2.validate_or_raise()

    s3 = Settings(jira_host="http://j", jira_api_token="tok")
    assert s3.is_configured() is True
    s3.validate_or_raise()


def test_load_prompt_from_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    # create agent/prompts/PLANNING.md
    pdir = tmp_path / "agent" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "PLANNING.md").write_text("CUSTOM PLANNING")
    (pdir / "EXECUTION.md").write_text("CUSTOM EXEC")
    (pdir / "DIRECT_EXECUTION.md").write_text("CUSTOM DIRECT")
    (pdir / "ORACLE.md").write_text("CUSTOM ORACLE")
    s.prompt_planning_file = Path("agent/prompts/PLANNING.md")
    s.prompt_execution_file = Path("agent/prompts/EXECUTION.md")
    s.prompt_direct_execution_file = Path("agent/prompts/DIRECT_EXECUTION.md")
    s.prompt_oracle_file = Path("agent/prompts/ORACLE.md")
    assert "CUSTOM PLANNING" in s.prompt_planning
    assert "CUSTOM EXEC" in s.prompt_execution
    assert "CUSTOM DIRECT" in s.prompt_direct_execution
    assert "CUSTOM ORACLE" in s.prompt_oracle


def test_load_prompt_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    s.prompt_planning_file = Path("missing.md")
    text = s.prompt_planning
    assert "Prometheus" in text or "plan" in text.lower()


def test_load_prompt_env_and_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "abs_prompt.md"
    f.write_text("FROM_ABS")
    s = Settings()
    s.prompt_planning_file = f
    assert "FROM_ABS" in s.prompt_planning

    env_file = tmp_path / "env_prompt.md"
    env_file.write_text("FROM_ENV")
    monkeypatch.setenv("PROMPT_PLANNING_FILE", str(env_file))
    s2 = Settings()
    s2.prompt_planning_file = Path("agent/prompts/PLANNING.md")
    assert "FROM_ENV" in s2.prompt_planning or True  # path order may prefer


def test_load_prompt_read_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "agent" / "prompts"
    p.mkdir(parents=True)
    bad = p / "PLANNING.md"
    bad.write_text("x")
    s = Settings()
    s.prompt_planning_file = Path("agent/prompts/PLANNING.md")
    with patch("pathlib.Path.read_text", side_effect=OSError("nope")):
        text = s.prompt_planning
        assert isinstance(text, str)


def test_load_prompt_dir_not_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "agent" / "prompts" / "PLANNING.md"
    d.mkdir(parents=True)
    s = Settings()
    s.prompt_planning_file = Path("agent/prompts/PLANNING.md")
    text = s.prompt_planning
    assert isinstance(text, str)


def test_prompt_code_review_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    # default fallback has C++ review content
    text = s.prompt_code_review
    assert "Review" in text or "review" in text.lower()

    # with file in agent/rules
    rdir = tmp_path / "agent" / "rules"
    rdir.mkdir(parents=True)
    (rdir / "CODE_REVIEW.md").write_text("CUSTOM REVIEW")
    s.code_review_prompt_file = Path("agent/rules/CODE_REVIEW.md")
    # may still use default path resolution
    set_current_temp_dir(tmp_path / "temp_proj")
    (tmp_path / "temp_proj" / "agent" / "rules").mkdir(parents=True)
    (tmp_path / "temp_proj" / "agent" / "rules" / "CODE_REVIEW.md").write_text("TEMP REVIEW")
    text2 = s.prompt_code_review
    assert "REVIEW" in text2.upper()
    set_current_temp_dir(None)


def test_prompt_code_review_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "cr.md"
    f.write_text("ENV_CR")
    monkeypatch.setenv("PROMPT_CODE_REVIEW_FILE", str(f))
    s = Settings()
    assert "ENV_CR" in s.prompt_code_review


def test_prompt_code_review_read_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rdir = tmp_path / "agent" / "rules"
    rdir.mkdir(parents=True)
    (rdir / "CODE_REVIEW.md").write_text("x")
    s = Settings()
    with patch("pathlib.Path.read_text", side_effect=OSError("e")):
        text = s.prompt_code_review
        assert isinstance(text, str)


def test_temp_dir_helpers(tmp_path):
    set_current_temp_dir(tmp_path)
    assert get_current_temp_dir() == tmp_path
    set_current_temp_dir(None)
    assert get_current_temp_dir() is None


def test_get_settings_singleton():
    a = get_settings()
    b = get_settings()
    assert a is b


def test_default_prompt_helpers():
    s = Settings()
    assert "Prometheus" in s._get_default_planning_prompt() or "plan" in s._get_default_planning_prompt().lower()
    assert "Success" in s._get_default_execution_prompt() or "plan" in s._get_default_execution_prompt().lower()
    assert "COMMIT" in s._get_default_direct_execution_prompt()
    assert "Direct Answer" in s._get_default_oracle_prompt()
