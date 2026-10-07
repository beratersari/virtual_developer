"""Operator update.bat and update.sh against a local release server."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_SCRIPT = ROOT / "packaging" / "windows" / "update.bat"
LINUX_SCRIPT = ROOT / "packaging" / "linux" / "update.sh"
BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
SECRET = "super-secret-value"
PACKAGE_SECRET = "from-package-token"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        self.server.hits.append(parsed.path)
        if self.server.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1/nope")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path == "/api/latest":
            body = json.dumps(
                {
                    "version": self.server.version,
                    "sha256": self.server.sha,
                    "notes": "ship yaver.exe",
                }
            ).encode("utf-8")
            self._send(200, body)
            return
        if parsed.path == self.server.download_path:
            self._send(200, self.server.blob)
            return
        self._send(404, b"missing")

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _serve(blob: bytes, sha: str, version: str, download_path: str, mode: str = "ok"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.blob = blob
    server.sha = sha
    server.version = version
    server.download_path = download_path
    server.hits = []
    server.mode = mode
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _zip(files: dict[str, bytes], *, bad_name: str | None = None) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if bad_name is not None:
            archive.writestr(zipfile.ZipInfo(bad_name), b"evil")
    return buffer.getvalue()


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=120,
    )
    text = (result.stdout or b"").decode("utf-8", errors="replace")
    text += (result.stderr or b"").decode("utf-8", errors="replace")
    return result.returncode, text


def _windows_install(tmp_path: Path, port: int) -> Path:
    install = tmp_path / "Yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "_internal" / "engine.txt").write_bytes(b"old-engine")
    (install / "_internal" / "stale.txt").write_bytes(b"stale")
    (install / "yaver.exe").write_bytes(b"old-exe")
    (install / ".env.example").write_bytes(b"EXAMPLE=old\n")
    (install / ".env").write_bytes(
        f"RELEASE_HOST=127.0.0.1\r\nRELEASE_PORT={port}\r\nTOKEN={SECRET}\r\n".encode()
    )
    agents = install / "opencoderman" / "agents"
    agents.mkdir(parents=True)
    (agents / "derman-build.md").write_bytes(b"old-agent\n")
    (install / "install-agents.bat").write_bytes(b"@echo off\r\nrem old\r\n")
    (install / "VERSION").write_bytes(b"0.9.74\n")
    (install / "update.bat").write_bytes(WINDOWS_SCRIPT.read_bytes())
    (install / "notes").mkdir()
    (install / "notes" / "keep.txt").write_bytes(b"keep-me\n")
    return install


def _windows_package() -> dict[str, bytes]:
    return {
        "bundle/yaver.exe": b"new-exe",
        "bundle/_internal/engine.txt": b"new-engine",
        "bundle/.env.example": b"EXAMPLE=new\n",
        "bundle/.env": f"TOKEN={PACKAGE_SECRET}\n".encode(),
        "bundle/opencoderman/agents/derman-build.md": b"new-agent\n",
        "bundle/install-agents.bat": b"@echo off\r\nrem new\r\n",
        "bundle/VERSION": b"0.9.80\n",
        "bundle/update.bat": b"@echo off\r\nrem package script must stay out\r\n",
        "bundle/START_HERE.txt": b"new start\n",
    }


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_replaces_program_files_and_keeps_env(tmp_path: Path):
    blob = _zip(_windows_package())
    sha = hashlib.sha256(blob).hexdigest()
    server = _serve(blob, sha, "0.9.80", "/download/windows")
    try:
        port = int(server.server_address[1])
        install = _windows_install(tmp_path, port)
        other = tmp_path / "other"
        other.mkdir()
        decoy_exe = other / "yaver.exe"
        decoy = None
        try:
            import shutil

            shutil.copy2(r"C:\Windows\System32\cmd.exe", decoy_exe)
            decoy = subprocess.Popen(
                [str(decoy_exe), "/c", "ping", "-n", "80", "127.0.0.1"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            decoy = None
        decoy_alive = decoy is not None and decoy.poll() is None
        env_before = (install / ".env").read_bytes()
        script_before = (install / "update.bat").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code == 0, text
        assert text.startswith("Release server 127.0.0.1:") or "Release server 127.0.0.1:" in text
        assert "Updated to 0.9.80. Start yaver.exe when you want." in text
        assert SECRET not in text
        assert PACKAGE_SECRET not in text
        assert (install / "yaver.exe").read_bytes() == b"new-exe"
        assert (install / "_internal" / "engine.txt").read_bytes() == b"new-engine"
        assert not (install / "_internal" / "stale.txt").exists()
        assert (install / ".env.example").read_bytes() == b"EXAMPLE=new\n"
        assert (install / ".env").read_bytes() == env_before
        assert (install / "opencoderman" / "agents" / "derman-build.md").read_bytes() == b"new-agent\n"
        assert (install / "install-agents.bat").read_bytes() == b"@echo off\r\nrem new\r\n"
        assert (install / "VERSION").read_bytes() == b"0.9.80\n"
        assert (install / "START_HERE.txt").read_bytes() == b"new start\n"
        assert (install / "update.bat").read_bytes() == script_before
        assert (install / "notes" / "keep.txt").read_bytes() == b"keep-me\n"
        assert "/download/windows" in server.hits
        if decoy_alive:
            assert decoy is not None
            assert decoy.poll() is None, text
    finally:
        server.shutdown()
        if "decoy" in locals() and decoy is not None and decoy.poll() is None:
            decoy.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_writes_the_published_version(tmp_path: Path):
    files = _windows_package()
    files["bundle/VERSION"] = b"0.9.74\n"
    files["bundle/_internal/VERSION"] = b"0.9.74\n"
    files["bundle/_internal/atlassian/VERSION"] = b"5.0.5"
    files["bundle/_internal/marker.txt"] = b"new-text"
    blob = _zip(files)
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/windows")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        (install / "_internal" / "VERSION").write_bytes(b"0.9.74\n")
        (install / "_internal" / "atlassian").mkdir()
        (install / "_internal" / "atlassian" / "VERSION").write_bytes(b"5.0.5")
        (install / "_internal" / "marker.txt").write_bytes(b"old-text")
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code == 0, text
        assert "Updated to 0.9.80." in text
        assert (install / "VERSION").read_bytes() == b"0.9.80\n"
        assert (install / "_internal" / "VERSION").read_bytes() == b"0.9.80\n"
        assert (install / "_internal" / "atlassian" / "VERSION").read_bytes() == b"5.0.5"
        assert (install / "_internal" / "marker.txt").read_bytes() == b"new-text"
        assert not (install / "_internal" / "stale.txt").exists()
        assert [path.name for path in install.iterdir() if path.name.startswith(".yaver-hold-")] == []
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_leaves_files_when_the_checksum_mismatches(tmp_path: Path):
    blob = _zip(_windows_package())
    server = _serve(blob, "a" * 64, "0.9.80", "/download/windows")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        before = (install / "yaver.exe").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code != 0, text
        assert "checksum" in text.lower()
        assert (install / "yaver.exe").read_bytes() == before
        assert (install / "_internal" / "stale.txt").read_bytes() == b"stale"
        assert SECRET not in text
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_leaves_files_when_the_server_is_down(tmp_path: Path):
    install = _windows_install(tmp_path, _closed_port())
    before = (install / "yaver.exe").read_bytes()
    code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
    assert code != 0, text
    assert "Could not reach the release server." in text
    assert (install / "yaver.exe").read_bytes() == before
    assert SECRET not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_refuses_a_git_checkout_before_download(tmp_path: Path):
    blob = _zip(_windows_package())
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/windows")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        (install / ".git").mkdir()
        before = (install / "yaver.exe").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code != 0, text
        assert "git checkout" in text
        assert server.hits == []
        assert (install / "yaver.exe").read_bytes() == before
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_skips_a_current_install(tmp_path: Path):
    blob = _zip(_windows_package())
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/windows")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        (install / "VERSION").write_bytes(b"0.9.80+local\n")
        before = (install / "yaver.exe").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code == 0, text
        assert "This install is already 0.9.80." in text
        assert "/download/windows" not in server.hits
        assert (install / "yaver.exe").read_bytes() == before
        assert (install / "VERSION").read_bytes() == b"0.9.80+local\n"
        assert "Stopping Yaver." not in text
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_refuses_an_unsafe_path(tmp_path: Path):
    blob = _zip(_windows_package(), bad_name="../evil.txt")
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/windows")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        before = (install / "yaver.exe").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code != 0, text
        assert "unsafe path" in text
        assert (install / "yaver.exe").read_bytes() == before
        assert not (tmp_path / "evil.txt").exists()
    finally:
        server.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows update.bat")
def test_update_bat_does_not_follow_a_redirect(tmp_path: Path):
    blob = _zip(_windows_package())
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/windows", mode="redirect")
    try:
        install = _windows_install(tmp_path, int(server.server_address[1]))
        before = (install / "yaver.exe").read_bytes()
        code, text = _run(["cmd.exe", "/d", "/c", str(install / "update.bat")], install)
        assert code != 0, text
        assert (install / "yaver.exe").read_bytes() == before
        assert "/download/windows" not in server.hits
    finally:
        server.shutdown()


def test_update_scripts_avoid_powershell_automatic_names():
    bat = WINDOWS_SCRIPT.read_text(encoding="utf-8")
    assert not bat.startswith("\ufeff")
    body = bat[bat.rfind("YAVER_UPDATE_BODY") + len("YAVER_UPDATE_BODY") :]
    for bad in ("$pid", "$PID", "$args", "$host", "$Host", "taskkill"):
        assert bad not in body


def test_update_bat_copies_directories_with_robocopy():
    bat = WINDOWS_SCRIPT.read_text(encoding="utf-8")
    body = bat[bat.rfind("YAVER_UPDATE_BODY") + len("YAVER_UPDATE_BODY") :]
    assert "robocopy.exe" in body
    assert "Copy-Item -LiteralPath $child.FullName -Destination $dest -Recurse -Force" not in body
    raw = LINUX_SCRIPT.read_bytes()
    assert b"\r" not in raw
    assert b"$'" not in raw


def _linux_ready() -> bool:
    if not BASH.is_file():
        return False
    probe = subprocess.run(
        [str(BASH), "-lc", "command -v unzip && command -v sha256sum && command -v curl && command -v awk"],
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


def _linux_install(tmp_path: Path, port: int) -> Path:
    install = tmp_path / "yaver"
    (install / "_internal").mkdir(parents=True)
    (install / "_internal" / "engine.txt").write_bytes(b"old-engine")
    (install / "_internal" / "stale.txt").write_bytes(b"stale")
    (install / "yaver").write_bytes(b"old-exe")
    (install / ".env.example").write_bytes(b"EXAMPLE=old\n")
    (install / ".env").write_bytes(
        f"RELEASE_HOST=127.0.0.1\nRELEASE_PORT={port}\nTOKEN={SECRET}\n".encode()
    )
    agents = install / "opencoderman" / "agents"
    agents.mkdir(parents=True)
    (agents / "derman-build.md").write_bytes(b"old-agent\n")
    (install / "install-agents.sh").write_bytes(b"#!/bin/sh\necho old\n")
    (install / "VERSION").write_bytes(b"0.9.74\n")
    (install / "update.sh").write_bytes(LINUX_SCRIPT.read_bytes())
    (install / "notes").mkdir()
    (install / "notes" / "keep.txt").write_bytes(b"keep-me\n")
    return install


def _linux_package() -> dict[str, bytes]:
    return {
        "yaver": b"new-exe",
        "_internal/engine.txt": b"new-engine",
        ".env.example": b"EXAMPLE=new\n",
        ".env": f"TOKEN={PACKAGE_SECRET}\n".encode(),
        "opencoderman/agents/derman-build.md": b"new-agent\n",
        "install-agents.sh": b"#!/bin/sh\necho new\n",
        "VERSION": b"0.9.80\n",
        "update.sh": b"#!/bin/sh\necho package-script\n",
    }


def _run_sh(install: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["YAVER_UPDATE_PLATFORM"] = "ubuntu-22.04"
    env["MSYS_NO_PATHCONV"] = "1"
    return _run([str(BASH), str(install / "update.sh")], install, env)


@pytest.mark.skipif(not _linux_ready(), reason="Git bash with unzip, sha256sum, and curl")
def test_update_sh_replaces_program_files_and_keeps_env(tmp_path: Path):
    blob = _zip(_linux_package())
    sha = hashlib.sha256(blob).hexdigest()
    server = _serve(blob, sha, "0.9.80", "/download/ubuntu-22.04")
    try:
        install = _linux_install(tmp_path, int(server.server_address[1]))
        env_before = (install / ".env").read_bytes()
        script_before = (install / "update.sh").read_bytes()
        code, text = _run_sh(install)
        assert code == 0, text
        assert "Updated to 0.9.80. Start ./yaver when you want." in text
        assert SECRET not in text
        assert PACKAGE_SECRET not in text
        assert (install / "yaver").read_bytes() == b"new-exe"
        assert (install / "_internal" / "engine.txt").read_bytes() == b"new-engine"
        assert not (install / "_internal" / "stale.txt").exists()
        assert (install / ".env.example").read_bytes() == b"EXAMPLE=new\n"
        assert (install / ".env").read_bytes() == env_before
        assert (install / "opencoderman" / "agents" / "derman-build.md").read_bytes() == b"new-agent\n"
        assert (install / "install-agents.sh").read_bytes() == b"#!/bin/sh\necho new\n"
        assert (install / "VERSION").read_bytes() == b"0.9.80\n"
        assert (install / "update.sh").read_bytes() == script_before
        assert (install / "notes" / "keep.txt").read_bytes() == b"keep-me\n"
        assert "/download/ubuntu-22.04" in server.hits
    finally:
        server.shutdown()


@pytest.mark.skipif(not _linux_ready(), reason="Git bash with unzip, sha256sum, and curl")
def test_update_sh_leaves_files_when_the_checksum_mismatches(tmp_path: Path):
    blob = _zip(_linux_package())
    server = _serve(blob, "b" * 64, "0.9.80", "/download/ubuntu-22.04")
    try:
        install = _linux_install(tmp_path, int(server.server_address[1]))
        before = (install / "yaver").read_bytes()
        code, text = _run_sh(install)
        assert code != 0, text
        assert "checksum" in text.lower()
        assert (install / "yaver").read_bytes() == before
        assert (install / "_internal" / "stale.txt").read_bytes() == b"stale"
        assert SECRET not in text
    finally:
        server.shutdown()


@pytest.mark.skipif(not _linux_ready(), reason="Git bash with unzip, sha256sum, and curl")
def test_update_sh_skips_a_current_install(tmp_path: Path):
    blob = _zip(_linux_package())
    server = _serve(blob, hashlib.sha256(blob).hexdigest(), "0.9.80", "/download/ubuntu-22.04")
    try:
        install = _linux_install(tmp_path, int(server.server_address[1]))
        (install / "VERSION").write_bytes(b"0.9.80+local\n")
        before = (install / "yaver").read_bytes()
        code, text = _run_sh(install)
        assert code == 0, text
        assert "This install is already 0.9.80." in text
        assert "/download/ubuntu-22.04" not in server.hits
        assert (install / "yaver").read_bytes() == before
    finally:
        server.shutdown()
