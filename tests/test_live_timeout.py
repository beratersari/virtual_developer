"""Live dashboard timeout must reach OpenCode even if the job froze 1800."""

from __future__ import annotations

import pytest

from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update
from src.state.models import JiraAgentState, TaskStatus


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


def test_live_timeout_ignores_frozen_1800(monkeypatch):
    from src import config as config_mod
    from src.config import live_agent_timeout_seconds
    from src.processor import _live_agent_timeout_seconds

    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 7200)
    frozen = JiraAgentState(
        issue_key="TO-2",
        issue_summary="s",
        status=TaskStatus.EXECUTING,
        timeout_seconds=1800,
    )
    assert live_agent_timeout_seconds() == 7200
    assert _live_agent_timeout_seconds(frozen) == 7200
    assert frozen.timeout_seconds == 7200


def test_stored_timeout_zero_uses_the_default_budget(monkeypatch):
    from src import config as config_mod
    from src.config import compute_stuck_limit_seconds, live_agent_timeout_seconds

    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 0)
    assert live_agent_timeout_seconds() == 1800
    assert compute_stuck_limit_seconds(live_agent_timeout_seconds(), 3) == 1800 * 4 * 1.5


def test_stored_timeout_under_thirty_seconds_stays(monkeypatch):
    from src import config as config_mod
    from src.config import live_agent_timeout_seconds

    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 15)
    assert live_agent_timeout_seconds() == 15


def test_timeout_save_writes_dotenv(tmp_path, monkeypatch):
    from src import config as config_mod

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_TASK_TIMEOUT_SECONDS=1800\n", encoding="utf-8")
    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 1800)
    apply_settings_update(SettingsUpdate(agent_task_timeout_seconds=7200))
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AGENT_TASK_TIMEOUT_SECONDS=7200" in text
    assert config_mod.settings.agent_task_timeout_seconds == 7200
    assert config_mod.live_agent_timeout_seconds() == 7200
