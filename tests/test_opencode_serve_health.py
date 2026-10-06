"""Sidebar OpenCode serve health."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def test_sidebar_health_returns_the_serve_status(monkeypatch):
    from src.opencode_serve_supervisor import supervisor

    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: {
            "status": "deferred",
            "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
        },
    )
    monkeypatch.setattr(
        "src.opencode_serve_supervisor.probe_healthy",
        lambda url="": False,
    )
    client = TestClient(create_dashboard_app())
    response = client.get("/api/opencode-serve")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "deferred",
        "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
        "healthy": False,
    }


def test_sidebar_health_reports_a_healthy_serve_that_is_waiting_to_reload(monkeypatch):
    from src.opencode_serve_supervisor import supervisor

    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: {
            "status": "deferred",
            "message": "Agents are saved. OpenCode will reload when KAN-1 finishes.",
        },
    )
    monkeypatch.setattr(
        "src.opencode_serve_supervisor.probe_healthy",
        lambda url="": True,
    )
    client = TestClient(create_dashboard_app())
    body = client.get("/api/opencode-serve").json()
    assert body["status"] == "deferred"
    assert body["healthy"] is True


def test_sidebar_health_does_not_report_a_failed_read_as_healthy(monkeypatch):
    from src.opencode_serve_supervisor import supervisor

    def boom():
        raise RuntimeError("status locked")

    monkeypatch.setattr(supervisor, "status", boom)
    client = TestClient(create_dashboard_app())
    response = client.get("/api/opencode-serve")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["status"] != "ready"
    assert body["healthy"] is False
    assert "unavailable" in body["message"].lower()


def test_sidebar_health_rejects_a_status_that_is_not_a_report(monkeypatch):
    from src.opencode_serve_supervisor import supervisor

    monkeypatch.setattr(supervisor, "status", lambda: "ready")
    client = TestClient(create_dashboard_app())
    body = client.get("/api/opencode-serve").json()
    assert body["status"] == "unavailable"
    assert body["healthy"] is False
    assert "unavailable" in body["message"].lower()


def test_sidebar_shows_serve_health_at_the_bottom_left():
    shell = (WEB / "src" / "app" / "Shell.tsx").read_text(encoding="utf-8")
    brand = shell.split('<div className="vd-brand">', 1)[1].split(
        '<nav className="vd-nav"', 1
    )[0]
    assert "<ServeHealth />" not in brand
    footer = shell.split("</nav>", 1)[1].split("</aside>", 1)[0]
    assert "<ServeHealth />" in footer
    assert footer.index("localClock") < footer.index("<ServeHealth />")
    assert footer.index("<ServeHealth />") < footer.index("Connected")
    client = (WEB / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    fetch = client.split("export function fetchOpencodeServe", 1)[1].split(
        "export function", 1
    )[0]
    assert "/api/opencode-serve" in fetch
    assert "slot: 'background'" in fetch
    panel = (WEB / "src" / "app" / "ServeHealth.tsx").read_text(encoding="utf-8")
    assert "setInterval(tick, 4000)" in panel


def test_serve_health_labels():
    npx = shutil.which("npx")
    if not npx:
        pytest.skip("npx is required to run the serve health label check")
    proc = subprocess.run(
        [npx, "--yes", "tsx", "src/app/serveHealth.test.ts"],
        cwd=WEB,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        shell=sys.platform == "win32",
    )
    assert proc.returncode == 0, (
        f"serve health labels failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
