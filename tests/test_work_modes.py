"""Custom modes link to an OpenCode agent without changing plan/build/test delivery."""

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.issue_git_spec import parse_issue_mode
from src.opencode_agents import AgentFileError, read_agent, write_agent
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.work_modes import apply_saved_modes, lookup


def _params(mode: str) -> str:
    return (
        "{params}\n"
        f"Mode: {mode}\n"
        "Repository: https://gitlab.example.com/acme/app.git\n"
        "Source: feature/x\n"
        "Target: develop\n"
        "{params}"
    )


def test_custom_mode_uses_build_delivery_and_its_agent(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [
            {"name": "plan", "behavior": "plan", "agent": "derman-plan"},
            {"name": "build", "behavior": "build", "agent": "derman-build"},
            {"name": "test", "behavior": "test", "agent": "derman-test"},
            {"name": "docs", "behavior": "build", "agent": "derman-docs"},
        ]
    )
    text = _params("docs")
    assert parse_issue_mode("", text) == "docs"
    assert WorkflowRouter.route_issue("KAN-1", "", text) == WorkflowType.EXECUTION
    assert (
        WorkflowRouter.agent_for_issue("", text, WorkflowType.EXECUTION)
        == "derman-docs"
    )
    assert lookup("plan")["agent"] == "derman-plan"


def test_builtin_mode_behavior_can_be_changed(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "work_modes", "")
    apply_saved_modes(
        [
            {"name": "plan", "behavior": "build", "agent": "derman-plan"},
            {"name": "build", "behavior": "build", "agent": "derman-build"},
            {"name": "test", "behavior": "test", "agent": "derman-test"},
        ]
    )
    text = _params("plan")
    assert lookup("plan")["behavior"] == "build"
    assert WorkflowRouter.route_issue("KAN-1", "", text) == WorkflowType.EXECUTION
    assert (
        WorkflowRouter.agent_for_issue("", text, WorkflowType.EXECUTION)
        == "derman-plan"
    )


def test_plan_execute_keeps_the_build_agent_when_mode_is_plan(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "default_agent", "derman-build")
    monkeypatch.setattr(settings, "default_plan_agent", "my-planner")
    monkeypatch.setattr(settings, "work_modes", "")
    text = _params("plan")
    assert (
        WorkflowRouter.agent_for_issue("", text, WorkflowType.PLANNING) == "my-planner"
    )
    assert (
        WorkflowRouter.agent_for_issue("", text, WorkflowType.EXECUTION)
        == "derman-build"
    )


def test_unknown_mode_stays_unset():
    assert parse_issue_mode("", _params("not-a-mode")) is None


def test_agent_file_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: tmp_path)
    path = write_agent("derman-docs", "---\nmode: primary\n---\n\nDo the docs.\n", create=True)
    assert path == tmp_path / "derman-docs.md"
    assert "Do the docs." in read_agent("derman-docs")
    with pytest.raises(AgentFileError):
        write_agent("../secret", "nope")
    with pytest.raises(AgentFileError):
        write_agent("derman-docs", "again\n", create=True)


def test_agent_http_create_and_edit(tmp_path, monkeypatch):
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: tmp_path)
    app = create_dashboard_app()
    client = TestClient(app)
    created = client.post("/api/opencode-agents", json={"name": "derman-docs", "text": ""})
    assert created.status_code == 200, created.text
    assert "unattended" in created.json()["text"].lower()
    saved = client.put(
        "/api/opencode-agents/derman-docs",
        json={"text": "---\nmode: primary\n---\n\nWrite the guide.\n"},
    )
    assert saved.status_code == 200
    body = client.get("/api/opencode-agents/derman-docs")
    assert "Write the guide." in body.json()["text"]
    assert (tmp_path / "derman-docs.md").is_file()
    assert client.post("/api/opencode-agents", json={"name": "../x", "text": "no"}).status_code == 400
