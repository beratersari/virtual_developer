"""Dashboard release banner: compare versions and explain the update script."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from src.config import settings
from src.dashboard.release_notice import (
    clear_release_notice_cache,
    detect_update_platform,
    peek_release_notice,
    refresh_release_notice,
    version_is_newer,
    version_key,
)
from src.dashboard.service import build_live_envelope


@pytest.fixture(autouse=True)
def _quiet_cache():
    clear_release_notice_cache()
    yield
    clear_release_notice_cache()


def test_version_order_matches_release_numbers():
    assert version_key("0.9.72") > version_key("0.9.9")
    assert version_is_newer("0.9.81", "0.9.80")
    assert version_is_newer("0.10.0", "0.9.99")
    assert not version_is_newer("0.9.80", "0.9.80")
    assert not version_is_newer("0.9.80+g1", "0.9.80")
    assert not version_is_newer("0.9.80-dev", "0.9.80")
    assert not version_is_newer("0.9.79", "0.9.80")


def test_platform_follows_the_update_script(monkeypatch):
    monkeypatch.delenv("YAVER_UPDATE_PLATFORM", raising=False)
    assert detect_update_platform(system="nt") == "windows"
    ubuntu = 'ID=ubuntu\nVERSION_ID="22.04"\n'
    assert detect_update_platform(system="posix", os_release_text=ubuntu) == "ubuntu-22.04"
    assert detect_update_platform(system="posix", os_release_text='ID=debian\nVERSION_ID="12"\n') == ""
    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "ubuntu-24.04")
    assert detect_update_platform(system="nt") == "ubuntu-24.04"
    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "macos")
    assert detect_update_platform(system="nt") == ""


def test_newer_package_is_announced_without_a_download(monkeypatch):
    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "windows")
    monkeypatch.setattr(settings, "release_host", "10.1.2.3")
    monkeypatch.setattr(settings, "release_port", 0)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"version": "0.9.81", "sha256": "abc"})

    notice = refresh_release_notice(
        current="0.9.80",
        transport=httpx.MockTransport(handler),
    )

    assert notice["available"] is True
    assert notice["latest"] == "0.9.81"
    assert notice["current"] == "0.9.80"
    assert "Yaver 0.9.81 is available" in notice["message"]
    assert any("update.bat" in step for step in notice["steps"])
    assert all("download" not in step.lower() or "download the zip once" in step.lower() for step in notice["steps"])
    assert seen[0].url.scheme == "http"
    assert seen[0].url.host == "10.1.2.3"
    assert seen[0].url.port == 8090
    assert seen[0].url.params["platform"] == "windows"
    assert peek_release_notice()["available"] is True


def test_redirect_and_same_version_stay_quiet(monkeypatch):
    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "ubuntu-22.04")
    monkeypatch.setattr(settings, "release_host", "10.1.2.3")
    monkeypatch.setattr(settings, "release_port", 8090)

    def redirected(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.test/api/latest"})

    notice = refresh_release_notice(
        current="0.9.80",
        transport=httpx.MockTransport(redirected),
    )
    assert notice["available"] is False
    assert notice["steps"] == []

    def same(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "0.9.80"})

    notice = refresh_release_notice(
        current="0.9.80",
        transport=httpx.MockTransport(same),
    )
    assert notice["available"] is False


def test_unsafe_host_is_not_requested(monkeypatch):
    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "windows")
    monkeypatch.setattr(settings, "release_host", "http://evil.test/path")
    monkeypatch.setattr(settings, "release_port", 8090)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"requested {request.url}")

    notice = refresh_release_notice(
        current="0.9.80",
        transport=httpx.MockTransport(handler),
    )
    assert notice["available"] is False


def test_live_feed_includes_the_cached_notice(monkeypatch, tmp_path):
    from src.state.manager import JiraStateManager

    monkeypatch.setenv("YAVER_UPDATE_PLATFORM", "windows")
    monkeypatch.setattr(settings, "release_host", "10.1.2.3")
    monkeypatch.setattr(settings, "release_port", 8090)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "1.2.3"})

    refresh_release_notice(current="0.9.80", transport=httpx.MockTransport(handler))
    sm = JiraStateManager(state_dir=tmp_path / "state")
    env = build_live_envelope(state_manager=sm, processor=None)
    assert env["release"]["available"] is True
    assert env["release"]["latest"] == "1.2.3"
    assert "tasks" not in env


def test_update_banner_renders():
    web = Path(__file__).resolve().parents[1] / "web"
    page = (web / "src/app/Shell.tsx").read_text(encoding="utf-8")
    assert "UpdateBanner" in page
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to render the update banner")
    proc = subprocess.run(
        [
            npx,
            "--yes",
            "tsx",
            "--tsconfig",
            "tsconfig.app.json",
            "src/app/updateBanner.test.tsx",
        ],
        cwd=web,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "updateBanner ok" in proc.stdout
