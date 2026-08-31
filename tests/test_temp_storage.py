"""Temp-clone disk view and force-delete (including a reserved ``nul`` name)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import (
    TempStorageError,
    build_storage_view,
    force_delete_temp_folder,
    queue_delete_temp_folder,
    reset_delete_jobs,
    reset_size_cache,
    resolve_temp_base,
    scan_folder_sizes_now,
)
from src.temp_fs import (
    force_rmtree,
    force_rmtree_progress,
    format_bytes,
    volume_label,
    win_device_path,
    win_long_path,
)


@pytest.fixture(autouse=True)
def _clear_delete_jobs():
    reset_delete_jobs()
    reset_size_cache()
    yield
    reset_delete_jobs()
    reset_size_cache()


def _wait_gone(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while path.exists() and time.time() < deadline:
        time.sleep(0.04)
    assert not path.exists(), f"still exists after {timeout}s: {path}"


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


def test_force_rmtree_progress_reports_and_deletes(tmp_path: Path):
    root = tmp_path / "clone"
    nested = root / "sub"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("a", encoding="utf-8")
    (nested / "nul").write_text("reserved\n", encoding="utf-8")
    seen: list[tuple[int, int]] = []
    force_rmtree_progress(root, on_progress=lambda d, t: seen.append((d, t)))
    assert not root.exists()
    assert seen
    assert seen[0][0] == 0
    assert seen[-1][0] == seen[-1][1]
    assert seen[-1][1] >= 2


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
    assert view["folders"][0]["in_use"] is False
    if view["folders"][0].get("size_pending"):
        scan_folder_sizes_now()
        view = build_storage_view()
    assert view["folders"][0]["size_bytes"] >= 5
    assert view["folders"][0].get("size_pending") is False


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
    assert gone.status_code == 202
    payload = gone.json()
    assert payload["ok"] is True
    assert payload["accepted"] is True
    assert payload["status"] == "deleting"
    _wait_gone(clone)
    leftover = [f for f in client.get("/api/storage").json()["folders"] if f["name"] == "killme"]
    assert not leftover or leftover[0].get("delete", {}).get("status") == "done"


def test_queue_delete_is_async_and_exposes_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings

    base = tmp_path / ".temp"
    clone = base / "slowme"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def _blocked(path, on_progress=None):  # noqa: ARG001
        started.set()
        if not release.wait(5):
            raise TimeoutError("test did not release delete")
        force_rmtree(path)
        if on_progress is not None:
            on_progress(1, 1)

    with patch("src.dashboard.temp_storage.force_rmtree_progress", side_effect=_blocked):
        accepted = queue_delete_temp_folder("slowme")
        assert accepted["accepted"] is True
        assert started.wait(2)
        assert clone.exists()
        view = build_storage_view()
        row = next(f for f in view["folders"] if f["name"] == "slowme")
        assert row["delete"]["status"] == "deleting"
        assert 0 <= int(row["delete"]["percent"]) <= 99
        second = None
        try:
            queue_delete_temp_folder("slowme")
        except TempStorageError as e:
            second = e
        assert second is not None
        assert second.status_code == 409
        release.set()
    _wait_gone(clone)


def test_storage_api_queue_delete_returns_before_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings

    base = tmp_path / ".temp"
    clone = base / "holdme"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def _blocked(path, on_progress=None):  # noqa: ARG001
        started.set()
        release.wait(5)
        force_rmtree(path)
        if on_progress is not None:
            on_progress(1, 1)

    app = create_dashboard_app()
    client = TestClient(app)
    with patch("src.dashboard.temp_storage.force_rmtree_progress", side_effect=_blocked):
        resp = client.post("/api/storage/delete", json={"name": "holdme"})
        assert resp.status_code == 202
        assert started.wait(2)
        listed = client.get("/api/storage").json()
        row = next(f for f in listed["folders"] if f["name"] == "holdme")
        assert row["delete"]["status"] == "deleting"
        assert clone.exists()
        dup = client.post("/api/storage/delete", json={"name": "holdme"})
        assert dup.status_code == 409
        progress = client.get("/api/storage/deletes")
        assert progress.status_code == 200
        jobs = progress.json()["deletes"]
        assert any(j["name"] == "holdme" and j["status"] == "deleting" for j in jobs)
        release.set()
    _wait_gone(clone)


def test_storage_view_attaches_jira_issue_from_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings
    from src.state.job_store import job_store

    base = tmp_path / ".temp"
    clone = base / "repo_deadbeef1234"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)

    job = job_store.create_job(
        issue_key="STOR-42",
        summary="Login ekranini duzelt",
        workflow_type="direct",
    )
    job_store.update_job(job["job_id"], working_directory=str(clone.resolve()))

    view = build_storage_view()
    row = next(f for f in view["folders"] if f["name"] == "repo_deadbeef1234")
    assert row["issue_key"] == "STOR-42"
    assert row["summary"] == "Login ekranini duzelt"
    assert row["job_id"] == job["job_id"]

    other = base / "orphan_clone"
    other.mkdir()
    view = build_storage_view()
    orphan = next(f for f in view["folders"] if f["name"] == "orphan_clone")
    assert orphan["issue_key"] is None
    assert orphan.get("summary") == ""


def test_storage_view_prefers_newest_job_for_same_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings
    from src.state.job_store import job_store

    base = tmp_path / ".temp"
    clone = base / "repo_cafebabe9999"
    clone.mkdir(parents=True)
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)

    older = job_store.create_job(issue_key="STOR-1", summary="Eski baslik")
    job_store.update_job(
        older["job_id"],
        working_directory=str(clone.resolve()),
        started_at="2026-08-01T10:00:00",
        updated_at="2026-08-01T10:00:00",
    )
    newer = job_store.create_job(issue_key="STOR-1", summary="Yeni baslik")
    job_store.update_job(
        newer["job_id"],
        working_directory=str(clone.resolve()),
        started_at="2026-08-31T12:00:00",
        updated_at="2026-08-31T12:00:00",
    )

    row = next(f for f in build_storage_view()["folders"] if f["name"] == clone.name)
    assert row["issue_key"] == "STOR-1"
    assert row["summary"] == "Yeni baslik"
    assert row["job_id"] == newer["job_id"]


def test_storage_view_attaches_issue_from_session_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings
    from src.state.session_bind_store import session_bind_store

    base = tmp_path / ".temp"
    clone = base / "repo_bindonly0001"
    clone.mkdir(parents=True)
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)

    rec = session_bind_store.upsert(
        repository_url="https://git.example.com/g/r.git",
        branch="feature/STOR-7",
        session_id="ses_stor7",
        issue_key="STOR-7",
        working_directory=str(clone.resolve()),
        target_branch="develop",
        job_id="job_bindonly",
    )
    assert rec is not None

    row = next(f for f in build_storage_view()["folders"] if f["name"] == clone.name)
    assert row["issue_key"] == "STOR-7"
    assert row["job_id"] == "job_bindonly"


def test_storage_api_includes_issue_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings
    from src.state.job_store import job_store

    base = tmp_path / ".temp"
    clone = base / "repo_apiissue01"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)
    job = job_store.create_job(issue_key="STOR-9", summary="API title")
    job_store.update_job(job["job_id"], working_directory=str(clone.resolve()))

    app = create_dashboard_app()
    client = TestClient(app)
    body = client.get("/api/storage").json()
    row = next(f for f in body["folders"] if f["name"] == clone.name)
    assert row["issue_key"] == "STOR-9"
    assert row["summary"] == "API title"
    assert row["job_id"] == job["job_id"]


def test_storage_view_returns_before_size_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from src.config import settings

    base = tmp_path / ".temp"
    clone = base / "huge"
    clone.mkdir(parents=True)
    (clone / "a.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.chdir(tmp_path)

    def _slow_size(path):  # noqa: ARG001
        time.sleep(2)
        return 99

    with patch("src.dashboard.temp_storage._dir_size_bytes", side_effect=_slow_size):
        started = time.monotonic()
        view = build_storage_view()
        elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert view["sizes_pending"] is True
    assert view["folders"][0]["size_pending"] is True
    assert view["disk"]["free_bytes"] > 0
