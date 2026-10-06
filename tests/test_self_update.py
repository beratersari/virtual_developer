"""LAN release check, zip apply, and the restart helper."""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import apply_settings_update
from src.self_update import (
    _JOB,
    _LOCK,
    _interpreter,
    download_release,
    fetch_latest,
    listener_pids_from_netstat,
    platform_from_os,
    release_base_url,
    run_apply_job,
    start_apply,
    update_stopped_install,
    versions_equal,
)
from src.update_helper import (
    UpdateError,
    apply_frozen_tree,
    apply_source_tree,
    assert_install_dir,
    classify_members,
    pid_alive,
    port_open,
    run_plan,
    safe_extract,
)

# Ports the operator's stack uses on this machine. Tests must not bind or stop them.
_LIVE_PORTS = {8080, 5173, 4096, 8090}


@pytest.fixture(autouse=True)
def _isolate_update_work_dir(tmp_path, monkeypatch):
    """Keep yaver update logs out of the operator's data folder."""
    work = tmp_path / "update-work"
    monkeypatch.setattr("src.self_update._work_dir", lambda: work)


@pytest.fixture(autouse=True)
def _reset_job():
    blank = {
        "phase": "idle",
        "error": "",
        "remote_version": "",
        "remote_notes": "",
        "remote_layout": "",
        "bytes_done": 0,
        "bytes_total": 0,
    }
    with _LOCK:
        _JOB.clear()
        _JOB.update(blank)
    yield
    with _LOCK:
        _JOB.clear()
        _JOB.update(blank)


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_release_address_is_host_and_port_only():
    assert release_base_url("192.168.1.20", 8090) == "http://192.168.1.20:8090"
    assert release_base_url("releases.internal", 80) == "http://releases.internal:80"
    assert release_base_url("::1", 8090) == "http://[::1]:8090"
    with pytest.raises(UpdateError):
        release_base_url("http://192.168.1.20", 8090)
    with pytest.raises(UpdateError):
        release_base_url("192.168.1.20/admin", 8090)
    with pytest.raises(UpdateError):
        release_base_url("", 8090)
    with pytest.raises(UpdateError):
        release_base_url("192.168.1.20", 0)


def test_platform_ids_follow_windows_and_ubuntu():
    assert platform_from_os("Windows") == "windows"
    release = 'ID=ubuntu\nVERSION_ID="22.04"\n'
    assert platform_from_os("Linux", release) == "ubuntu-22.04"
    assert platform_from_os("Linux", 'ID=ubuntu\nVERSION_ID="24.04"\n') == "ubuntu-24.04"
    with pytest.raises(UpdateError):
        platform_from_os("Linux", 'ID=debian\nVERSION_ID="12"\n')
    with pytest.raises(UpdateError):
        platform_from_os("Linux", 'ID=ubuntu\nVERSION_ID="16.04"\n')
    assert versions_equal("0.9.71", "0.9.71")
    assert versions_equal("0.9.71", "0.9.71+g1")
    assert not versions_equal("0.9.71", "0.9.72")
    assert not versions_equal("0.9.71", "0.9.71-rc1")
    assert not versions_equal("0.9.71-rc1", "0.9.71-rc2")


def test_classify_and_reject_parent_paths(tmp_path: Path):
    assert classify_members(["yaver.exe", "_internal/python.dll"]) == "frozen"
    assert classify_members(["pkg/src/daemon.py", "pkg/VERSION"]) == "source"
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../evil.txt", "no")
    with pytest.raises(UpdateError):
        safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_extract_keeps_the_launcher_executable(tmp_path: Path):
    archive = tmp_path / "app.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        launcher = zipfile.ZipInfo("yaver")
        launcher.external_attr = 0o100755 << 16
        handle.writestr(launcher, b"#!/bin/sh\n")
        readme = zipfile.ZipInfo("README")
        readme.external_attr = 0o100644 << 16
        handle.writestr(readme, b"hi\n")
        bare = zipfile.ZipInfo("yaver.exe")
        handle.writestr(bare, b"MZ")
    root = safe_extract(archive, tmp_path / "out")
    if sys.platform == "win32":
        assert (root / "yaver").is_file()
        assert (root / "yaver.exe").is_file()
        return
    assert (root / "yaver").stat().st_mode & 0o111
    assert not (root / "README").stat().st_mode & 0o111
    assert (root / "yaver.exe").stat().st_mode & 0o111


def test_source_apply_keeps_env_and_venv(tmp_path: Path):
    install = tmp_path / "app"
    (install / "src").mkdir(parents=True)
    (install / "src" / "old.py").write_text("old", encoding="utf-8")
    (install / "src" / "keep.py").write_text("old-keep", encoding="utf-8")
    env_path = install / ".env"
    env_path.write_bytes(b"JIRA_HOST=https://jira\n")
    env_path.chmod(stat.S_IREAD)
    (install / ".venv").mkdir()
    (install / ".venv" / "pyvenv.cfg").write_text("home = here\n", encoding="utf-8")
    (install / "notes.txt").write_text("leave me", encoding="utf-8")
    staging = tmp_path / "stage"
    (staging / "src").mkdir(parents=True)
    (staging / "src" / "keep.py").write_text("new-keep", encoding="utf-8")
    (staging / "VERSION").write_text("0.9.72\n", encoding="utf-8")
    (staging / ".env").write_text("JIRA_HOST=replaced\n", encoding="utf-8")
    try:
        apply_source_tree(staging, install)
        assert (install / ".env").read_bytes() == b"JIRA_HOST=https://jira\n"
        assert env_path.stat().st_mode & stat.S_IWRITE == 0
    finally:
        env_path.chmod(stat.S_IWRITE)
    assert (install / ".venv" / "pyvenv.cfg").is_file()
    assert not (install / "src" / "old.py").exists()
    assert (install / "src" / "keep.py").read_text(encoding="utf-8") == "new-keep"
    assert (install / "VERSION").read_text(encoding="utf-8").startswith("0.9.72")
    assert (install / "notes.txt").read_text(encoding="utf-8") == "leave me"


def test_frozen_apply_keeps_env_and_moves_the_old_tree(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_bytes("TOKEN=keep\n".encode("utf-8"))
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "_internal" / "marker").write_text("1", encoding="utf-8")
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / ".env").write_bytes(b"TOKEN=from-package\n")
    apply_frozen_tree(staging, install)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"


def test_frozen_apply_leaves_an_existing_env_file(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    env_path = install / ".env"
    env_path.write_bytes(b"TOKEN=keep\n")
    before = env_path.stat()
    env_path.chmod(stat.S_IREAD)
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / ".env").write_bytes(b"TOKEN=from-package\n")
    try:
        apply_frozen_tree(staging, install)
        assert env_path.read_bytes() == b"TOKEN=keep\n"
        assert env_path.stat().st_ino == before.st_ino
        assert env_path.stat().st_mode & stat.S_IWRITE == 0
        assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    finally:
        env_path.chmod(stat.S_IWRITE)


def test_frozen_apply_copies_env_when_the_install_has_none(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / ".env").write_bytes(b"TOKEN=from-package\n")
    apply_frozen_tree(staging, install)
    assert (install / ".env").read_bytes() == b"TOKEN=from-package\n"


def test_frozen_apply_works_while_a_console_is_in_the_folder(tmp_path: Path):
    """A prompt whose current directory is the install cannot be renamed away."""
    import subprocess

    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (install / ".env").write_bytes(b"TOKEN=keep\n")
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "_internal" / "marker.txt").write_text("bundle", encoding="utf-8")
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(install),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        apply_frozen_tree(staging, install)
    finally:
        holder.kill()
        holder.wait(timeout=5)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / "VERSION").read_text(encoding="utf-8").startswith("0.2.0")
    assert (install / "_internal" / "marker.txt").read_text(encoding="utf-8") == "bundle"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"
    assert (tmp_path / "Yaver.previous" / "VERSION").read_text(encoding="utf-8").startswith("0.1.0")
    assert holder.poll() is not None


def test_helper_refuses_a_git_checkout(tmp_path: Path):
    install = tmp_path / "repo"
    (install / "src").mkdir(parents=True)
    (install / "src" / "daemon.py").write_text("x", encoding="utf-8")
    (install / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (install / ".git").mkdir()
    (install / ".env").write_text("KEEP=1\n", encoding="utf-8")
    staging = tmp_path / "stage"
    (staging / "src").mkdir(parents=True)
    (staging / "src" / "daemon.py").write_text("y", encoding="utf-8")
    (staging / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 0,
                "layout": "source",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "cwd": str(install),
                "version": "0.2.0",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError):
        run_plan(plan)
    assert (install / ".env").read_text(encoding="utf-8") == "KEEP=1\n"
    assert "y" not in (install / "src" / "daemon.py").read_text(encoding="utf-8")


def test_helper_replaces_files_and_does_not_start(tmp_path: Path):
    install = tmp_path / "app"
    (install / "src").mkdir(parents=True)
    (install / "src" / "daemon.py").write_text("old", encoding="utf-8")
    (install / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (install / ".env").write_bytes(b"BOARD=2\n")
    (install / ".venv").mkdir()
    staging = tmp_path / "stage"
    (staging / "src").mkdir(parents=True)
    (staging / "src" / "daemon.py").write_text("new", encoding="utf-8")
    (staging / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    started = install / "started.txt"
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 0,
                "layout": "source",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(started)!r}).write_text('yes')",
                ],
                "cwd": str(install),
                "version": "0.2.0",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    run_plan(plan)
    assert (install / ".env").read_bytes() == b"BOARD=2\n"
    assert (install / ".venv").is_dir()
    assert (install / "src" / "daemon.py").read_text(encoding="utf-8") == "new"
    assert not started.exists()
    log = (tmp_path / "update.log").read_text(encoding="utf-8")
    assert "updated 0.2.0" in log
    assert "started " not in log
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["version"] == "0.2.0"


def test_helper_keeps_the_new_files_without_opening_a_port(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_bytes(b"TOKEN=keep\n")
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 9,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                "cwd": str(tmp_path),
                "version": "0.2.0",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True


def test_helper_does_not_launch_the_plan_command(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_bytes(b"TOKEN=keep\n")
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    marker = tmp_path / "launched.txt"
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 9,
                "probe_host": "127.0.0.1",
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')",
                ],
                "cwd": str(install),
                "version": "0.2.0",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    assert not marker.exists()


def _frozen_plan(tmp_path: Path, install: Path, staging: Path, **extra: object) -> Path:
    body = {
        "created": 10**10,
        "pid": 0,
        "port": 0,
        "probe_host": "127.0.0.1",
        "layout": "frozen",
        "staging": str(staging),
        "install_root": str(install),
        "argv": [sys.executable, "-c", "raise SystemExit(0)"],
        "cwd": str(tmp_path),
        "version": "0.9.71",
        "log": str(tmp_path / "update.log"),
        "result": str(tmp_path / "result.json"),
        "health_seconds": 0,
    }
    body.update(extra)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_frozen_swap_keeps_a_data_folder_inside_the_install(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / "extra.txt").write_text("gone", encoding="utf-8")
    data = install / "office-data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    outside = tmp_path / "outside-data"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside", encoding="utf-8")
    (install / ".env").write_text(
        f"TOKEN=keep\nYAVER_BASE_DIR={data}\n",
        encoding="utf-8",
    )
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "_internal" / "VERSION").write_text("0.9.71\n", encoding="utf-8")
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    run_plan(_frozen_plan(tmp_path, install, staging))
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / "_internal" / "VERSION").read_text(encoding="utf-8") == "0.9.71\n"
    assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert "TOKEN=keep" in (install / ".env").read_text(encoding="utf-8")
    assert not (install / "extra.txt").exists()
    assert (tmp_path / "Yaver.previous" / "extra.txt").read_text(encoding="utf-8") == "gone"
    assert not (tmp_path / "Yaver.previous" / "office-data").exists()
    assert not (tmp_path / "Yaver.userdata").exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "outside"


def test_frozen_swap_replaces_the_bundle_when_data_dir_is_internal(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "_internal" / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_text(
        f"TOKEN=keep\nYAVER_BASE_DIR={install / '_internal'}\n",
        encoding="utf-8",
    )
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "_internal" / "VERSION").write_text("0.9.71\n", encoding="utf-8")
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    run_plan(_frozen_plan(tmp_path, install, staging))
    assert (install / "_internal" / "VERSION").read_text(encoding="utf-8") == "0.9.71\n"
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"


def test_swap_keeps_an_inside_data_folder_without_starting(tmp_path: Path):
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    data = install / "office-data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    (install / ".env").write_bytes(f"TOKEN=keep\nYAVER_BASE_DIR={data}\n".encode())
    staging = tmp_path / "new"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    plan = _frozen_plan(
        tmp_path,
        install,
        staging,
        port=9,
        cwd=str(install),
        argv=[sys.executable, "-c", "import time; time.sleep(120)"],
        health_seconds=1,
    )
    run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert not (tmp_path / "Yaver.userdata").exists()


def test_drive_root_is_refused():
    if sys.platform != "win32":
        root = Path("/")
    else:
        root = Path("C:\\")
    with pytest.raises(UpdateError):
        assert_install_dir(root)


class _ReleaseHandler(BaseHTTPRequestHandler):
    body = b""
    meta: dict = {}
    status = 200
    redirect = ""

    def do_GET(self):  # noqa: N802
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", self.redirect)
            self.end_headers()
            return
        if self.path.startswith("/api/latest"):
            payload = json.dumps(self.meta).encode("utf-8")
            self.send_response(self.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path.startswith("/download/"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        return


def _serve() -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ReleaseHandler)
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_download_checks_the_checksum_and_refuses_a_redirect(tmp_path: Path):
    import hashlib

    payload = _zip({"src/daemon.py": b"print(1)\n", "VERSION": b"0.9.72\n"})
    digest = hashlib.sha256(payload).hexdigest()
    _ReleaseHandler.body = payload
    _ReleaseHandler.redirect = ""
    _ReleaseHandler.meta = {
        "version": "0.9.72",
        "sha256": digest,
        "size": len(payload),
        "layout": "source",
        "notes": "office build",
    }
    server, port = _serve()
    try:
        base = f"http://127.0.0.1:{port}"
        meta = fetch_latest(base, "windows")
        assert meta["version"] == "0.9.72"
        dest = tmp_path / "pkg.zip"
        download_release(base, "windows", dest, expected_sha=digest, expected_size=len(payload))
        assert dest.read_bytes() == payload
        with pytest.raises(UpdateError):
            download_release(
                base,
                "windows",
                tmp_path / "bad.zip",
                expected_sha="0" * 64,
                expected_size=len(payload),
            )
        _ReleaseHandler.redirect = "http://example.invalid/package.zip"
        with pytest.raises(UpdateError):
            fetch_latest(base, "windows")
    finally:
        server.shutdown()
        server.server_close()
        _ReleaseHandler.redirect = ""


def test_apply_job_spawns_a_helper_and_then_stops(tmp_path: Path, monkeypatch):
    import hashlib

    payload = _zip({"src/daemon.py": b"x", "VERSION": b"0.9.72\n"})
    digest = hashlib.sha256(payload).hexdigest()
    _ReleaseHandler.body = payload
    _ReleaseHandler.redirect = ""
    _ReleaseHandler.meta = {
        "version": "0.9.72",
        "sha256": digest,
        "size": len(payload),
        "layout": "source",
        "notes": "",
    }
    server, port = _serve()
    calls: list[tuple[Path, str]] = []
    stopped: list[str] = []
    install = tmp_path / "install"
    install.mkdir()
    monkeypatch.setattr("src.self_update.local_platform", lambda: "windows")
    monkeypatch.setattr("src.self_update.local_layout", lambda: "source")
    monkeypatch.setattr("src.self_update.install_root", lambda: install)
    monkeypatch.setattr("src.self_update._work_dir", lambda: tmp_path / "work")
    monkeypatch.setattr("src.__version__", "0.9.71")
    try:
        run_apply_job(
            f"http://127.0.0.1:{port}",
            lambda: stopped.append("down"),
            spawn=lambda plan, kind: calls.append((plan, kind)),
        )
    finally:
        server.shutdown()
        server.server_close()
    assert stopped == ["down"]
    assert calls and calls[0][1] == "python"
    plan = json.loads(calls[0][0].read_text(encoding="utf-8"))
    assert plan["install_root"] == str(install)
    assert plan["version"] == "0.9.72"
    assert plan["layout"] == "source"
    assert (tmp_path / "work" / "staging").exists()


def test_apply_job_waits_for_the_helper_and_does_not_start(tmp_path: Path, monkeypatch):
    import hashlib
    import os

    payload = _zip({"src/daemon.py": b"x", "VERSION": b"0.9.72\n"})
    digest = hashlib.sha256(payload).hexdigest()
    _ReleaseHandler.body = payload
    _ReleaseHandler.redirect = ""
    _ReleaseHandler.meta = {
        "version": "0.9.72",
        "sha256": digest,
        "size": len(payload),
        "layout": "source",
        "notes": "",
    }
    server, port = _serve()
    stopped: list[str] = []
    install = tmp_path / "install"
    install.mkdir()
    marker = tmp_path / "dashboard-started.txt"
    launcher = tmp_path / "should_not_run.py"
    launcher.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('yes')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("src.self_update.local_platform", lambda: "windows")
    monkeypatch.setattr("src.self_update.local_layout", lambda: "source")
    monkeypatch.setattr("src.self_update.install_root", lambda: install)
    monkeypatch.setattr("src.self_update._work_dir", lambda: tmp_path / "work")
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 0))
    monkeypatch.setattr(
        "src.self_update._restart_argv",
        lambda: [sys.executable, str(launcher)],
    )
    monkeypatch.setattr("src.__version__", "0.9.71")

    def fail_exec(*_args, **_kwargs):
        raise AssertionError("the update command must wait, not replace this process")

    monkeypatch.setattr("src.self_update.os.execv", fail_exec)
    previous = Path.cwd()
    try:
        run_apply_job(f"http://127.0.0.1:{port}", lambda: stopped.append("down"))
    finally:
        os.chdir(previous)
        server.shutdown()
        server.server_close()
    assert stopped == ["down"]
    assert (install / "src" / "daemon.py").read_bytes() == b"x"
    assert (install / "VERSION").read_bytes().startswith(b"0.9.72")
    assert not marker.exists()
    plan = json.loads((tmp_path / "work" / "plan.json").read_text(encoding="utf-8"))
    assert plan["pid"] == 0
    assert plan["version"] == "0.9.72"
    log = (tmp_path / "work" / "update.log").read_text(encoding="utf-8")
    assert "updated 0.9.72" in log
    assert "started " not in log


def test_windows_executable_exits_before_the_helper_moves_internal(
    tmp_path: Path, monkeypatch
):
    """A frozen Windows update must not swap while yaver.exe is still open."""
    import hashlib
    import os

    payload = _zip({"yaver.exe": b"new", "_internal/marker.txt": b"bundle"})
    digest = hashlib.sha256(payload).hexdigest()
    _ReleaseHandler.body = payload
    _ReleaseHandler.redirect = ""
    _ReleaseHandler.meta = {
        "version": "0.9.75",
        "sha256": digest,
        "size": len(payload),
        "layout": "frozen",
        "notes": "",
    }
    server, port = _serve()
    started: list[list[str]] = []
    stopped: list[str] = []
    lines: list[str] = []
    install = tmp_path / "Yaver"
    install.mkdir()
    monkeypatch.setattr("src.self_update.local_platform", lambda: "windows")
    monkeypatch.setattr("src.self_update.local_layout", lambda: "frozen")
    monkeypatch.setattr("src.self_update.install_root", lambda: install)
    monkeypatch.setattr("src.self_update._work_dir", lambda: tmp_path / "work")
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 0))
    monkeypatch.setattr(
        "src.self_update._windows_frozen_handoff",
        lambda layout: layout == "frozen",
    )
    monkeypatch.setattr("src.__version__", "0.9.74")

    def refuse_foreground(*_args, **_kwargs):
        raise AssertionError("yaver.exe must exit before the helper moves _internal")

    monkeypatch.setattr("src.self_update._run_helper_foreground", refuse_foreground)
    monkeypatch.setattr(
        "src.self_update._spawn_console",
        lambda args, cwd: started.append(list(args)),
    )
    previous = Path.cwd()
    try:
        run_apply_job(
            f"http://127.0.0.1:{port}",
            lambda: stopped.append("down"),
            report=lines.append,
        )
    finally:
        os.chdir(previous)
        server.shutdown()
        server.server_close()
    assert stopped == ["down"]
    assert started
    plan = json.loads((tmp_path / "work" / "plan.json").read_text(encoding="utf-8"))
    assert plan["pid"] == os.getpid()
    assert plan["layout"] == "frozen"
    text = "\n".join(lines)
    assert "Closing Yaver so Windows can replace the files." in text
    assert "Wait for the line that begins with Updated to." in text
    assert "helper continues after this process exits" in text
    assert "Replacing the files." not in text
    assert not (install / "yaver.exe").exists()
    assert not (install / "_internal").exists()


def test_a_second_apply_is_rejected_while_the_first_is_starting(monkeypatch):
    """The busy flag is taken before the slow settings write."""
    import time

    monkeypatch.setattr("src.self_update.checkout_block", lambda: "")
    monkeypatch.setattr("src.self_update.local_platform", lambda: "windows")
    monkeypatch.setattr(
        "src.self_update.save_release_target",
        lambda *_args, **_kwargs: time.sleep(0.3),
    )

    class _Thread:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def start(self) -> None:
            return None

    real_thread = threading.Thread
    monkeypatch.setattr("src.self_update.threading.Thread", _Thread)
    outcomes: list[object] = []

    def run() -> None:
        try:
            start_apply("127.0.0.1", 8090, shutdown=lambda: None)
        except UpdateError as exc:
            outcomes.append(exc.status_code)
        else:
            outcomes.append("started")

    threads = [real_thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(str(item) for item in outcomes) == ["409", "started"]


def test_apply_job_does_not_stop_when_the_package_kind_differs(tmp_path: Path, monkeypatch):
    _ReleaseHandler.redirect = ""
    _ReleaseHandler.meta = {
        "version": "0.9.72",
        "sha256": "ab" * 32,
        "size": 10,
        "layout": "frozen",
        "notes": "",
    }
    server, port = _serve()
    stopped: list[str] = []
    monkeypatch.setattr("src.self_update.local_platform", lambda: "windows")
    monkeypatch.setattr("src.self_update.local_layout", lambda: "source")
    monkeypatch.setattr("src.__version__", "0.9.71")
    try:
        with pytest.raises(UpdateError):
            run_apply_job(
                f"http://127.0.0.1:{port}",
                lambda: stopped.append("down"),
                spawn=lambda plan, kind: stopped.append("spawn"),
            )
    finally:
        server.shutdown()
        server.server_close()
    assert stopped == []


def test_settings_save_the_release_server(tmp_path: Path, monkeypatch):
    from src.config import settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "release_host", "")
    monkeypatch.setattr(settings, "release_port", 0)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: tmp_path / "runtime_settings.json")
    view = apply_settings_update(SettingsUpdate(release_host="10.1.2.3", release_port=8090))
    assert view.release_host == "10.1.2.3"
    assert view.release_port == 8090
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "RELEASE_HOST=10.1.2.3" in env
    assert "RELEASE_PORT=8090" in env
    stored = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert stored["release_host"] == "10.1.2.3"
    assert stored["release_port"] == 8090


def test_dashboard_update_routes_are_gone():
    app = create_dashboard_app()
    client = TestClient(app)
    assert client.get("/api/update").status_code == 404
    for path in ("/api/update/check", "/api/update/apply"):
        response = client.post(path, json={"release_host": "10.0.0.8", "release_port": 8090})
        assert response.status_code in {404, 405}
        assert "can_apply" not in response.text
        assert "update_available" not in response.text


def _stop_test_pid(pid: int) -> None:
    """Stop a process this test started. Never the test runner."""
    if pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        from src.update_helper import _terminate_one

        _terminate_one(pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _ports_still_open(ports: list[int]) -> None:
    closed = [port for port in ports if not port_open(port, "127.0.0.1")]
    assert closed == []


def test_netstat_listener_pids_match_the_exact_port():
    text = "\n".join(
        [
            "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       16960",
            "  TCP    127.0.0.1:18080        0.0.0.0:0              LISTENING       22222",
            "  TCP    [::]:8080              [::]:0                 LISTENING       16960",
            "  TCP    [fe80::1%12]:8080      [::]:0                 LISTENING       16960",
            "  TCP    127.0.0.1:8080         127.0.0.1:54321        ESTABLISHED     16960",
            "  TCP    127.0.0.1:8080         127.0.0.1:1            TIME_WAIT       0",
            "  TCP    0.0.0.0:80             0.0.0.0:0              LISTENING       5",
            "  TCP    127.0.0.1:50000        10.0.0.1:8080          LISTENING       99",
        ]
    )
    assert listener_pids_from_netstat(text, 8080) == [16960]
    assert listener_pids_from_netstat(text, 18080) == [22222]
    assert listener_pids_from_netstat(text, 80) == [5]
    assert listener_pids_from_netstat("", 8080) == []


def test_windows_listener_lookup_uses_the_exact_port(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("netstat lookup")
    sample = (
        "TCP 0.0.0.0:8080 0.0.0.0:0 LISTENING 16960\n"
        "TCP 127.0.0.1:18080 0.0.0.0:0 LISTENING 22222\n"
    )

    def fake_run(argv, **_kwargs):
        assert argv == ["netstat", "-ano"]

        class Result:
            stdout = sample

        return Result()

    monkeypatch.setattr("src.self_update.subprocess.run", fake_run)
    from src.self_update import _dashboard_listener_pids

    assert _dashboard_listener_pids(8080) == [16960]
    assert _dashboard_listener_pids(18080) == [22222]


def test_linux_listener_lookup_reads_ss(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(list(argv))

        class Result:
            stdout = 'users:(("python",pid=4242,fd=3))'

        return Result()

    monkeypatch.setattr("src.self_update.subprocess.run", fake_run)
    from src.self_update import _dashboard_listener_pids

    assert _dashboard_listener_pids(23456) == [4242]
    assert seen == [["ss", "-ltnp", "sport = :23456"]]


def test_linux_stop_signals_that_pid_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # Windows Python has no SIGKILL. The Linux branch still has to name it.
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((int(pid), int(sig))))
    outcomes = {"dead": True}
    monkeypatch.setattr(
        "src.update_helper.wait_dead",
        lambda pid, seconds: outcomes["dead"],
    )
    from src.self_update import _stop_listener

    _stop_listener(0)
    _stop_listener(-3)
    _stop_listener(os.getpid())
    _stop_listener(424242)
    assert sent == [(424242, int(signal.SIGTERM))]
    sent.clear()
    outcomes["dead"] = False
    _stop_listener(424242)
    assert sent == [
        (424242, int(signal.SIGTERM)),
        (424242, int(signal.SIGKILL)),
    ]


def test_linux_stop_leaves_a_missing_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def missing(_pid, _sig):
        raise OSError("missing")

    monkeypatch.setattr(os, "kill", missing)
    waited: list[int] = []
    monkeypatch.setattr(
        "src.update_helper.wait_dead",
        lambda pid, seconds: waited.append(pid) or False,
    )
    from src.self_update import _stop_listener

    _stop_listener(424242)
    assert waited == []


@pytest.mark.parametrize("kind", ["empty", "self", "zero"])
def test_update_does_not_download_when_the_listener_is_unknown(tmp_path: Path, monkeypatch, kind):
    if kind == "self":
        pids = [os.getpid()]
    elif kind == "zero":
        pids = [0, os.getpid()]
    else:
        pids = []
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 9))
    monkeypatch.setattr(
        "src.update_helper.port_open",
        lambda port, host="127.0.0.1": True,
    )
    monkeypatch.setattr("src.self_update._dashboard_listener_pids", lambda port: list(pids))
    stopped: list[int] = []
    monkeypatch.setattr("src.self_update._stop_listener", lambda pid: stopped.append(pid))

    def download(*_args, **_kwargs):
        raise AssertionError("download started without a listener pid")

    monkeypatch.setattr("src.self_update.run_apply_job", download)
    lines: list[str] = []
    with pytest.raises(UpdateError, match="could not be found"):
        update_stopped_install(
            "10.0.0.8",
            8090,
            shutdown=lambda: None,
            report=lines.append,
        )
    assert stopped == []
    text = "\n".join(lines)
    assert "no listener pid was found" in text
    assert "stopping listener" not in text
    assert "Update log:" not in text
    assert not (tmp_path / "update-work" / "update.log").exists()


def test_update_does_not_download_when_the_port_stays_open(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 9))
    monkeypatch.setattr(
        "src.update_helper.port_open",
        lambda port, host="127.0.0.1": True,
    )
    monkeypatch.setattr(
        "src.update_helper.wait_port_closed",
        lambda port, seconds, host="127.0.0.1": False,
    )
    monkeypatch.setattr("src.self_update._dashboard_listener_pids", lambda port: [424242])
    stopped: list[int] = []
    monkeypatch.setattr("src.self_update._stop_listener", lambda pid: stopped.append(pid))

    def download(*_args, **_kwargs):
        raise AssertionError("download started while the port was open")

    monkeypatch.setattr("src.self_update.run_apply_job", download)
    lines: list[str] = []
    with pytest.raises(UpdateError, match="Yaver is still running"):
        update_stopped_install(
            "10.0.0.8",
            8090,
            shutdown=lambda: None,
            report=lines.append,
        )
    assert stopped == [424242]
    text = "\n".join(lines)
    assert "Update log:" not in text
    assert "Stopping Yaver." in lines
    assert "stopping listener pid=424242" in text
    assert "dashboard port 9 still open" in text
    assert not (tmp_path / "update-work" / "update.log").exists()


def test_update_skips_stop_when_the_dashboard_port_is_closed(tmp_path: Path, monkeypatch):
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port in _LIVE_PORTS:
        pytest.skip("ephemeral port collided with a live service")
    monkeypatch.setattr(
        "src.self_update._dashboard_probe",
        lambda bound=port: ("127.0.0.1", bound),
    )
    stopped: list[int] = []
    monkeypatch.setattr("src.self_update._stop_listener", lambda pid: stopped.append(pid))
    monkeypatch.setattr(
        "src.self_update.checkout_block",
        lambda: "This folder is a git checkout.",
    )
    lines: list[str] = []
    with pytest.raises(UpdateError, match="git checkout"):
        update_stopped_install(
            "10.0.0.8",
            1,
            shutdown=lambda: None,
            report=lines.append,
        )
    text = "\n".join(lines)
    assert "Update log:" not in text
    assert "Stopping Yaver." not in text
    assert "dashboard port" in text and "is closed" in text
    assert stopped == []
    assert not (tmp_path / "update-work" / "update.log").exists()


def test_update_stops_the_dashboard_listener_then_continues(tmp_path: Path, monkeypatch):
    """Stop only the process on the dashboard port, then start the download."""
    watched = [port for port in sorted(_LIVE_PORTS) if port_open(port, "127.0.0.1")]
    note = tmp_path / "listener.txt"
    script = tmp_path / "yaver_update_listener.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os
            import socket
            import subprocess
            import sys
            import time

            note = sys.argv[1]
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = int(sock.getsockname()[1])
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(120)",
                    "yaver-update-listener-child",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            with open(note, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()) + " " + str(port) + " " + str(child.pid) + chr(10))
            while True:
                time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(note), "yaver-update-listener"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener_pid = 0
    child_pid = 0
    bound = 0
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not note.is_file():
            if proc.poll() is not None:
                pytest.fail(f"listener exited {proc.returncode}")
            time.sleep(0.05)
        assert note.is_file()
        listener_pid, bound, child_pid = (
            int(part) for part in note.read_text(encoding="ascii").split()
        )
        assert bound not in _LIVE_PORTS
        assert listener_pid > 0 and listener_pid != os.getpid()
        assert child_pid > 0 and child_pid not in {listener_pid, os.getpid(), proc.pid}
        assert pid_alive(child_pid)
        from src.self_update import _dashboard_listener_pids

        seen: list[int] = []
        deadline = time.time() + 5
        while time.time() < deadline:
            seen = _dashboard_listener_pids(bound)
            if listener_pid in seen:
                break
            time.sleep(0.1)
        assert seen == [listener_pid]
        monkeypatch.setattr(
            "src.self_update._dashboard_probe",
            lambda chosen=bound: ("127.0.0.1", chosen),
        )
        monkeypatch.setattr("src.self_update.checkout_block", lambda: "")
        monkeypatch.setattr("src.self_update.save_release_target", lambda host, port: None)
        calls: list[str] = []

        def run(base, shutdown, spawn=None, report=None):
            del shutdown, spawn, report
            assert not port_open(bound, "127.0.0.1")
            assert not pid_alive(listener_pid)
            assert pid_alive(child_pid)
            calls.append(base)

        monkeypatch.setattr("src.self_update.run_apply_job", run)
        lines: list[str] = []
        update_stopped_install(
            "10.0.0.8",
            8090,
            shutdown=lambda: None,
            report=lines.append,
        )
        assert calls == ["http://10.0.0.8:8090"]
        text = "\n".join(lines)
        assert "Update log:" not in text
        assert "Stopping Yaver." in lines
        import re

        stopped_pids = [
            int(match) for match in re.findall(r"stopping listener pid=(\d+)", text)
        ]
        assert stopped_pids == [listener_pid]
        assert f"dashboard port {bound} is closed" in text
        assert not (tmp_path / "update-work" / "update.log").exists()
        assert proc.wait(timeout=5) is not None
        _ports_still_open(watched)
    finally:
        _stop_test_pid(listener_pid)
        _stop_test_pid(child_pid)
        if proc.poll() is None:
            _stop_test_pid(proc.pid)
            proc.wait(timeout=5)


def test_frozen_swap_log_omits_the_env_file(tmp_path: Path, capsys) -> None:
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_text("TOKEN=super-secret-value\n", encoding="utf-8")
    staging = tmp_path / "staged"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / ".env").write_text("TOKEN=from-package\n", encoding="utf-8")
    notes: list[str] = []
    apply_frozen_tree(staging, install, note=notes.append)
    text = "\n".join(notes)
    shown = capsys.readouterr().out
    assert "super-secret-value" not in text
    assert "from-package" not in text
    assert "super-secret-value" not in shown
    assert "from-package" not in shown
    assert "env file present" in text
    assert "env file present" in shown
    assert "env file left in place" in text
    assert "package env file skipped" in text
    assert (install / ".env").read_text(encoding="utf-8") == "TOKEN=super-secret-value\n"


def test_stopped_update_refuses_a_checkout(monkeypatch):
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 0))
    monkeypatch.setattr(
        "src.self_update.checkout_block",
        lambda: "This folder is a git checkout.",
    )
    with pytest.raises(UpdateError, match="git checkout"):
        update_stopped_install("10.0.0.8", 8090, shutdown=lambda: None)


def test_stopped_update_uses_the_saved_address(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "release_host", "10.4.5.6")
    monkeypatch.setattr(settings, "release_port", 8090)
    monkeypatch.setattr("src.self_update._dashboard_probe", lambda: ("127.0.0.1", 0))
    monkeypatch.setattr("src.self_update.checkout_block", lambda: "")
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        "src.self_update.save_release_target",
        lambda host, port: saved.update(host=host, port=port),
    )
    calls: list[str] = []

    def run(base, shutdown, spawn=None, report=None):
        del spawn
        calls.append(base)
        if report is not None:
            report("Downloading 0.9.80.")
        shutdown()

    monkeypatch.setattr("src.self_update.run_apply_job", run)
    lines: list[str] = []
    update_stopped_install(
        None,
        None,
        shutdown=lambda: calls.append("down"),
        report=lines.append,
    )
    assert calls == ["http://10.4.5.6:8090", "down"]
    assert "Update log:" not in "\n".join(lines)
    assert "release 10.4.5.6:8090" in lines
    assert lines[-1] == "Downloading 0.9.80."
    assert saved == {"host": "10.4.5.6", "port": 8090}


def test_cli_update_prints_a_stop_error(monkeypatch):
    from click.testing import CliRunner

    from cli import cli

    def refuse(*_args, **_kwargs):
        raise UpdateError("Yaver is still running.")

    monkeypatch.setattr("src.self_update.update_stopped_install", refuse)
    result = CliRunner().invoke(cli, ["--update"])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 1
    assert "Yaver is still running." in text


def test_cli_update_passes_the_address_and_closes(monkeypatch):
    from click.testing import CliRunner

    from cli import cli

    seen: dict[str, object] = {}

    def run(host, port, *, shutdown, report=None):
        seen["host"] = host
        seen["port"] = port
        if report is not None:
            report("Downloading 0.9.80.")
        shutdown()

    monkeypatch.setattr("src.self_update.update_stopped_install", run)
    result = CliRunner().invoke(cli, ["update", "--host", "10.2.3.4", "--port", "8090"])
    assert result.exit_code == 0
    assert seen == {"host": "10.2.3.4", "port": 8090}
    assert "Downloading 0.9.80." in result.output


def test_cli_update_is_quiet_when_the_install_is_current(monkeypatch):
    from click.testing import CliRunner

    from cli import cli

    def current(*_args, **_kwargs):
        raise UpdateError("This install is already 0.9.72.")

    monkeypatch.setattr("src.self_update.update_stopped_install", current)
    result = CliRunner().invoke(cli, ["update"])
    text = result.output + (result.stderr or "")
    assert result.exit_code == 0
    assert "already 0.9.72" in text


def test_powershell_helper_replaces_an_executable_and_keeps_env(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    env_path = install / ".env"
    env_path.write_bytes(b"TOKEN=keep\n")
    before = env_path.stat()
    env_path.chmod(stat.S_IREAD)
    staging = tmp_path / "staged"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    (staging / ".env").write_bytes(b"TOKEN=from-package\n")
    started = tmp_path / "started.txt"
    plan = {
        "created": 10**10,
        "pid": 0,
        "port": 0,
        "layout": "frozen",
        "staging": str(staging),
        "install_root": str(install),
        "argv": [
            str(powershell),
            "-NoProfile",
            "-Command",
            f"Set-Content -LiteralPath '{started}' -Value ok",
        ],
        "cwd": str(tmp_path),
        "version": "0.9.72",
        "log": str(tmp_path / "update.log"),
        "result": str(tmp_path / "result.json"),
        "health_seconds": 0,
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "src" / "update_helper.ps1"
    import subprocess

    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-PlanPath",
                str(plan_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
        assert env_path.read_bytes() == b"TOKEN=keep\n"
        assert env_path.stat().st_ino == before.st_ino
        assert env_path.stat().st_mode & stat.S_IWRITE == 0
    finally:
        env_path.chmod(stat.S_IWRITE)
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"
    assert not started.exists()
    assert "updated 0.9.72" in (tmp_path / "update.log").read_text(encoding="utf-8-sig")


def test_powershell_helper_does_not_launch_the_plan_command(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_bytes(b"TOKEN=keep\n")
    staging = tmp_path / "staged"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 9,
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [sys.executable, "-c", "import time; time.sleep(120)  # yaver-ps-rollback"],
                "cwd": str(install),
                "version": "0.9.72",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "src" / "update_helper.ps1"
    import subprocess

    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-PlanPath",
                str(plan_path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
        assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
        log = (tmp_path / "update.log").read_text(encoding="utf-8-sig")
        assert "updated 0.9.72" in log
        assert "starting restored " not in log
    finally:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*yaver-ps-rollback*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            check=False,
            timeout=20,
        )


def test_powershell_helper_keeps_a_data_folder_inside_the_install(tmp_path: Path):
    if sys.platform != "win32":
        pytest.skip("Windows helper")
    powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    if not powershell.is_file():
        pytest.skip("Windows PowerShell is not installed")
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    data = install / "office-data"
    (data / "yaver").mkdir(parents=True)
    (data / "yaver" / "keep.txt").write_text("kept", encoding="utf-8")
    (install / ".env").write_text(f"TOKEN=keep\nYAVER_BASE_DIR={data}\n", encoding="utf-8")
    staging = tmp_path / "staged"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    started = tmp_path / "started.txt"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "created": 10**10,
                "pid": 0,
                "port": 0,
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    str(powershell),
                    "-NoProfile",
                    "-Command",
                    f"Set-Content -LiteralPath '{started}' -Value ok",
                ],
                "cwd": str(tmp_path),
                "version": "0.9.72",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[1] / "src" / "update_helper.ps1"
    import subprocess

    completed = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-PlanPath",
            str(plan_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / "office-data" / "yaver" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert "TOKEN=keep" in (install / ".env").read_text(encoding="utf-8")
    assert not (tmp_path / "Yaver.userdata").exists()


def test_update_python_is_the_process_that_runs_the_script():
    """The helper must be the real interpreter, not a Windows launcher.

    A venv or ``py`` launcher stays running and starts Python as a child.
    Stopping that launcher stops the helper in the middle of the copy.
    """
    import subprocess

    host = _interpreter()
    proc = subprocess.Popen(
        [host, "-c", "import os; print(os.getpid())"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise
    assert proc.returncode == 0, out
    assert int(out.strip()) == proc.pid


def test_spawn_refuses_to_stay_inside_a_job_that_kills_its_members(tmp_path: Path, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows job breakaway")
    from src.self_update import _spawn

    calls: list[int] = []

    def popen(**kwargs):
        flags = int(kwargs.get("creationflags") or 0)
        calls.append(flags)
        if flags & 0x01000000:
            denied = OSError("access denied")
            denied.winerror = 5
            raise denied
        raise AssertionError("helper was started inside the job")

    monkeypatch.setattr("src.self_update.subprocess.Popen", popen)
    with pytest.raises(UpdateError, match="updater"):
        _spawn(["python"], tmp_path, tmp_path / "update.log")
    assert calls == [0x00000008 | 0x00000200 | 0x08000000 | 0x01000000]


def test_powershell_spawn_is_not_detached(tmp_path: Path, monkeypatch):
    """DETACHED_PROCESS makes powershell.exe exit before the script runs."""
    if sys.platform != "win32":
        pytest.skip("Windows PowerShell spawn")
    from src.self_update import _spawn

    calls: list[int] = []

    def popen(**kwargs):
        calls.append(int(kwargs.get("creationflags") or 0))
        return None

    monkeypatch.setattr("src.self_update.subprocess.Popen", popen)
    _spawn(
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile"],
        tmp_path,
        tmp_path / "update.log",
    )
    assert calls == [0x00000200 | 0x08000000 | 0x01000000]


def test_helper_replaces_the_install_after_killing_a_stuck_daemon(tmp_path: Path):
    """The helper must survive killing a daemon that does not exit.

    On Windows the helper is started with the venv ``python.exe``, which
    is a launcher. The real interpreter is that launcher's child, and the
    launcher is the daemon's child. Stopping the launcher, or stopping
    the daemon with ``taskkill /T``, stops the interpreter too. The new
    ``yaver.exe`` is then left half replaced or not replaced at all.
    """
    import subprocess
    import textwrap
    import time

    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "yaver.exe").write_text("old", encoding="utf-8")
    (install / ".env").write_bytes(b"BOARD=2\n")
    staging = tmp_path / "stage"
    (staging / "_internal").mkdir(parents=True)
    (staging / "yaver.exe").write_text("new", encoding="utf-8")
    result = tmp_path / "result.json"
    log = tmp_path / "update.log"
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "created": time.time(),
                "pid": 0,
                "port": 0,
                "layout": "frozen",
                "staging": str(staging),
                "install_root": str(install),
                "argv": [
                    sys.executable,
                    "-c",
                    "raise SystemExit(0)",
                ],
                "cwd": str(install),
                "version": "0.2.0",
                "log": str(log),
                "result": str(result),
                "health_seconds": 0,
                "wait_seconds": 0.4,
            }
        ),
        encoding="utf-8",
    )
    helper = Path(__file__).resolve().parents[1] / "src" / "update_helper.py"
    sleeper = tmp_path / "stuck_daemon.py"
    sleeper.write_text(
        textwrap.dedent(
            """\
            import json
            import os
            import subprocess
            import sys
            import time
            from pathlib import Path

            plan_path, helper, python = sys.argv[1:]
            plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            plan["pid"] = os.getpid()
            Path(plan_path).write_text(json.dumps(plan), encoding="utf-8")
            kwargs = {
                "cwd": str(Path(plan_path).parent),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if sys.platform == "win32":
                # Same flags as src.self_update._spawn, including job breakaway.
                kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([python, helper, "--apply", plan_path], **kwargs)
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )
    stuck = subprocess.Popen(
        [sys.executable, str(sleeper), str(plan_path), str(helper), sys.executable]
    )
    try:
        stuck.wait(timeout=12)
    except subprocess.TimeoutExpired:
        stuck.kill()
        stuck.wait(timeout=5)
        pytest.fail("the stuck daemon was still alive; the helper never killed it")
    deadline = time.time() + 8
    while time.time() < deadline and not result.is_file():
        time.sleep(0.1)
    body = (install / "yaver.exe").read_text(encoding="utf-8") if (install / "yaver.exe").is_file() else "<missing>"
    reported = result.read_text(encoding="utf-8") if result.is_file() else "<no result>"
    logged = log.read_text(encoding="utf-8") if log.is_file() else "<no log>"
    assert body == "new", (
        "helper did not replace the executable after the daemon stayed up. "
        f"file={body!r} result={reported!r} log={logged!r}"
    )
    assert (install / ".env").read_bytes() == b"BOARD=2\n"
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"
    assert json.loads(reported)["ok"] is True
