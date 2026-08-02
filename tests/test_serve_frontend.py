"""Tests for offline frontend proxy (packaging/windows/serve_frontend.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient


def _load_serve_frontend():
    path = (
        Path(__file__).resolve().parents[1]
        / "packaging"
        / "windows"
        / "serve_frontend.py"
    )
    spec = importlib.util.spec_from_file_location("serve_frontend", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    (d / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    assets = d / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
    return d


def test_spa_index_served(dist: Path):
    mod = _load_serve_frontend()
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:8080")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="root"' in r.text


def test_proxy_api_returns_502_when_backend_down(dist: Path):
    mod = _load_serve_frontend()
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:9")
    client = TestClient(app)

    class BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError("refused")

    with patch("httpx.AsyncClient", return_value=BoomClient()):
        r = client.get("/api/meta")
    assert r.status_code == 502
    assert "Backend unreachable" in r.text
    assert "start-backend" in r.text.lower() or "Backend" in r.text


def test_proxy_api_forwards_when_backend_ok(dist: Path):
    mod = _load_serve_frontend()
    app = mod.build_app(dist=dist, backend="http://127.0.0.1:8080")
    client = TestClient(app)

    mock_resp = MagicMock()
    mock_resp.content = b'{"version":"test"}'
    mock_resp.status_code = 200
    mock_resp.headers = {"content-type": "application/json"}

    class OkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, *a, **k):
            return mock_resp

    with patch("httpx.AsyncClient", return_value=OkClient()):
        r = client.get("/api/meta")
    assert r.status_code == 200
    assert b"version" in r.content
