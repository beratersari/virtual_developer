"""Dashboard agent timeout must persist and apply to the next job."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view
from src.state.models import TaskStatus


def test_timeout_update_persists_and_reloads(tmp_path, monkeypatch):
    from src import config as config_mod
    from src.config import (
        apply_runtime_settings_to,
        load_runtime_settings,
        runtime_settings_path,
        save_runtime_settings,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "agent_task_timeout_seconds", 1800)

    view = apply_settings_update(
        SettingsUpdate(agent_task_timeout_seconds=120)
    )
    assert view.agent_task_timeout_seconds == 120
    assert config_mod.settings.agent_task_timeout_seconds == 120

    path = runtime_settings_path()
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent_task_timeout_seconds"] == 120
    assert load_runtime_settings()["agent_task_timeout_seconds"] == 120

    # Simulate restart: Settings from env, then apply runtime file
    config_mod.settings.agent_task_timeout_seconds = 15  # pretend .env was 15
    apply_runtime_settings_to(config_mod.settings)
    assert config_mod.settings.agent_task_timeout_seconds == 120
    assert build_settings_view().agent_task_timeout_seconds == 120


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
