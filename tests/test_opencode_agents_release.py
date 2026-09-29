"""The Windows/Linux release exe keeps agents next to the binary."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app


def test_release_exe_lists_agents_beside_the_binary(monkeypatch, tmp_path: Path):
    exe_dir = tmp_path / "yaver"
    catalog = exe_dir / "opencoderman" / "agents"
    catalog.mkdir(parents=True)
    (catalog / "derman-build.md").write_text("# build\n", encoding="utf-8")
    (catalog / "derman-plan.md").write_text("# plan\n", encoding="utf-8")

    # PyInstaller stores imported modules under _internal. The release zip
    # does not put opencoderman there; a decoy must not become the catalog.
    meipass = exe_dir / "_internal"
    decoy = meipass / "opencoderman" / "agents"
    decoy.mkdir(parents=True)
    (decoy / "decoy-agent.md").write_text("# no\n", encoding="utf-8")
    frozen_module = meipass / "src" / "opencode_agents.py"
    frozen_module.parent.mkdir(parents=True)
    frozen_module.write_text("", encoding="utf-8")

    import src.install_paths as paths
    import src.opencode_agents as agents_mod

    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(exe_dir / "yaver.exe"))
    monkeypatch.setattr(agents_mod, "__file__", str(frozen_module))
    monkeypatch.setattr(agents_mod, "opencode_agents_dir", lambda: tmp_path / "opencode")
    monkeypatch.setattr(agents_mod, "opencode_xdg_agents_dir", lambda: tmp_path / "xdg")
    monkeypatch.setattr(agents_mod, "claude_agents_dir", lambda: tmp_path / "claude")

    client = TestClient(create_dashboard_app())
    response = client.get("/api/opencode-agents")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["agents"] == ["derman-build", "derman-plan"]
    assert "decoy-agent" not in body["agents"]
