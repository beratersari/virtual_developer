"""FastAPI app for the ops dashboard (REST + WebSocket)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Set

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.dashboard.schemas import BulkJobDeleteRequest, SettingsUpdate
from src.dashboard.service import (
    apply_settings_update,
    build_dashboard_payload,
    build_jobs,
    build_meta,
    build_models_response,
    build_poll_status,
    build_settings_view,
    build_task_detail,
    build_tasks,
    delete_job_record,
    delete_job_records,
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


def _safe_under_static(static: Path, full_path: str) -> Optional[Path]:
    """Resolve a path under ``static`` or return None (blocks traversal)."""
    if not full_path or full_path.startswith(("/", "\\")):
        return None
    # Reject empty segments / parent references before resolve
    parts = Path(full_path).parts
    if any(p in ("..", "") for p in parts):
        return None
    static_root = static.resolve()
    try:
        candidate = (static_root / full_path).resolve()
        candidate.relative_to(static_root)
    except (ValueError, OSError):
        return None
    if candidate.is_file():
        return candidate
    return None


def create_dashboard_app(
    *,
    processor: Optional["JobProcessor"] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> FastAPI:
    """Create dashboard FastAPI application bound to daemon services."""
    sm = state_manager or JiraStateManager()
    # No OpenAPI UI in production path — dashboard has no auth
    app = FastAPI(
        title="JIRA Virtual Developer Dashboard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.processor = processor
    app.state.state_manager = sm

    # Same-origin SPA in production; Vite dev proxy only (never wildcard + credentials)
    dev_origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=dev_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
    def jobs(
        issue_key: Optional[str] = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=25, ge=1, le=100),
        limit: Optional[int] = Query(
            default=None,
            ge=1,
            le=100,
            description="Deprecated alias for page_size (page forced to 1 if set alone)",
        ),
    ) -> dict:
        size = page_size if limit is None else limit
        return build_jobs(
            issue_key=issue_key,
            page=page,
            page_size=size,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
        ).model_dump()

    @app.post("/api/jobs/bulk-delete")
    def jobs_bulk_delete(body: BulkJobDeleteRequest) -> dict:
        """Permanently delete multiple historical job records.

        Refuses live / in-flight jobs per id. Always returns 200 with per-id
        results when ``job_ids`` is non-empty so partial success is visible.
        Does not delete Jira issues.
        """
        ids = [jid.strip() for jid in (body.job_ids or []) if (jid or "").strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="job_ids is required")
        result = delete_job_records(
            ids,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
            delete_artifacts=body.delete_artifacts,
        )
        result["server_time"] = build_meta().server_time
        return result

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        """Single job document. Falls back to merged jobs list (incl. legacy session rows)."""
        jid = (job_id or "").strip()
        job = job_store.get_job(jid) if jid else None
        if not job and jid:
            # Legacy session-derived jobs are not in JobStore files
            merged = build_jobs(
                limit=500,
                page=1,
                page_size=500,
                processor=app.state.processor,
                state_manager=app.state.state_manager,
            )
            for item in merged.jobs:
                if item.job_id == jid:
                    job = item.model_dump()
                    break
        if not job:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        issue_key = job.get("issue_key") or ""
        detail = None
        if issue_key:
            detail = build_task_detail(
                issue_key,
                state_manager=app.state.state_manager,
                processor=app.state.processor,
            )
            if detail is not None:
                detail["jobs"] = build_jobs(
                    issue_key=issue_key,
                    limit=100,
                    processor=app.state.processor,
                    state_manager=app.state.state_manager,
                ).model_dump()["jobs"]
        # Daemon log lines for this job: durable file + live ring (survives restart)
        from src.dashboard.issue_logs import issue_log_ring

        system_logs = issue_log_ring.for_job(jid, limit=2000)
        return {
            "job": job,
            "issue": detail,
            "system_logs": system_logs,
            "server_time": build_meta().server_time,
        }

    @app.delete("/api/jobs/{job_id}")
    def job_delete(
        job_id: str,
        delete_artifacts: bool = Query(
            default=True,
            description="Also delete linked session log / prompt under .jira-agent",
        ),
    ) -> dict:
        """Permanently delete a historical job record.

        Refuses live / in-flight jobs. Does not delete the Jira issue.
        """
        result = delete_job_record(
            job_id,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
            delete_artifacts=delete_artifacts,
        )
        if not result.get("ok"):
            err = result.get("error") or "Delete failed"
            # 404 if missing; 409 if still running
            if "No job" in err or "not found" in err.lower():
                raise HTTPException(status_code=404, detail=err)
            raise HTTPException(status_code=409, detail=err)
        result["server_time"] = build_meta().server_time
        return result

    @app.get("/api/tasks/{issue_key}")
    def task_detail(issue_key: str) -> dict:
        detail = build_task_detail(
            issue_key,
            state_manager=app.state.state_manager,
            processor=app.state.processor,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No state for {issue_key}")
        detail["jobs"] = build_jobs(
            issue_key=issue_key,
            limit=100,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
        ).model_dump()["jobs"]
        return detail

    @app.post("/api/tasks/{issue_key}/cancel")
    async def task_cancel(issue_key: str) -> dict:
        """Cancel on the event loop so it shares locks with workflows (B6)."""
        proc = app.state.processor
        if proc is None:
            raise HTTPException(status_code=503, detail="Processor not available")
        result = await proc.cancel_job(
            issue_key,
            reason="Cancelled from ops dashboard",
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "Cancel failed")
        return result

    @app.post("/api/tasks/{issue_key}/start")
    async def task_start(issue_key: str) -> dict:
        """Deprecated: plan execution is started from Jira only.

        Set ``Mode: build`` in the issue description and move the issue back
        to To Do so the board poller picks it up.
        """
        raise HTTPException(
            status_code=410,
            detail=(
                "Starting work from the dashboard is disabled. "
                "Set Mode: build in the issue description and move the issue "
                "back to To Do."
            ),
        )

    @app.get("/api/poll")
    def poll() -> dict:
        return build_poll_status(poll_snapshot_store, app.state.state_manager).model_dump()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return build_settings_view().model_dump()

    @app.get("/api/models")
    def get_models(refresh: bool = False) -> dict:
        """List OpenCode models (CLI + opencode.json). Sole inventory endpoint for the UI."""
        return build_models_response(refresh=refresh).model_dump()

    @app.patch("/api/settings")
    def patch_settings(body: SettingsUpdate) -> dict:
        view = apply_settings_update(body)
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
        # Resize live job semaphore when concurrency setting changes
        proc = app.state.processor
        if (
            proc is not None
            and body.max_concurrent_jobs is not None
            and hasattr(proc, "resize_job_semaphore")
        ):
            try:
                proc.resize_job_semaphore(int(body.max_concurrent_jobs))
            except Exception as e:
                logger.warning(f"Could not resize job semaphore: {e}")
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
            try:
                asyncio.run_coroutine_threadsafe(_broadcast(_payload()), loop)
            except Exception:
                pass

        unsub = poll_snapshot_store.subscribe(_on_snapshot)
        try:
            await ws.send_json(_payload())
            while True:
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

        def _spa_index() -> FileResponse:
            # Always revalidate HTML so browsers pick up new hashed asset names
            return FileResponse(
                static / "index.html",
                headers={
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        def _static_file(path: Path) -> FileResponse:
            # Hashed assets are immutable; non-hashed files revalidate
            name = path.name
            if name.startswith("index-") and name.endswith((".js", ".css")):
                cache = "public, max-age=31536000, immutable"
            else:
                cache = "no-cache, no-store, must-revalidate"
            return FileResponse(
                path,
                headers={
                    "Cache-Control": cache,
                    "Pragma": "no-cache" if "no-cache" in cache else "public",
                },
            )

        @app.get("/")
        def index() -> FileResponse:
            return _spa_index()

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> Any:
            # Never serve files outside web/dist (blocks ../ path traversal)
            # Do not SPA-fallback reserved API/docs paths (docs are disabled)
            low = (full_path or "").lstrip("/").lower()
            if low == "docs" or low.startswith("docs/") or low in (
                "redoc",
                "openapi.json",
            ) or low.startswith("api/"):
                raise HTTPException(status_code=404, detail="Not found")
            safe = _safe_under_static(static, full_path)
            if safe is not None:
                return _static_file(safe)
            return _spa_index()
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
                    "job_delete": "DELETE /api/jobs/{job_id}",
                    "jobs_bulk_delete": "POST /api/jobs/bulk-delete",
                    "poll": "/api/poll",
                    "settings": "/api/settings",
                    "models": "/api/models",
                    "dashboard": "/api/dashboard",
                    "ws": "/ws",
                },
            }

    return app
