"""Temp-clone disk view and force-delete (including a reserved ``nul`` name)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import (
    TempStorageError,
    build_storage_view,
    force_delete_temp_folder,
    resolve_temp_base,
)
from src.temp_fs import force_rmtree, format_bytes, volume_label, win_device_path, win_long_path


def test_format_bytes():
    assert format_bytes(0) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1024) == "1.0 KiB"
    assert "MiB" in format_bytes(5 * 1024 * 1024)


def test_win_path_prefixes():
    long = win_long_path(r"C:\vd\.temp\clone")
    assert long.startswith("\\\\?\\")
    assert "clone" in long
    device = win_device_path(r"C:\vd\.temp\clone\nul")
    assert device.startswith("\\\\.\\")
    assert device.endswith("nul") or device.endswith("NUL") or "nul" in device.lower()


def test_force_rmtree_deletes_nul_named_file(tmp_path: Path):
    root = tmp_path / "clone"
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "readme.txt").write_text("ok\n", encoding="utf-8")
    (nested / "nul").write_text("reserved\n", encoding="utf-8")
    force_rmtree(root)
    assert not root.exists()


def test_resolve_temp_base_and_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.config import settings

    base = tmp_path / "tmpclones"
    a = base / "repo_aaa"
    a.mkdir(parents=True)
    (a / "f.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    resolved = resolve_temp_base()
    assert resolved == base.resolve()
    view = build_storage_view()
    assert view["disk"]["free_bytes"] > 0
    assert view["disk"]["total_bytes"] >= view["disk"]["free_bytes"]
    assert view["disk"]["volume"]
    assert view["folder_count"] == 1
    assert view["folders"][0]["name"] == "repo_aaa"
    assert view["folders"][0]["size_bytes"] >= 5
    assert view["folders"][0]["in_use"] is False


def test_force_delete_rejects_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.config import settings

    base = tmp_path / ".temp"
    base.mkdir()
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TempStorageError):
        force_delete_temp_folder("../secret")
    with pytest.raises(TempStorageError):
        force_delete_temp_folder("a/b")
    with pytest.raises(TempStorageError):
        force_delete_temp_folder("missing")


def test_force_delete_removes_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.config import settings

    base = tmp_path / ".temp"
    clone = base / "proj_deadbeef"
    clone.mkdir(parents=True)
    (clone / "nul").write_bytes(b"x")
    (clone / "keep.txt").write_text("y", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    out = force_delete_temp_folder("proj_deadbeef")
    assert out["ok"] is True
    assert not clone.exists()


def test_storage_api_list_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.config import settings

    base = tmp_path / ".temp"
    clone = base / "killme"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    app = create_dashboard_app()
    client = TestClient(app)
    listed = client.get("/api/storage")
    assert listed.status_code == 200
    body = listed.json()
    assert body["folder_count"] == 1
    assert body["folders"][0]["name"] == "killme"
    assert "free_label" in body["disk"]
    bad = client.post("/api/storage/delete", json={"name": "../etc"})
    assert bad.status_code in (400, 422)
    gone = client.post("/api/storage/delete", json={"name": "killme"})
    assert gone.status_code == 200
    assert gone.json()["ok"] is True
    assert not clone.exists()
    again = client.get("/api/storage")
    assert again.json()["folder_count"] == 0
