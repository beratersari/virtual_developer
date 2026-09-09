"""Dashboard agent timeout must persist and apply to the next job."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view
from src.state.models import TaskStatus


def _isolate_runtime(tmp_path, monkeypatch):
    """Keep apply_settings_update / save_runtime_settings off C:\\vd\\yaver."""
    runtime = tmp_path / "runtime_settings.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    return runtime


def test_timeout_update_persists_and_reloads(tmp_path, monkeypatch):
    from src import config as config_mod
    from src.config import (
        apply_runtime_settings_to,
        load_runtime_settings,
    )

    runtime = _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 1800)

    view = apply_settings_update(
        SettingsUpdate(agent_task_timeout_seconds=120)
    )
    assert view.agent_task_timeout_seconds == 120
    assert config_mod.settings.agent_task_timeout_seconds == 120

    assert runtime.is_file()
    data = json.loads(runtime.read_text(encoding="utf-8"))
    assert data["agent_task_timeout_seconds"] == 120
    assert load_runtime_settings()["agent_task_timeout_seconds"] == 120
    assert "agent_task_timeout_seconds" in (data.get("_updated") or {})

    # Restart without a cwd .env: runtime must still win.
    env_file = tmp_path / ".env"
    if env_file.is_file():
        env_file.unlink()
    config_mod.settings.agent_task_timeout_seconds = 15
    apply_runtime_settings_to(config_mod.settings)
    assert config_mod.settings.agent_task_timeout_seconds == 120
    assert build_settings_view().agent_task_timeout_seconds == 120


def test_runtime_settings_ignore_invalid_board_id(tmp_path, monkeypatch):
    from src import config as config_mod
    from src.config import apply_runtime_settings_to, save_runtime_settings

    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(config_mod.settings, "jira_board_id", "1")
    save_runtime_settings({"jira_board_id": "`"})
    apply_runtime_settings_to(config_mod.settings)
    assert config_mod.settings.jira_board_id == "1"

    save_runtime_settings({"jira_board_id": "99"})
    apply_runtime_settings_to(config_mod.settings)
    assert config_mod.settings.jira_board_id == "99"


def test_retry_counts_persist_and_reloads(tmp_path, monkeypatch):
    from src import config as config_mod
    from src.config import apply_runtime_settings_to, load_runtime_settings

    _isolate_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(config_mod.settings, "agent_task_max_retries", 3)
    monkeypatch.setattr(config_mod.settings, "agent_task_max_incomplete_retries", 256)

    view = apply_settings_update(
        SettingsUpdate(
            agent_task_max_retries=7,
            agent_task_max_incomplete_retries=40,
        )
    )
    assert view.agent_task_max_retries == 7
    assert view.agent_task_max_incomplete_retries == 40

    data = load_runtime_settings()
    assert data["agent_task_max_retries"] == 7
    assert data["agent_task_max_incomplete_retries"] == 40
    assert "opencode_serve_max_compact_continues" not in data

    config_mod.settings.agent_task_max_retries = 1
    config_mod.settings.agent_task_max_incomplete_retries = 2
    apply_runtime_settings_to(config_mod.settings)
    assert config_mod.settings.agent_task_max_retries == 7
    assert config_mod.settings.agent_task_max_incomplete_retries == 40
    dumped = build_settings_view()
    assert dumped.agent_task_max_retries == 7
    assert dumped.agent_task_max_incomplete_retries == 40


def test_settings_field_default_is_not_env_example_names():
    from src.config import Settings

    default = Settings.model_fields["trigger_assignee_names"].default
    assert default == ""


def test_newer_dotenv_keeps_trigger_names_over_stale_runtime(tmp_path, monkeypatch):
    """Editing .env after a dashboard save must show in Settings on restart."""
    import time

    from src.config import Settings, apply_runtime_settings_to

    monkeypatch.chdir(tmp_path)
    runtime = tmp_path / "runtime_settings.json"
    runtime.write_text(
        '{"trigger_assignee_names": "jira ai bot,jira-ai-bot,jiraai,devbot"}\n',
        encoding="utf-8",
    )
    time.sleep(0.05)
    (tmp_path / ".env").write_text(
        "TRIGGER_ASSIGNEE_NAMES=My Display Bot\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: runtime,
    )
    loaded = Settings(trigger_assignee_names="My Display Bot")
    apply_runtime_settings_to(loaded)
    assert loaded.trigger_assignee_names == "My Display Bot"


def test_newer_runtime_still_overrides_dotenv_trigger_names(tmp_path, monkeypatch):
    import time

    from src.config import Settings, apply_runtime_settings_to, save_runtime_settings

    _isolate_runtime(tmp_path, monkeypatch)
    (tmp_path / ".env").write_text(
        "TRIGGER_ASSIGNEE_NAMES=FromEnv\n",
        encoding="utf-8",
    )
    time.sleep(0.05)
    save_runtime_settings({"trigger_assignee_names": "FromDashboard"})
    loaded = Settings(trigger_assignee_names="FromEnv")
    apply_runtime_settings_to(loaded)
    assert loaded.trigger_assignee_names == "FromDashboard"


def test_legacy_runtime_without_timestamps_keeps_dotenv_trigger_names(
    tmp_path, monkeypatch
):
    """Old runtime_settings.json dumps must not hide a .env bot name."""
    from src.config import Settings, apply_runtime_settings_to

    runtime = _isolate_runtime(tmp_path, monkeypatch)
    runtime.write_text(
        '{"trigger_assignee_names": "jira ai bot,jira-ai-bot,jiraai,devbot"}\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "TRIGGER_ASSIGNEE_NAMES=Beratersari\n",
        encoding="utf-8",
    )
    loaded = Settings(trigger_assignee_names="Beratersari")
    apply_runtime_settings_to(loaded)
    assert loaded.trigger_assignee_names == "Beratersari"


def test_saving_timeout_does_not_override_dotenv_trigger_names(tmp_path, monkeypatch):
    """A later Settings save of timeout must not re-stamp leftover bot names."""
    from src.config import Settings, apply_runtime_settings_to, save_runtime_settings

    runtime = _isolate_runtime(tmp_path, monkeypatch)
    runtime.write_text(
        '{"trigger_assignee_names": "devbot", "agent_task_timeout_seconds": 1800}\n',
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "TRIGGER_ASSIGNEE_NAMES=My Display Bot\n",
        encoding="utf-8",
    )
    save_runtime_settings({"agent_task_timeout_seconds": 120})
    loaded = Settings(
        trigger_assignee_names="My Display Bot",
        agent_task_timeout_seconds=1800,
    )
    apply_runtime_settings_to(loaded)
    assert loaded.trigger_assignee_names == "My Display Bot"
    assert loaded.agent_task_timeout_seconds == 120


def test_settings_import_ignores_legacy_runtime_intake_mode(tmp_path):
    """Leftover jira_intake_mode in runtime JSON must not become a Settings field."""
    import os
    import subprocess
    import sys

    agent = tmp_path / "yaver-data"
    agent.mkdir()
    (agent / "runtime_settings.json").write_text(
        json.dumps({"jira_intake_mode": "webhook", "jira_board_id": "1"}),
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    env["YAVER_DATA_DIR"] = str(agent)
    env.pop("JIRA_INTAKE_MODE", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.config import settings; "
            "print(hasattr(settings, 'jira_intake_mode'), settings.jira_board_id)",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "False 1"


def test_begin_workflow_uses_live_timeout(tmp_path, monkeypatch, state_manager):
    """_begin_workflow_run freezes current settings.agent_task_timeout_seconds."""
    from src.config import settings
    from src.processor import JobProcessor
    from src.orchestrator.agent_runner import AgentTask
    from tests.conftest import FakeJiraClient
    from unittest.mock import patch, MagicMock

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "agent_task_timeout_seconds", 90)
    monkeypatch.setattr(settings, "agent_task_max_retries", 1)

    fake = FakeJiraClient()
    with patch("src.processor.create_jira_client", return_value=fake):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = MagicMock()
    proc.jira_client = fake
    proc.job_store = MagicMock()
    proc.job_store.create_job.return_value = {"job_id": "job_test"}

    state = state_manager.create_state("TO-1", "s", "d")
    task = AgentTask(description="d", prompt="p", agent="a", issue_key="TO-1")
    jid = proc._begin_workflow_run(
        state,
        status=TaskStatus.EXECUTING,
        task=task,
        workflow_type="execution",
        agent="a",
        job_status="executing",
    )
    assert jid == "job_test"
    loaded = state_manager.get_state("TO-1")
    assert loaded is not None
    assert loaded.timeout_seconds == 90
    assert loaded.max_retries == 1

    # Dashboard changes timeout; next begin uses new value
    monkeypatch.setattr(settings, "agent_task_timeout_seconds", 600)
    state2 = state_manager.get_state("TO-1")
    # reset to pending-like for CAS (begin rejects terminal only)
    state_manager.update_state("TO-1", status=TaskStatus.PENDING, force=True)
    task2 = AgentTask(description="d", prompt="p", agent="a", issue_key="TO-1")
    proc._begin_workflow_run(
        state2,
        status=TaskStatus.EXECUTING,
        task=task2,
        workflow_type="execution",
        agent="a",
        job_status="executing",
    )
    loaded2 = state_manager.get_state("TO-1")
    assert loaded2.timeout_seconds == 600
