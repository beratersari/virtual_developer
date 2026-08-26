"""FastAPI app for the ops dashboard (REST + WebSocket)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Set

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.dashboard.schemas import (
    BulkJobDeleteRequest,
    GitlabConnectionTestRequest,
    IssueReportRequest,
    JiraConnectionTestRequest,
    ScheduleCreateRequest,
    ScheduleExistingRequest,
    SettingsUpdate,
    TempFolderDeleteRequest,
)
from src.dashboard.service import (
    apply_settings_update,
    build_dashboard_payload,
    build_jobs,
    build_meta,
    build_models_response,
    build_one_job,
    build_poll_status,
    build_settings_view,
    build_task_detail,
    build_tasks,
    collect_job_chat,
    collect_job_text_artifacts,
    build_queue,
    delete_job_record,
    delete_job_records,
    refresh_runtime_jira_clients,
)
from src.gitlab_connection import probe_gitlab_connection
from src.jira_connection import probe_jira_connection
from src.scheduler.service import (
    cancel_scheduled_job,
    create_scheduled_job,
    dispatch_schedule_now,
    list_project_issue_types,
    list_scheduled_jobs,
    preview_existing_issue,
    schedule_existing_issue,
)
from src.state.schedule_store import schedule_store
from src.state.job_store import job_store
from src.dashboard.snapshot import poll_snapshot_store
from src.logger import logger
from src.state.manager import JiraStateManager

if TYPE_CHECKING:
    from src.processor import JobProcessor


def _static_dir() -> Optional[Path]:
    """Locate prebuilt ops SPA (``web/dist``).

    Offline Windows packages ship ``web/dist`` next to ``src/``. Prefer that
    layout; also honor ``VD_WEB_DIST`` / cwd so start.bat from project root works
    even if import paths differ.
    """
    import os

    candidates: list[Path] = []
    for env_key in ("VD_WEB_DIST", "DASHBOARD_STATIC_DIR"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            candidates.append(Path(raw))
    # src/dashboard/api.py → parents[2] = package / repo root
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    candidates.append(Path.cwd() / "web" / "dist")

    seen: set[str] = set()
    for root in candidates:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_dir() and (resolved / "index.html").is_file():
            return resolved
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
    # Windows registry can map .js → text/plain; force SPA-safe types before StaticFiles
    try:
        from src.web_mimetypes import ensure_spa_mimetypes, media_type_for_path

        ensure_spa_mimetypes()
    except Exception:
        media_type_for_path = None  # type: ignore[assignment]

    sm = state_manager or JiraStateManager()
    # No OpenAPI UI in production path — dashboard has no auth
    app = FastAPI(
        title="Yaver",
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

    @app.post("/api/reports")
    def create_issue_report(body: IssueReportRequest) -> Response:
        """Download a diagnostic zip (general daemon logs, or one job + logs)."""
        from src.dashboard.issue_report import build_issue_report_zip

        try:
            payload, filename = build_issue_report_zip(
                body,
                processor=app.state.processor,
                state_manager=app.state.state_manager,
                store=job_store,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:
            logger.exception("Issue report zip failed", e)
            raise HTTPException(
                status_code=500, detail="Could not build issue report"
            ) from e
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        }
        return Response(content=payload, media_type="application/zip", headers=headers)

    @app.post("/webhooks/gitlab")
    async def gitlab_webhook(request: Request) -> dict:
        """GitLab Note Hook (MR comments). CE and EE / all plans.

        Register on the **project**: Comments only. Secret → X-Gitlab-Token.
        """
        from fastapi.responses import JSONResponse

        from src.gitlab.webhook import decide_gitlab_note_webhook

        headers = {k: v for k, v in request.headers.items()}
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "reason": "invalid json"}, status_code=400
            )
        decision = decide_gitlab_note_webhook(
            payload,
            headers=headers,
            enabled=bool(getattr(settings, "gitlab_webhook_enabled", False)),
            secret=str(getattr(settings, "gitlab_webhook_secret", "") or ""),
            bot_mentions=list(settings.gitlab_bot_mentions_list),
            bot_usernames=list(settings.gitlab_bot_usernames_list),
        )
        if not decision.accepted:
            status = int(decision.http_status or 200)
            return JSONResponse(
                {"ok": False, "reason": decision.reason},
                status_code=status,
            )
        proc = app.state.processor
        if proc is None or decision.event is None:
            raise HTTPException(
                status_code=503, detail="processor not bound; start the daemon"
            )
        result = await proc.enqueue_gitlab_note(decision.event)
        return {
            "ok": True,
            "issue_key": decision.event.issue_key,
            "queue_id": result.get("queue_id"),
            "queued": result.get("queued"),
            "started": result.get("started"),
            "status": result.get("status"),
            "reason": "queued" if result.get("queued") else "accepted",
            "server_time": build_meta().server_time,
        }

    @app.post("/webhooks/jira")
    async def jira_webhook(request: Request) -> dict:
        """Jira Server/DC 9.4 + Cloud webhook (assignment to bot, or mention).

        Register on Jira: Issue created, Issue updated, Comment created.
        Secret: ``?token=`` (Server has no HMAC) or ``X-Hub-Signature`` (Cloud).
        """
        from fastapi.responses import JSONResponse

        from src.jira.triggers import jira_body_to_text
        from src.jira.webhook import decide_jira_webhook, normalize_intake_mode

        headers = {k: v for k, v in request.headers.items()}
        raw = await request.body()
        try:
            import json as _json

            payload = _json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return JSONResponse(
                {"ok": False, "reason": "invalid json"}, status_code=400
            )
        mode = normalize_intake_mode(getattr(settings, "jira_intake_mode", "poll"))
        decision = decide_jira_webhook(
            payload,
            headers=headers,
            query=dict(request.query_params),
            raw_body=raw,
            enabled=mode == "webhook",
            secret=str(getattr(settings, "jira_webhook_secret", "") or ""),
            intake_mode=mode,
            assignee_needles=list(settings.trigger_assignee_names_list),
            mention_tokens=list(settings.trigger_mentions_list),
        )
        if not decision.accepted:
            status = int(decision.http_status or 200)
            return JSONResponse(
                {"ok": False, "reason": decision.reason},
                status_code=status,
            )
        proc = app.state.processor
        if proc is None or decision.event is None:
            raise HTTPException(
                status_code=503, detail="processor not bound; start the daemon"
            )
        event = decision.event
        issue = event.get("issue") or {}
        key = (issue.get("key") or "").strip()
        fields = issue.get("fields") or {}
        desc = fields.get("description")
        if key and (desc in (None, "")) and hasattr(proc, "jira_client"):
            try:
                full = proc.jira_client.get_issue(
                    key,
                    fields=[
                        "summary",
                        "description",
                        "labels",
                        "assignee",
                        "status",
                        "issuetype",
                    ],
                )
            except Exception:
                full = None
            if full and full.get("fields"):
                merged = dict(issue)
                merged_fields = dict(fields)
                live_fields = full.get("fields") or {}
                if live_fields.get("description") is not None and not isinstance(
                    live_fields.get("description"), str
                ):
                    live_fields = dict(live_fields)
                    live_fields["description"] = jira_body_to_text(
                        live_fields.get("description")
                    )
                merged_fields.update(live_fields)
                merged["fields"] = merged_fields
                event = dict(event)
                event["issue"] = merged
        result = await proc.enqueue_jira_event(event)
        return {
            "ok": True,
            "issue_key": key,
            "trigger": decision.trigger,
            "event_id": decision.event_id,
            "queue_id": result.get("queue_id"),
            "queued": result.get("queued"),
            "started": result.get("started"),
            "status": result.get("status"),
            "duplicate": result.get("duplicate"),
            "reason": "queued" if result.get("queued") else "accepted",
            "server_time": build_meta().server_time,
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return build_meta().model_dump()

    @app.get("/api/queue")
    def queue_list(
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict:
        """Jira + GitLab messages waiting or running on the work queue."""
        from src.state.queue_store import work_queue_store as qstore

        proc = app.state.processor
        store = getattr(proc, "queue_store", None) if proc is not None else qstore
        return build_queue(status=status, limit=limit, store=store).model_dump()

    @app.delete("/api/queue/{queue_id}")
    def queue_cancel(queue_id: str) -> dict:
        """Cancel a queued item (running jobs must be cancelled via Jobs)."""
        from src.state.queue_store import work_queue_store as qstore

        proc = app.state.processor
        store = getattr(proc, "queue_store", None) if proc is not None else qstore
        rec = store.get(queue_id)
        if not rec:
            raise HTTPException(status_code=404, detail=f"No queue item {queue_id}")
        if rec.get("status") != "queued":
            raise HTTPException(
                status_code=409,
                detail=f"Cannot cancel {queue_id} (status={rec.get('status')})",
            )
        if not store.cancel(queue_id):
            raise HTTPException(status_code=409, detail="Cancel failed")
        return {
            "ok": True,
            "queue_id": queue_id,
            "status": "cancelled",
            "server_time": build_meta().server_time,
        }

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

    @app.get("/api/jira/issue-types")
    def jira_issue_types(
        project_key: Optional[str] = Query(
            default=None,
            description="Jira project key (default: first of JIRA_PROJECTS)",
        ),
    ) -> dict:
        """List creatable issue types for the project (Cloud + on-prem)."""
        result = list_project_issue_types(project_key=project_key)
        result["server_time"] = build_meta().server_time
        if not result.get("ok") and not result.get("issue_types"):
            # Soft: still 200 with empty list + error so UI can fall back
            return result
        return result

    @app.get("/api/schedules")
    def schedules_list(
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        """List local scheduled jobs (create via CLI or POST /api/schedules)."""
        rows = list_scheduled_jobs(status=status, limit=limit, store=schedule_store)
        return {
            "schedules": rows,
            "total": len(rows),
            "server_time": build_meta().server_time,
        }

    def _maybe_dispatch_now(result: dict, dispatch_now: bool) -> dict:
        if not dispatch_now or not result.get("ok"):
            result["dispatched"] = False
            return result
        rec = result.get("schedule") or {}
        sid = (rec.get("schedule_id") or "").strip()
        if not sid:
            result["dispatched"] = False
            return result
        launched = dispatch_schedule_now(
            sid,
            processor=app.state.processor,
            store=schedule_store,
        )
        if launched.get("schedule"):
            result["schedule"] = launched["schedule"]
        result["dispatched"] = bool(launched.get("ok"))
        if not launched.get("ok"):
            result["dispatch_error"] = launched.get("error")
            return result
        # Run now must not keep the form's picker time (default +5 min).
        from datetime import datetime

        now_iso = datetime.now().isoformat(timespec="seconds")
        stamped = schedule_store.update(sid, scheduled_at=now_iso)
        if stamped:
            result["schedule"] = stamped
        return result

    @app.post("/api/schedules")
    async def schedules_create(body: ScheduleCreateRequest) -> dict:
        """Create Jira issue + schedule. Hard-fails only if issue create fails."""
        result = create_scheduled_job(
            title=body.title,
            description=body.description or "",
            repository_url=body.repository_url,
            source_branch=body.source_branch or "",
            target_branch=body.target_branch,
            mode=body.mode,
            scheduled_at=body.scheduled_at,
            project_key=body.project_key,
            issue_type=body.issue_type or "Task",
            source_branch_mode=body.source_branch_mode or "custom",
            model=body.model or "",
            backend=body.backend or "",
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error") or "Failed to create scheduled job",
            )
        result = _maybe_dispatch_now(result, body.dispatch_now)
        return {
            "ok": True,
            "schedule": result.get("schedule"),
            "issue_key": result.get("issue_key"),
            "dispatched": bool(result.get("dispatched")),
            "dispatch_error": result.get("dispatch_error"),
            "server_time": build_meta().server_time,
        }

    @app.get("/api/schedules/preview")
    def schedules_preview(
        issue_key: str = Query(..., description="Existing Jira issue key"),
    ) -> dict:
        """Load an existing issue and validate the ``{params}`` template.

        Used by the Scheduled tab before showing the run-at picker.
        Hard-fails (400) if issue missing or template invalid.
        """
        result = preview_existing_issue(issue_key)
        result["server_time"] = build_meta().server_time
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error") or "Issue preview failed",
            )
        return result

    @app.post("/api/schedules/from-issue")
    async def schedules_from_issue(body: ScheduleExistingRequest) -> dict:
        """Schedule an existing Jira issue (no new issue created).

        Hard-fails if issue cannot be loaded or template is invalid.
        Soft: In Progress transition + SCHEDULED_AI_JOB label.
        """
        result = schedule_existing_issue(
            body.issue_key,
            scheduled_at=body.scheduled_at,
            model=body.model or "",
            backend=body.backend or "",
            store=schedule_store,
        )
        if not result.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=result.get("error") or "Failed to schedule existing issue",
            )
        result = _maybe_dispatch_now(result, body.dispatch_now)
        return {
            "ok": True,
            "schedule": result.get("schedule"),
            "issue_key": result.get("issue_key"),
            "message": result.get("message"),
            "dispatched": bool(result.get("dispatched")),
            "dispatch_error": result.get("dispatch_error"),
            "server_time": build_meta().server_time,
        }

    @app.post("/api/schedules/{schedule_id}/dispatch")
    async def schedules_dispatch(schedule_id: str) -> dict:
        """Start a scheduled (or failed) job immediately, ignoring scheduled_at."""
        result = dispatch_schedule_now(
            schedule_id,
            processor=app.state.processor,
            store=schedule_store,
        )
        if not result.get("ok"):
            err = result.get("error") or "Dispatch failed"
            if "No schedule" in err:
                raise HTTPException(status_code=404, detail=err)
            raise HTTPException(status_code=409, detail=err)
        return {
            "ok": True,
            "schedule": result.get("schedule"),
            "issue_key": result.get("issue_key"),
            "message": result.get("message") or "Dispatch started",
            "server_time": build_meta().server_time,
        }

    @app.post("/api/schedules/{schedule_id}/cancel")
    def schedules_cancel(schedule_id: str) -> dict:
        result = cancel_scheduled_job(
            schedule_id,
            store=schedule_store,
            processor=app.state.processor,
        )
        if not result.get("ok"):
            err = result.get("error") or "Cancel failed"
            if "No schedule" in err:
                raise HTTPException(status_code=404, detail=err)
            raise HTTPException(status_code=409, detail=err)
        return {
            "ok": True,
            "schedule": result.get("schedule"),
            "message": result.get("message"),
            "server_time": build_meta().server_time,
        }

    @app.get("/api/storage")
    def storage_view() -> dict:
        """Disk usage for TEMP_DIR_BASE plus clone folders under it."""
        from src.dashboard.temp_storage import TempStorageError, build_storage_view

        try:
            payload = build_storage_view()
        except TempStorageError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        payload["server_time"] = build_meta().server_time
        return payload

    @app.post("/api/storage/delete")
    def storage_delete(body: TempFolderDeleteRequest) -> dict:
        """Force-delete one clone folder (Windows ``nul`` / reserved names included)."""
        from src.dashboard.temp_storage import TempStorageError, force_delete_temp_folder

        try:
            result = force_delete_temp_folder(body.name)
        except TempStorageError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message) from e
        result["server_time"] = build_meta().server_time
        return result

    @app.get("/api/opencode-sessions")
    def opencode_sessions_list(limit: int = Query(default=200, ge=1, le=500)) -> dict:
        """OpenCode sessions bound to a git repository + work branch."""
        from src.state.session_bind_store import session_bind_store as binds

        rows = binds.list_binds(limit=limit)
        return {
            "sessions": rows,
            "total": len(rows),
            "server_time": build_meta().server_time,
        }

    @app.delete("/api/opencode-sessions/{bind_id}")
    def opencode_sessions_reset(bind_id: str) -> dict:
        """Forget the stored session for this repo+branch (next job starts cold)."""
        from src.state.session_bind_store import session_bind_store as binds

        bid = (bind_id or "").strip()
        rec = binds.get_by_id(bid)
        if not rec:
            raise HTTPException(status_code=404, detail=f"No session bind {bind_id}")
        forgotten = binds.forget_session(
            bid,
            session_id=str(rec.get("session_id") or ""),
            reason="dashboard-reset",
        )
        if not forgotten:
            raise HTTPException(status_code=404, detail=f"No session bind {bind_id}")
        sid = str(rec.get("session_id") or "").strip()
        ik = str(rec.get("issue_key") or "").strip()
        sm = getattr(app.state, "state_manager", None)
        if sm is not None and ik and sid:
            try:
                st = sm.get_state(ik)
                if st and (st.current_opencode_session_id or "") == sid:
                    sm.update_state(ik, current_opencode_session_id=None)
            except Exception:
                pass
        return {
            "ok": True,
            "bind_id": bid,
            "message": (
                f"Reset OpenCode session for {rec.get('repository_key') or rec.get('repository_url')}"
                f"@{rec.get('branch')}→{rec.get('target_branch')}. "
                "Next job on that bind starts a new session."
            ),
            "server_time": build_meta().server_time,
        }

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

    @app.get("/api/jobs/{job_id}/artifacts")
    def job_artifacts(job_id: str) -> dict:
        """Prompt + OpenCode session text for this job only (lazy tab load)."""
        jid = (job_id or "").strip()
        if not jid or jid.startswith("legacy_"):
            raise HTTPException(
                status_code=404,
                detail="Legacy session jobs are not supported; open a real job_* id",
            )
        item = build_one_job(
            jid,
            processor=app.state.processor,
            store=job_store,
            state_manager=app.state.state_manager,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        arts = collect_job_text_artifacts(item)
        return {
            "job_id": item.job_id,
            "prompts": arts["prompts"],
            "session_logs": arts["session_logs"],
            "server_time": build_meta().server_time,
        }

    @app.get("/api/jobs/{job_id}/chat")
    def job_chat(job_id: str) -> dict:
        """OpenCode session transcript (user/assistant/tool parts) for this job."""
        jid = (job_id or "").strip()
        if not jid or jid.startswith("legacy_"):
            raise HTTPException(
                status_code=404,
                detail="Legacy session jobs are not supported; open a real job_* id",
            )
        item = build_one_job(
            jid,
            processor=app.state.processor,
            store=job_store,
            state_manager=app.state.state_manager,
        )
        if item is None:
            raise HTTPException(status_code=404, detail=f"No job {job_id}")
        chat = collect_job_chat(item)
        chat["server_time"] = build_meta().server_time
        return chat

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict:
        """Single job document from JobStore (retries are fields on the job).

        Overview-only: no issue-wide session dumps and no live Jira. Prompt /
        Output tabs fetch ``GET /api/jobs/{id}/artifacts``.
        """
        jid = (job_id or "").strip()
        if not jid or jid.startswith("legacy_"):
            raise HTTPException(
                status_code=404,
                detail="Legacy session jobs are not supported; open a real job_* id",
            )
        item = build_one_job(
            jid,
            processor=app.state.processor,
            store=job_store,
            state_manager=app.state.state_manager,
        )
        if item is None:
            raw = job_store.get_job(jid)
            if not raw:
                raise HTTPException(status_code=404, detail=f"No job {job_id}")
            job = raw
            issue_key = (job.get("issue_key") or "").strip()
        else:
            job = item.model_dump()
            issue_key = item.issue_key or ""
        detail = None
        if issue_key:
            detail = build_task_detail(
                issue_key,
                state_manager=app.state.state_manager,
                processor=app.state.processor,
                include_artifacts=False,
                include_live_jira=False,
                jobs=[item] if item is not None else None,
            )
        from src.dashboard.issue_logs import issue_log_ring

        system_logs = issue_log_ring.for_job(jid, limit=500)
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
    def task_detail(
        issue_key: str,
        live: bool = Query(
            default=False,
            description="Fetch live Jira summary/description (slower).",
        ),
        artifacts: bool = Query(
            default=False,
            description="Include issue-wide prompt/session file bodies.",
        ),
    ) -> dict:
        jobs_resp = build_jobs(
            issue_key=issue_key,
            limit=100,
            page=1,
            page_size=100,
            processor=app.state.processor,
            state_manager=app.state.state_manager,
        )
        detail = build_task_detail(
            issue_key,
            state_manager=app.state.state_manager,
            processor=app.state.processor,
            include_artifacts=artifacts,
            include_live_jira=live,
            jobs=jobs_resp.jobs,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail=f"No state for {issue_key}")
        detail["jobs"] = jobs_resp.model_dump()["jobs"]
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
        """Deprecated: plans never auto-start; dashboard Start is disabled.

        Use a new issue with Mode: build, or label ai-start-work / ai-execute
        on a plan_ready ticket while it is To Do.
        """
        raise HTTPException(
            status_code=410,
            detail=(
                "Starting work from the dashboard is disabled. "
                "Plans never auto-start: open a new Mode: build issue, or add "
                "label ai-start-work / ai-execute on a plan_ready ticket in To Do."
            ),
        )

    @app.get("/api/poll")
    def poll() -> dict:
        return build_poll_status(poll_snapshot_store, app.state.state_manager).model_dump()

    @app.get("/api/settings")
    def get_settings() -> dict:
        return build_settings_view().model_dump()

    @app.post("/api/settings/gitlab/test")
    def settings_gitlab_test(body: GitlabConnectionTestRequest) -> dict:
        """Verify a GitLab host PAT (list user + reachable projects).

        PAT is optional in the body: when omitted/empty, uses the stored PAT
        for that host. Never echoes the PAT back.
        """
        result = probe_gitlab_connection(
            body.host,
            pat=body.pat,
            max_projects=int(body.max_projects or 25),
        )
        result["server_time"] = build_meta().server_time
        # Always 200 with ok flag so UI can show soft failures cleanly
        return result

    @app.post("/api/settings/jira/test")
    def settings_jira_test(body: JiraConnectionTestRequest) -> dict:
        """Verify Jira host credentials (``/myself`` + project list).

        Token only → Bearer; email + token → Cloud Basic. Empty token uses the
        stored runtime token. Never echoes secrets.
        """
        # Omit/blank fields use saved runtime settings (host, email, token).
        result = probe_jira_connection(
            host=body.host,
            email=body.email,
            api_token=body.api_token,
            max_projects=int(body.max_projects or 25),
        )
        result["server_time"] = build_meta().server_time
        return result

    @app.get("/api/models")
    def get_models(refresh: bool = False, backend: str = "") -> dict:
        """List models for the selected worker (OpenCode CLI or Codex ~/.codex)."""
        return build_models_response(refresh=refresh, backend=backend).model_dump()

    @app.patch("/api/settings")
    def patch_settings(body: SettingsUpdate) -> dict:
        # Detect auth/connection changes before apply (for client refresh).
        # Settings save always clears JIRA_EMAIL — treat a previously set email
        # as an auth change so live clients switch to Bearer.
        auth_keys = ("jira_host", "jira_api_token")
        dumped = body.model_dump(exclude_unset=True)
        email_before = (getattr(settings, "jira_email", "") or "").strip()
        auth_changed = any(k in dumped for k in auth_keys) or bool(email_before)

        try:
            view = apply_settings_update(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        poller = getattr(app.state, "poller", None)
        proc = app.state.processor
        if poller is not None and body.poll_interval_seconds is not None:
            try:
                poller.interval = int(body.poll_interval_seconds)
            except Exception as e:
                logger.warning(f"Could not update poller interval: {e}")
        if body.jira_board_id is not None:
            board = str(body.jira_board_id).strip()
            if poller is not None:
                try:
                    poller.board_id = board
                except Exception as e:
                    logger.warning(f"Could not update poller board_id: {e}")
            try:
                poll_snapshot_store.set_board_id(board)
            except Exception as e:
                logger.warning(f"Could not update poll snapshot board_id: {e}")
        # Resize live job semaphore when concurrency setting changes
        if (
            proc is not None
            and body.max_concurrent_jobs is not None
            and hasattr(proc, "resize_job_semaphore")
        ):
            try:
                proc.resize_job_semaphore(int(body.max_concurrent_jobs))
            except Exception as e:
                logger.warning(f"Could not resize job semaphore: {e}")
        # Rebuild Jira clients so new host/token/email take effect immediately
        if auth_changed:
            try:
                refresh_runtime_jira_clients(processor=proc, poller=poller)
            except Exception as e:
                logger.warning(f"Jira client refresh after settings update failed: {e}")
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
        logger.info(f"Dashboard SPA static root: {static}")
        assets = static / "assets"
        if assets.is_dir():
            # Mount assets first so hashed JS/CSS are not swallowed by SPA fallback.
            # Subclass forces JS/CSS MIME — Windows registry can map .js → text/plain.
            class _SpaStaticFiles(StaticFiles):
                def file_response(  # type: ignore[no-untyped-def]
                    self, full_path, stat_result, scope, status_code=200
                ):
                    resp = super().file_response(
                        full_path, stat_result, scope, status_code=status_code
                    )
                    if media_type_for_path is not None:
                        try:
                            mt = media_type_for_path(Path(str(full_path)))
                        except Exception:
                            mt = None
                        if mt:
                            resp.headers["content-type"] = mt
                            if hasattr(resp, "media_type"):
                                resp.media_type = mt
                    return resp

            app.mount(
                "/assets",
                _SpaStaticFiles(directory=str(assets)),
                name="assets",
            )
        else:
            logger.warning(
                f"Dashboard SPA missing assets/ under {static} — UI will be blank"
            )

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
            mt = None
            if media_type_for_path is not None:
                try:
                    mt = media_type_for_path(path)
                except Exception:
                    mt = None
            kwargs: dict = {
                "path": path,
                "headers": {
                    "Cache-Control": cache,
                    "Pragma": "no-cache" if "no-cache" in cache else "public",
                },
            }
            if mt:
                kwargs["media_type"] = mt
            return FileResponse(**kwargs)

        @app.get("/")
        def index() -> FileResponse:
            return _spa_index()

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> Any:
            # Never serve files outside web/dist (blocks ../ path traversal)
            # Do not SPA-fallback reserved API/docs/asset paths
            low = (full_path or "").lstrip("/").lower()
            if low == "docs" or low.startswith("docs/") or low in (
                "redoc",
                "openapi.json",
            ) or low.startswith("api/") or low == "ws" or low.startswith("ws/"):
                raise HTTPException(status_code=404, detail="Not found")
            # If /assets mount missed a file, 404 rather than returning index.html
            # (wrong MIME breaks the SPA with a blank page).
            if low == "assets" or low.startswith("assets/"):
                raise HTTPException(status_code=404, detail="Asset not found")
            safe = _safe_under_static(static, full_path)
            if safe is not None:
                return _static_file(safe)
            return _spa_index()
    else:
        logger.warning(
            "Dashboard SPA not found (web/dist/index.html). "
            "API still runs; browser will show JSON at /. "
            "Offline zip must include web/dist from CI npm run build."
        )

        @app.get("/")
        def index_fallback() -> dict:
            return {
                "message": (
                    "Dashboard API is running but the UI SPA is missing. "
                    "Expected web/dist/index.html next to src/. "
                    "Rebuild the Windows dist (CI builds web/) or run: "
                    "cd web && npm install && npm run build"
                ),
                "hint": "Open http://127.0.0.1:8080/ — not :5173 (no Vite in offline mode)",
                "api": {
                    "meta": "/api/meta",
                    "tasks": "/api/tasks",
                    "task_detail": "/api/tasks/{issue_key}",
                    "task_cancel": "POST /api/tasks/{issue_key}/cancel",
                    "job_delete": "DELETE /api/jobs/{job_id}",
                    "jobs_bulk_delete": "POST /api/jobs/bulk-delete",
                    "schedules": "GET|POST /api/schedules",
                    "schedule_dispatch": "POST /api/schedules/{id}/dispatch",
                    "schedule_cancel": "POST /api/schedules/{id}/cancel",
                    "poll": "/api/poll",
                    "settings": "/api/settings",
                    "models": "/api/models",
                    "dashboard": "/api/dashboard",
                    "reports": "POST /api/reports",
                    "ws": "/ws",
                },
            }

    return app
