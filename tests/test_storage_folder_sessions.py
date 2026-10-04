"""Storage details lists OpenCode chats that use one clone folder."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import (
    TempStorageError,
    load_storage_folder,
    working_directory_uses_folder,
)


def test_working_directory_matches_the_folder_and_not_a_prefix_sibling(tmp_path: Path):
    folder = tmp_path / "KAN-1"
    folder.mkdir()
    nested = folder / "repo"
    nested.mkdir()
    sibling = tmp_path / "KAN-10"
    assert working_directory_uses_folder(str(folder), folder)
    assert working_directory_uses_folder(str(folder) + "/", folder)
    assert working_directory_uses_folder(str(nested), folder)
    assert not working_directory_uses_folder(str(sibling), folder)
    assert not working_directory_uses_folder("", folder)
    assert not working_directory_uses_folder(str(tmp_path), folder)


def test_storage_folder_sessions_are_opencode_chats_for_that_clone(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    from src.config import settings

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    base = tmp_path / "clones"
    folder = base / "repo a"
    other = base / "KAN-10"
    folder.mkdir(parents=True)
    (folder / "README").write_text("x", encoding="utf-8")
    nested = folder / "checkout"
    nested.mkdir()
    other.mkdir()
    monkeypatch.setattr(settings, "temp_dir_base", base)

    inside = binds.upsert(
        repository_url="https://gitlab.example/group/x.git",
        branch="feature/KAN-1",
        target_branch="main",
        session_id="ses_plan",
        issue_key="KAN-1",
        kind="plan",
        backend="opencode",
        working_directory=str(folder.resolve()),
    )
    child = binds.upsert(
        repository_url="https://gitlab.example/group/y.git",
        branch="feature/KAN-1",
        target_branch="main",
        session_id="ses_build",
        issue_key="KAN-1",
        kind="build",
        backend="opencode",
        working_directory=str(nested.resolve()),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/x.git",
        branch="feature/KAN-10",
        target_branch="main",
        session_id="ses_other",
        issue_key="KAN-10",
        kind="build",
        backend="opencode",
        working_directory=str(other.resolve()),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/x.git",
        branch="feature/KAN-1",
        target_branch="main",
        session_id="claude-session",
        issue_key="KAN-1",
        kind="build",
        backend="claude",
        working_directory=str(folder.resolve()),
    )

    client = TestClient(create_dashboard_app())
    listed = client.get("/api/storage/folders/repo%20a/sessions")
    assert listed.status_code == 200
    body = listed.json()
    assert body["folder"]["name"] == "repo a"
    assert body["folder"]["exists"] is True
    assert [row["session_id"] for row in body["sessions"]] == ["ses_plan", "ses_build"]
    assert body["sessions"][0]["bind_id"] == inside["bind_id"]
    assert body["sessions"][1]["bind_id"] == child["bind_id"]
    assert "claude-session" not in {row["session_id"] for row in body["sessions"]}

    missing = client.get("/api/storage/folders/no_such_clone/sessions")
    assert missing.status_code == 404

    with pytest.raises(TempStorageError) as exc:
        load_storage_folder("../secret")
    assert exc.value.status_code == 400
