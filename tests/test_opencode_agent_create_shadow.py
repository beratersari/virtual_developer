"""The editor only sees opencoderman/agents. Sync copies those files out."""

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.opencode_agents import read_agent, sync_agents, sync_status


def test_create_refuses_a_name_already_in_the_catalog(tmp_path, monkeypatch):
    catalog = tmp_path / "opencoderman" / "agents"
    catalog.mkdir(parents=True)
    (catalog / "derman-build.md").write_text(
        "---\nmode: primary\n---\n\nREAL BUILD AGENT\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: catalog)
    client = TestClient(create_dashboard_app())
    created = client.post("/api/opencode-agents", json={"name": "derman-build", "text": ""})
    assert created.status_code == 400, created.text
    assert "REAL BUILD AGENT" in read_agent("derman-build")
    cased = client.post("/api/opencode-agents", json={"name": "Derman-Build", "text": ""})
    assert cased.status_code == 400, cased.text
    listed = client.get("/api/opencode-agents")
    assert listed.json()["agents"] == ["derman-build"]


def test_sync_copies_catalog_into_opencode_and_claude(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "derman-docs.md").write_text(
        "---\nmode: primary\n---\n\nWrite the guide.\n",
        encoding="utf-8",
    )
    opencode = tmp_path / "opencode"
    claude = tmp_path / "claude"
    monkeypatch.setattr("src.opencode_agents.agents_dir", lambda: catalog)
    monkeypatch.setattr("src.opencode_agents.opencode_agents_dir", lambda: opencode)
    monkeypatch.setattr("src.opencode_agents.claude_agents_dir", lambda: claude)
    result = sync_agents()
    assert result["agents"] == ["derman-docs"]
    assert "Write the guide." in (opencode / "derman-docs.md").read_text(encoding="utf-8")
    claude_text = (claude / "derman-docs.md").read_text(encoding="utf-8")
    assert "name: derman-docs" in claude_text
    assert "Write the guide." in claude_text
    assert sync_status()["synced"] is True
    (opencode / "leftover.md").write_text("old home agent\n", encoding="utf-8")
    assert sync_status()["synced"] is True
    (catalog / "derman-docs.md").write_text(
        "---\nmode: primary\n---\n\nChanged.\n",
        encoding="utf-8",
    )
    status = sync_status()
    assert status["synced"] is False
    assert status["pending"] == ["derman-docs"]
