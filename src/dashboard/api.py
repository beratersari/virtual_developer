"""FastAPI app for the ops dashboard (REST + WebSocket)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.service import (
    apply_settings_update,
    build_dashboard_payload,
    build_jobs,
    build_meta,
    build_poll_status,
    build_settings_view,
    build_task_detail,
    build_tasks,
)
from src.state.job_store import job_store
from src.dashboard.snapshot import poll_snapshot_store
from src.logger import logger
from src.state.manager import JiraStateManager

if TYPE_CHECKING:
    from src.processor import JobProcessor


def _static_dir() -> Optional[Path]:
    root = Path(__file__).resolve().parents[2] / "web" / "dist"
    if root.is_dir() and (root / "index.html").is_file():
        return root
    return None


def create_dashboard_app(
    *,
    processor: Optional["JobProcessor"] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> FastAPI:
    """Create dashboard FastAPI application bound to daemon services."""
    sm = state_manager or JiraStateManager()
    app = FastAPI(title="JIRA Virtual Developer Dashboard", version="1.0.0")
    app.state.processor = processor
    app.state.state_manager = sm

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _payload() -> dict:
        return build_dashboard_payload(
            state_manager=app.state.state_manager,
            processor=app.state.processor,
            store=poll_snapshot_store,
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": build_meta().version}

    @app.get("/api/meta")
    def meta() -> dict:
        return build_meta().model_dump()

    @app.get("/api/tasks")
    def tasks() -> dict:
        return build_tasks(app.state.state_manager, app.state.processor).model_dump()

    @app.get("/api/jobs")
    def jobs(issue_key: Optional[str] = None, limit: int = 200) -> dict:
        return build_jobs(
            issue_key=issue_key,
            limit=limit,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
        ).model_dump()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        job = job_store.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        issue_key = job.get("issue_key") or ""
        detail = build_task_detail(
            issue_key,
            state_manager=app.state.state_manager,
            processor=app.state.processor,
        )
        # Attach this job record; detail may still be current issue state
        return {
            "job": job,
            "issue": detail,
            "server_time": build_meta().server_time,
        }

    @app.get("/api/tasks/{issue_key}")
    def task_detail(issue_key: str) -> dict:
        detail = build_task_detail(
            issue_key,
            state_manager=app.state.state_manager,
            processor=app.state.processor,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No state for {issue_key}")
        # Include all historical jobs for this Jira issue
        detail["jobs"] = build_jobs(
            issue_key=issue_key,
            limit=100,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
        ).model_dump()["jobs"]
        return detail

    @app.post("/api/tasks/{issue_key}/cancel")
    def task_cancel(issue_key: str) -> dict:
        proc = app.state.processor
        if proc is None:
            raise HTTPException(status_code=503, detail="Processor not available")
        result = proc.cancel_job(
            issue_key,
            reason="Cancelled from ops dashboard",
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "Cancel failed")
        return result

    @app.get("/api/poll")
    def poll() -> dict:
        return build_poll_status(poll_snapshot_store, app.state.state_manager).model_dump()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return build_settings_view().model_dump()

    @app.patch("/api/settings")
    def patch_settings(body: SettingsUpdate) -> dict:
        view = apply_settings_update(body)
        # Live-update poller interval if daemon poller is attached
        poller = getattr(app.state, "poller", None)
        if poller is not None and body.poll_interval_seconds is not None:
            try:
                poller.interval = int(body.poll_interval_seconds)
            except Exception as e:
                logger.warning(f"Could not update poller interval: {e}")
        if poller is not None and body.jira_board_id is not None:
            try:
                poller.board_id = str(body.jira_board_id).strip()
            except Exception as e:
                logger.warning(f"Could not update poller board_id: {e}")
        return view.model_dump()

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return _payload()

    clients: Set[WebSocket] = set()

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        clients.add(ws)
        loop = asyncio.get_event_loop()

        def _on_snapshot(_snap: dict) -> None:
            # Called from poller thread — schedule send on event loop
            try:
                asyncio.run_coroutine_threadsafe(_broadcast(_payload()), loop)
            except Exception:
                pass

        unsub = poll_snapshot_store.subscribe(_on_snapshot)
        try:
            await ws.send_json(_payload())
            while True:
                # Keepalive / client pings; ignore payload
                try:
                    await asyncio.wait_for(ws.receive_text(), timeout=15.0)
                except asyncio.TimeoutError:
                    await ws.send_json(_payload())
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"Dashboard websocket closed: {e}")
        finally:
            unsub()
            clients.discard(ws)

    async def _broadcast(data: dict) -> None:
        dead = []
        for client in list(clients):
            try:
                await client.send_json(data)
            except Exception:
                dead.append(client)
        for d in dead:
            clients.discard(d)

    static = _static_dir()
    if static:
        assets = static / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(static / "index.html")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> Any:
            # SPA client routes
            candidate = static / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static / "index.html")
    else:

        @app.get("/")
        def index_fallback() -> dict:
            return {
                "message": "Dashboard API is running. Build the UI with: cd web && npm install && npm run build",
                "api": {
                    "meta": "/api/meta",
                    "tasks": "/api/tasks",
                    "task_detail": "/api/tasks/{issue_key}",
                    "task_cancel": "POST /api/tasks/{issue_key}/cancel",
                    "poll": "/api/poll",
                    "settings": "/api/settings",
                    "dashboard": "/api/dashboard",
                    "ws": "/ws",
                },
            }

    return app
