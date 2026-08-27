"""Linux durable storage paths + session-file delete via the storage API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import (
    build_storage_view,
    force_delete_temp_folder,
    reset_delete_jobs,
    reset_size_cache,
)
from src.paths import agent_subdir, ensure_agent_data_dir


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


@pytest.fixture(autouse=True)
def _reset_storage():
    reset_delete_jobs()
    reset_size_cache()
    yield
    reset_delete_jobs()
    reset_size_cache()


def test_storage_lists_and_deletes_session_files(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver-data"))
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / ".temp")
    (tmp_path / ".temp").mkdir()
    ensure_agent_data_dir()
    sessions = agent_subdir("sessions")
    sessions.mkdir(parents=True, exist_ok=True)
    log = sessions / "KAN-1_20260827_120000.log"
    log.write_text("session\n", encoding="utf-8")
    view = build_storage_view()
    names = [s["name"] for s in view.get("sessions") or []]
    assert "KAN-1_20260827_120000.log" in names
    assert view.get("data_dir")
    assert view.get("sessions_dir")
    out = force_delete_temp_folder("KAN-1_20260827_120000.log", area="sessions")
    assert out["ok"] is True
    assert not log.exists()


def test_storage_api_deletes_session_file(tmp_path, monkeypatch):
    from src.config import settings

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver-data"))
    monkeypatch.setattr(settings, "temp_dir_base", tmp_path / ".temp")
    (tmp_path / ".temp").mkdir()
    ensure_agent_data_dir()
    sessions = agent_subdir("sessions")
    sessions.mkdir(parents=True, exist_ok=True)
    name = "KAN-2_20260827_130000.log"
    (sessions / name).write_text("x\n", encoding="utf-8")
    app = create_dashboard_app()
    client = TestClient(app)
    listed = client.get("/api/storage")
    assert listed.status_code == 200
    body = listed.json()
    assert any(s["name"] == name for s in body.get("sessions") or [])
    gone = client.post("/api/storage/delete", json={"name": name, "area": "sessions"})
    assert gone.status_code == 202
    assert gone.json().get("ok") is True
    # File is small; wait briefly for the worker.
    import time

    deadline = time.time() + 3
    while (sessions / name).exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not (sessions / name).exists()
