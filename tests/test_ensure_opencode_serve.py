"""Launcher contract: start-backend ensures OpenCode serve without killing it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / "packaging" / "windows"


def test_ensure_opencode_serve_script_is_safe_sibling():
    ps1 = (WIN / "Ensure-OpencodeServe.ps1").read_text(encoding="utf-8")
    assert "Test-ServeHealthy" in ps1
    assert "Test-PortListening" in ps1
    assert "VD-OpenCode-Serve" in ps1
    assert "global/health" in ps1
    assert "-KillDaemon" not in ps1
    assert "src.daemon" not in ps1
    # PS 5.1: do not name a local $pid
    assert "$pid =" not in ps1.lower()
    assert all(ord(ch) < 128 for ch in ps1)


def test_start_backend_calls_ensure_before_daemon():
    bat = (WIN / "start-backend.bat").read_text(encoding="utf-8")
    ensure_at = bat.lower().find("ensure-opencodeserve.ps1")
    stop_at = bat.lower().find("stop-vdprocesses.ps1")
    daemon_at = bat.lower().find("-m src.daemon")
    assert ensure_at > 0
    assert stop_at > ensure_at
    assert daemon_at > stop_at
    assert "-KillDaemon" in bat
    assert "does not stop serve" in bat.lower() or "does not stop serve or frontend" in bat.lower()


def test_start_all_mentions_serve_window():
    bat = (WIN / "start.bat").read_text(encoding="utf-8")
    assert "4096" in bat
    assert "VD-OpenCode-Serve" in bat
