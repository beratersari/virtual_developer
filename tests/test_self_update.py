"""LAN release check, zip apply, and the restart helper."""

from __future__ import annotations

import io
import json
import sys
import threading
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
    platform_from_os,
    release_base_url,
    run_apply_job,
    start_apply,
    versions_equal,
)
from src.update_helper import (
    UpdateError,
    apply_frozen_tree,
    apply_source_tree,
    assert_install_dir,
    classify_members,
    run_plan,
    safe_extract,
)


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


def test_source_apply_keeps_env_and_venv(tmp_path: Path):
    install = tmp_path / "app"
    (install / "src").mkdir(parents=True)
    (install / "src" / "old.py").write_text("old", encoding="utf-8")
    (install / "src" / "keep.py").write_text("old-keep", encoding="utf-8")
    (install / ".env").write_bytes(b"JIRA_HOST=https://jira\n")
    (install / ".venv").mkdir()
    (install / ".venv" / "pyvenv.cfg").write_text("home = here\n", encoding="utf-8")
    (install / "notes.txt").write_text("leave me", encoding="utf-8")
    staging = tmp_path / "stage"
    (staging / "src").mkdir(parents=True)
    (staging / "src" / "keep.py").write_text("new-keep", encoding="utf-8")
    (staging / "VERSION").write_text("0.9.72\n", encoding="utf-8")
    (staging / ".env").write_text("JIRA_HOST=replaced\n", encoding="utf-8")
    apply_source_tree(staging, install)
    assert (install / ".env").read_bytes() == b"JIRA_HOST=https://jira\n"
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
    apply_frozen_tree(staging, install)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "new"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"


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


def test_helper_starts_the_new_tree_and_keeps_env(tmp_path: Path):
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
    for _ in range(50):
        if started.is_file():
            break
        import time

        time.sleep(0.1)
    assert started.read_text(encoding="utf-8") == "yes"
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert result["version"] == "0.2.0"


def test_failed_executable_start_restores_the_previous_tree(tmp_path: Path):
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
    with pytest.raises(UpdateError):
        run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "old"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["ok"] is False


def test_health_failure_stops_the_process_that_locks_the_install(tmp_path: Path):
    """The new process starts in the install folder. Rollback has to stop it."""
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
                "argv": [sys.executable, "-c", "import time; time.sleep(120)"],
                "cwd": str(install),
                "version": "0.2.0",
                "log": str(tmp_path / "update.log"),
                "result": str(tmp_path / "result.json"),
                "health_seconds": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError, match="did not open"):
        run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "old"
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"


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


def test_health_failure_puts_an_inside_data_folder_back(tmp_path: Path):
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
    with pytest.raises(UpdateError, match="did not open"):
        run_plan(plan)
    assert (install / "yaver.exe").read_text(encoding="utf-8") == "old"
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


def test_update_routes_report_status_and_refuse_a_checkout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.self_update._work_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "src.self_update.save_release_target",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("saved")),
    )
    app = create_dashboard_app()
    client = TestClient(app)
    status = client.get("/api/update")
    assert status.status_code == 200
    body = status.json()
    assert body["current_version"]
    assert "release_host" in body
    empty = client.post("/api/update/check", json={"release_host": "", "release_port": 0})
    assert empty.status_code == 400
    monkeypatch.setattr(
        "src.self_update.checkout_block",
        lambda: "This folder is a git checkout.",
    )
    blocked = client.post(
        "/api/update/apply",
        json={"release_host": "10.0.0.8", "release_port": 8090},
    )
    assert blocked.status_code == 400
    assert "git checkout" in blocked.json()["detail"]


def test_powershell_helper_replaces_an_executable_and_keeps_env(tmp_path: Path):
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
    assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
    assert (tmp_path / "Yaver.previous" / "yaver.exe").read_text(encoding="utf-8") == "old"
    for _ in range(50):
        if started.is_file():
            break
        import time

        time.sleep(0.1)
    assert started.read_text(encoding="utf-8").strip() == "ok"


def test_powershell_helper_restores_when_the_new_process_locks_the_folder(tmp_path: Path):
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
        assert completed.returncode != 0, completed.stdout + completed.stderr
        assert (install / "yaver.exe").read_text(encoding="utf-8") == "old"
        assert (install / ".env").read_bytes() == b"TOKEN=keep\n"
        log = (tmp_path / "update.log").read_text(encoding="utf-8-sig")
        assert "restored" in log.lower()
        assert "starting restored " in log
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
