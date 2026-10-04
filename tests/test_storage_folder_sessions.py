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


def test_storage_lists_opencode_sessions_whose_folder_is_gone(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Age delete removes the clone and keeps the session. The page must still list it."""
    from src.config import settings

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    base = tmp_path / "clones"
    live = base / "KAN-1"
    live.mkdir(parents=True)
    gone = base / "multi_dead"
    sibling = base / "KAN-10"
    monkeypatch.setattr(settings, "temp_dir_base", base)

    binds.upsert(
        repository_url="https://gitlab.example/group/live.git",
        branch="feature/KAN-1",
        target_branch="main",
        session_id="ses_live",
        issue_key="KAN-1",
        kind="build",
        backend="opencode",
        working_directory=str(live),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/nested.git",
        branch="feature/KAN-1b",
        target_branch="main",
        session_id="ses_nested",
        issue_key="KAN-1",
        kind="plan",
        backend="opencode",
        working_directory=str(live / "checkout"),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/gone.git",
        branch="feature/KAN-2",
        target_branch="main",
        session_id="ses_gone",
        issue_key="KAN-2",
        kind="plan",
        backend="opencode",
        working_directory=str(gone / "orders-api"),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/sib.git",
        branch="feature/KAN-10",
        target_branch="main",
        session_id="ses_sibling",
        issue_key="KAN-10",
        kind="build",
        backend="opencode",
        working_directory=str(sibling),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/claude.git",
        branch="feature/KAN-3",
        target_branch="main",
        session_id="claude-gone",
        issue_key="KAN-3",
        kind="build",
        backend="claude",
        working_directory=str(gone),
    )
    binds.upsert(
        repository_url="https://gitlab.example/group/empty.git",
        branch="feature/KAN-4",
        target_branch="main",
        session_id="ses_nodir",
        issue_key="KAN-4",
        kind="test",
        backend="opencode",
        working_directory="",
    )

    client = TestClient(create_dashboard_app())
    listed = client.get("/api/storage")
    assert listed.status_code == 200
    body = listed.json()
    assert [row["name"] for row in body["folders"]] == ["KAN-1"]
    found = {row["session_id"]: row for row in body["sessions_without_folder"]}
    assert set(found) == {"ses_gone", "ses_sibling", "ses_nodir"}
    assert found["ses_gone"]["working_directory"].endswith("orders-api")
    assert "claude-gone" not in found
    assert "ses_live" not in found
    assert "ses_nested" not in found
