"""Tests for SPA MIME overrides (Windows .js → text/plain fix)."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from src.web_mimetypes import ensure_spa_mimetypes, media_type_for_path


def test_ensure_spa_mimetypes_overrides_text_plain_js():
    mimetypes.init()
    mimetypes.types_map[".js"] = "text/plain"
    ensure_spa_mimetypes()
    assert mimetypes.types_map[".js"] == "text/javascript"
    assert "javascript" in (media_type_for_path(Path("assets/index-abc.js")) or "")


def test_media_type_for_css_and_wasm():
    ensure_spa_mimetypes()
    assert media_type_for_path("x.css") == "text/css"
    assert media_type_for_path("x.wasm") == "application/wasm"
