#!/usr/bin/env python3
"""Serve ops SPA (web/dist) and reverse-proxy /api + /ws to the backend daemon.

Offline Windows package has no Vite. Users who want a separate "frontend"
process run this on port 5173 (default) while start-backend.bat runs the API
on 8080. Same-origin /api and /ws work via proxy.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def _ensure_spa_mimetypes() -> None:
    """Force correct types for SPA assets (Windows registry often maps .js → text/plain)."""
    try:
        # Prefer shared helper when running from product tree
        from src.web_mimetypes import ensure_spa_mimetypes

        ensure_spa_mimetypes()
        return
    except Exception:
        pass
    # Standalone / import-path fallback (same overrides as src.web_mimetypes)
    overrides = {
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".cjs": "text/javascript",
        ".css": "text/css",
        ".wasm": "application/wasm",
        ".svg": "image/svg+xml",
        ".json": "application/json",
        ".map": "application/json",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".html": "text/html",
    }
    mimetypes.init()
    for ext, mime in overrides.items():
        mimetypes.add_type(mime, ext, strict=True)
        mimetypes.types_map[ext] = mime


def _media_type_for(path: Path) -> Optional[str]:
    try:
        from src.web_mimetypes import media_type_for_path

        return media_type_for_path(path)
    except Exception:
        _ensure_spa_mimetypes()
        return mimetypes.types_map.get(path.suffix.lower()) or mimetypes.guess_type(
            str(path)
        )[0]


def build_app(*, dist: Path, backend: str) -> FastAPI:
    # Must run before StaticFiles / FileResponse guess MIME types
    _ensure_spa_mimetypes()

    backend = backend.rstrip("/")
    app = FastAPI(title="VD Frontend", docs_url=None, redoc_url=None, openapi_url=None)

    assets = dist / "assets"
    if assets.is_dir():
        class _SpaStaticFiles(StaticFiles):
            """StaticFiles that never serves .js as text/plain (Windows MIME bug)."""

            def file_response(self, full_path, stat_result, scope, status_code=200):  # type: ignore[no-untyped-def]
                resp = super().file_response(
                    full_path, stat_result, scope, status_code=status_code
                )
                mt = _media_type_for(Path(str(full_path)))
                if mt:
                    # Starlette may have guessed text/plain from Windows registry
                    resp.headers["content-type"] = mt
                    if hasattr(resp, "media_type"):
                        resp.media_type = mt
                return resp

        app.mount(
            "/assets",
            _SpaStaticFiles(directory=str(assets)),
            name="assets",
        )

    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-encoding",
        "content-length",
    }

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_api(path: str, request: Request) -> Response:
        url = f"{backend}/api/{path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in hop_by_hop
        }
        body = await request.body()
        try:
            # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
            async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
                r = await client.request(
                    request.method,
                    url,
                    content=body if body else None,
                    headers=headers,
                )
        except httpx.ConnectError as e:
            msg = (
                f"Backend unreachable at {backend} ({e}). "
                "Start start-backend.bat first and leave the VD-Backend window open."
            )
            return Response(
                content=msg,
                status_code=502,
                media_type="text/plain; charset=utf-8",
            )
        except httpx.HTTPError as e:
            return Response(
                content=f"Proxy error talking to backend: {e}",
                status_code=502,
                media_type="text/plain; charset=utf-8",
            )
        out_headers = {
            k: v
            for k, v in r.headers.items()
            if k.lower() not in hop_by_hop
        }
        return Response(
            content=r.content,
            status_code=r.status_code,
            headers=out_headers,
            media_type=r.headers.get("content-type"),
        )

    @app.websocket("/ws")
    async def proxy_ws(client_ws: WebSocket) -> None:
        await client_ws.accept()
        ws_backend = (
            backend.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        )
        try:
            import websockets
        except ImportError:
            await client_ws.send_json(
                {"error": "websockets package missing; live updates unavailable"}
            )
            await client_ws.close()
            return

        try:
            async with websockets.connect(
                ws_backend,
                open_timeout=10,
                ping_interval=20,
            ) as server_ws:

                async def client_to_server() -> None:
                    try:
                        while True:
                            msg = await client_ws.receive()
                            if msg.get("type") == "websocket.disconnect":
                                break
                            if "text" in msg and msg["text"] is not None:
                                await server_ws.send(msg["text"])
                            elif "bytes" in msg and msg["bytes"] is not None:
                                await server_ws.send(msg["bytes"])
                    except WebSocketDisconnect:
                        pass
                    except Exception:
                        pass

                async def server_to_client() -> None:
                    try:
                        async for message in server_ws:
                            if isinstance(message, bytes):
                                await client_ws.send_bytes(message)
                            else:
                                await client_ws.send_text(message)
                    except Exception:
                        pass

                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(client_to_server()),
                        asyncio.create_task(server_to_client()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
        except Exception as e:
            try:
                await client_ws.send_json({"error": f"ws proxy: {e}"})
            except Exception:
                pass
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass

    def _spa_index() -> FileResponse:
        return FileResponse(
            dist / "index.html",
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    def _file_response(path: Path) -> FileResponse:
        mt = _media_type_for(path)
        kwargs = {
            "path": path,
            "headers": {
                "Cache-Control": "public, max-age=31536000, immutable"
                if path.suffix.lower() in {".js", ".css", ".woff", ".woff2"}
                and "index-" in path.name
                else "no-cache",
            },
        }
        if mt:
            kwargs["media_type"] = mt
        return FileResponse(**kwargs)

    @app.get("/")
    def index() -> FileResponse:
        return _spa_index()

    @app.get("/{full_path:path}")
    def spa_or_file(full_path: str) -> FileResponse:
        low = (full_path or "").lstrip("/").lower()
        if low.startswith("api/") or low == "ws" or low.startswith("ws/"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Not found")
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            return _spa_index()
        if candidate.is_file():
            return _file_response(candidate)
        return _spa_index()

    return app



def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="VD offline frontend (SPA + API proxy)")
    p.add_argument(
        "--dist",
        default=_env("VD_WEB_DIST", str(Path.cwd() / "web" / "dist")),
        help="Path to web/dist",
    )
    p.add_argument(
        "--backend",
        default=_env("VD_BACKEND_URL", "http://127.0.0.1:8080"),
        help="Backend base URL (daemon)",
    )
    p.add_argument(
        "--host",
        default=_env("VD_FRONTEND_HOST", "0.0.0.0"),
        help="Bind host (default 0.0.0.0)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(_env("VD_FRONTEND_PORT", "5173") or "5173"),
        help="Bind port (default 5173)",
    )
    args = p.parse_args(argv)

    dist = Path(args.dist).expanduser().resolve()
    if not (dist / "index.html").is_file():
        print(f"[ERROR] SPA missing: {dist / 'index.html'}", file=sys.stderr)
        print("Run install from a CI zip that includes web\\dist, or npm run build.", file=sys.stderr)
        return 1

    app = build_app(dist=dist, backend=args.backend)
    print("=" * 50)
    print("  Virtual Developer - Frontend")
    print("=" * 50)
    print(f"SPA     : {dist}")
    print(f"Backend : {args.backend}")
    print(f"Listen  : http://{args.host}:{args.port}/")
    print(f"Local   : http://127.0.0.1:{args.port}/")
    print("Proxies /api/* and /ws to the backend.")
    print("=" * 50)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
