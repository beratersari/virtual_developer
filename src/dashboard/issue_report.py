"""Build operator issue-report zip archives (general or per-job)."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

import httpx

from src.config import settings
from src.dashboard.issue_logs import issue_log_ring, job_system_log_path
from src.dashboard.schemas import IssueReportRequest
from src.dashboard.service import (
    _job_prompt_paths,
    _job_session_log_paths,
    build_jobs,
    build_meta,
    build_one_job,
    build_poll_status,
    build_queue,
    build_settings_view,
    build_task_detail,
    collect_job_chat,
    collect_job_text_artifacts,
)
from src.logger import daemon_log_path, logger

if TYPE_CHECKING:
    from src.processor import JobProcessor
    from src.state.job_store import JobStore
    from src.state.manager import JiraStateManager

_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_OPENCODE_LOG_BYTES = 2 * 1024 * 1024
_MAX_SYSTEM_LINES = 8000
_MAX_LOG_DIR_FILES = 40
_MAX_OPENCODE_LOG_FILES = 20
_MAX_CHAT_JSON_CHARS = 8 * 1024 * 1024
_SERVE_TIMEOUT = httpx.Timeout(1.2, connect=0.35)
_serve_cache: Dict[str, Dict[str, Any]] = {}
_GLPAT = re.compile(r"glpat-[A-Za-z0-9_\-]{8,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}")
_BASIC = re.compile(r"(?i)(basic\s+)[A-Za-z0-9+/=]{8,}")


def _resolved_job_ids(body: IssueReportRequest) -> List[str]:
    ids: List[str] = []
    seen = set()
    for raw in list(body.job_ids or []) + [body.job_id]:
        jid = (raw or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        ids.append(jid)
    return ids[:20]


def _job_raw_record(job_id: str, store: Optional["JobStore"]) -> Optional[Dict[str, Any]]:
    if store is not None:
        return store.get_job(job_id)
    import src.state.job_store as job_store_mod

    return job_store_mod.job_store.get_job(job_id)


def build_issue_report_zip(
    body: IssueReportRequest,
    *,
    processor: Optional["JobProcessor"] = None,
    state_manager: Optional["JiraStateManager"] = None,
    store: Optional["JobStore"] = None,
) -> Tuple[bytes, str]:
    """Return ``(zip_bytes, filename)`` for a general or job report."""
    kind = body.kind
    job_ids = _resolved_job_ids(body)
    if kind == "job" and not job_ids:
        raise ValueError("job_id or job_ids is required when kind is 'job'")

    selected: List[Tuple[Any, Dict[str, Any]]] = []
    for jid in job_ids:
        item = build_one_job(
            jid,
            processor=processor,
            store=store,
            state_manager=state_manager,
        )
        if item is None:
            raise FileNotFoundError(f"No job {jid}")
        raw = _job_raw_record(jid, store)
        selected.append((item, raw or {}))

    job_item = selected[0][0] if selected else None
    job_id = job_ids[0] if job_ids else ""

    _serve_cache.clear()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if len(selected) == 1 and job_item is not None:
        issue = _safe_name(job_item.issue_key or "issue")
        filename = f"yaver-report-{issue}-{_safe_name(job_item.job_id)}-{stamp}.zip"
    elif len(selected) > 1:
        filename = f"yaver-report-jobs-{len(selected)}-{stamp}.zip"
    else:
        filename = f"yaver-report-general-{stamp}.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_text(zf, "NOTE.txt", body.note + "\n")
        _write_text(zf, "README.txt", _readme(kind, [item for item, _raw in selected]))
        _write_json(
            zf,
            "meta.json",
            {
                "kind": kind,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "app": build_meta().model_dump(),
                "job_id": job_item.job_id if job_item is not None else None,
                "job_ids": [item.job_id for item, _raw in selected],
                "issue_key": job_item.issue_key if job_item is not None else None,
                "issue_keys": list(
                    dict.fromkeys(
                        item.issue_key
                        for item, _raw in selected
                        if getattr(item, "issue_key", None)
                    )
                ),
                "summary": job_item.summary if job_item is not None else None,
            },
        )
        _add_runtime(zf, processor=processor)
        _add_serve(zf, job_items=[item for item, _raw in selected])
        _add_settings(zf)
        _add_poll(zf, state_manager=state_manager)
        _add_queue(zf)
        _add_schedules(zf)
        _add_session_binds(zf)
        _add_issue_states(
            zf,
            state_manager=state_manager,
            job_items=[item for item, _raw in selected],
        )
        _add_storage(zf)
        _add_recent_jobs(zf, processor=processor, store=store, state_manager=state_manager)
        _add_safe_environ(zf)
        _add_opencode_home(zf)
        _add_system_logs(zf)

        for item, raw in selected:
            prefix = (
                f"jobs/{_safe_name(item.job_id)}"
                if len(selected) > 1
                else "job"
            )
            _add_job_bundle(
                zf,
                item,
                raw,
                processor=processor,
                state_manager=state_manager,
                prefix=prefix,
            )

    payload = buf.getvalue()
    logger.info(
        f"Issue report built kind={kind} job_id={job_id or '-'} "
        f"bytes={len(payload)}"
    )
    return payload, filename


def _readme(kind: str, job_items: Any) -> str:
    items = [j for j in (job_items or []) if j is not None]
    job_item = items[0] if items else None
    lines = [
        "Yaver issue report",
        "",
        f"Kind: {kind}",
        "",
        "NOTE.txt                 Operator note",
        "meta.json                App version and report metadata",
        "runtime.json             Host, Python, CLI versions, live jobs, paths",
        "serve.json               OpenCode serve health, session status, models",
        "settings.json            Safe dashboard settings (no tokens)",
        "poll.json                Last board poll (matched issues + raw snapshot)",
        "queue.json               Work queue (queued + running)",
        "schedules.json           Scheduled jobs",
        "sessions.json            OpenCode session binds (repo + branch)",
        "states.json              Local issue state machine",
        "storage.json             Temp clone folders + disk use",
        "jobs.json                Recent job list (no chat transcripts)",
        "environ.json             Safe process env (no tokens)",
        "opencode-home.json       Detected OpenCode home + config (no keys)",
        "system/daemon.log        In-process daemon log ring",
        "system/daemon-file.log   Durable YAVER_DATA_DIR/logs/daemon.log (if any)",
        "system/logs/             Files from YAVER_DATA_DIR/logs and cwd/logs",
        "system/job-logs/         Per-job durable system logs",
        "system/opencode-logs/    Recent OpenCode serve/CLI log files",
    ]
    if kind == "job" and items:
        lines.extend(["", "Selected jobs:"])
        for item in items:
            lines.append(f"  - {item.job_id}  {item.issue_key} — {item.summary}")
        folder = "jobs/<job_id>/" if len(items) > 1 else "job/"
        lines.extend(
            [
                "",
                f"Job files live under {folder}",
                "record.json               Job record (safe fields)",
                "parameters.json           Issue {{params}} and run parameters",
                "description.txt           Frozen issue / MR description",
                "retry_attempts.json       Retry bookkeeping",
                "system.log                Daemon lines for this job",
                "prompts/                  Initial + retry prompt files",
                "session_logs/             OpenCode / Codex session logs",
                "chat.json                 Session transcript (tool calls, model text)",
                "chat.md                   Same transcript, readable",
                "issue.json                Local + live Jira/GitLab issue snapshot",
                "plan.md                   Durable plan file from YAVER_DATA_DIR/plans",
                "git.txt                   git status / log / diff --stat in the clone",
                "serve.json                Serve status, todos, last messages for this session",
            ]
        )
    return "\n".join(lines) + "\n"


def _add_runtime(
    zf: zipfile.ZipFile, *, processor: Optional["JobProcessor"]
) -> None:
    oc = (getattr(settings, "opencode_cli", None) or "opencode").strip() or "opencode"
    codex = (getattr(settings, "codex_cli", None) or "codex").strip() or "codex"
    serve_url = (
        getattr(settings, "opencode_serve_url", None) or "http://127.0.0.1:4096"
    ).rstrip("/")
    live_keys: List[str] = []
    active_jobs: Dict[str, str] = {}
    if processor is not None:
        try:
            live_keys = list(processor.list_live_processing_keys())
        except Exception:
            live_keys = []
        try:
            active_jobs = {
                str(k): str(v) for k, v in (processor._active_jobs or {}).items()
            }
        except Exception:
            active_jobs = {}
    payload = {
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "python": sys.version,
        "python_executable": sys.executable,
        "cwd": str(Path.cwd()),
        "pid": os.getpid(),
        "which": {
            "opencode": shutil.which(oc),
            "codex": shutil.which(codex),
            "glab": shutil.which("glab"),
            "git": shutil.which("git"),
        },
        "cli_versions": {
            "opencode": _cli_version(oc),
            "codex": _cli_version(codex),
            "glab": _cli_version("glab"),
            "git": _cli_version("git"),
        },
        "opencode_cli": oc,
        "codex_cli": codex,
        "opencode_serve_url": serve_url,
        "opencode_serve_health": _probe_serve(serve_url),
        "product_version": _product_version(),
        "live_issue_keys": live_keys,
        "active_jobs": active_jobs,
        "paths": _runtime_paths(),
    }
    _write_json(zf, "runtime.json", payload)


def _add_serve(zf: zipfile.ZipFile, *, job_items: Optional[List[Any]] = None) -> None:
    serve_url = (
        getattr(settings, "opencode_serve_url", None) or "http://127.0.0.1:4096"
    ).rstrip("/")
    payload: Dict[str, Any] = {
        "base_url": serve_url,
        "health": _serve_get(serve_url, "/global/health"),
        "session_status": _serve_get(serve_url, "/session/status"),
        "sessions": _serve_get(serve_url, "/session"),
        "providers": _serve_provider_ids(serve_url),
        "config": _serve_safe_config(serve_url),
    }
    sids: List[str] = []
    for job_item in job_items or []:
        for sid in list(getattr(job_item, "opencode_session_ids", None) or []):
            one = str(sid or "").strip()
            if one and one not in sids:
                sids.append(one)
        sid = str(getattr(job_item, "opencode_session_id", "") or "").strip()
        if sid and sid not in sids:
            sids.append(sid)
    if sids:
        sessions: Dict[str, Any] = {}
        for one in sids[:16]:
            sessions[one] = {
                "todos": _serve_get(serve_url, f"/session/{one}/todo"),
                "messages_tail": _serve_get(
                    serve_url, f"/session/{one}/message", params={"limit": "30"}
                ),
            }
        payload["job_sessions"] = sessions
    _write_json(zf, "serve.json", payload)


def _add_storage(zf: zipfile.ZipFile) -> None:
    def _collect() -> Dict[str, Any]:
        from src.dashboard.temp_storage import build_storage_view

        return build_storage_view()

    _write_json(zf, "storage.json", _safe_call("storage", _collect))


def _add_recent_jobs(
    zf: zipfile.ZipFile,
    *,
    processor: Optional["JobProcessor"],
    store: Optional["JobStore"],
    state_manager: Optional["JiraStateManager"],
) -> None:
    def _collect() -> Dict[str, Any]:
        resp = build_jobs(
            page=1,
            page_size=50,
            processor=processor,
            store=store,
            state_manager=state_manager,
        )
        dumped = resp.model_dump() if hasattr(resp, "model_dump") else resp
        rows = []
        for job in dumped.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            rows.append(
                {
                    "job_id": job.get("job_id"),
                    "issue_key": job.get("issue_key"),
                    "summary": job.get("summary"),
                    "status": job.get("status"),
                    "backend": job.get("backend"),
                    "model": job.get("model"),
                    "workflow_type": job.get("workflow_type"),
                    "live": job.get("live"),
                    "error_message": job.get("error_message"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                    "opencode_session_id": job.get("opencode_session_id"),
                    "working_directory": job.get("working_directory"),
                    "delivery_status": job.get("delivery_status"),
                }
            )
        return {"total": dumped.get("total"), "jobs": rows}

    _write_json(zf, "jobs.json", _safe_call("jobs", _collect))


def _add_safe_environ(zf: zipfile.ZipFile) -> None:
    allow_prefixes = (
        "YAVER_",
        "VD_",
        "DASHBOARD_",
        "JIRA_",
        "GITLAB_",
        "AZURE_",
        "POLL_",
        "TEMP_DIR",
        "OPENCODE_",
        "CODEX_",
    )
    deny_keys = {
        "JIRA_API_TOKEN",
        "GITLAB_PAT",
        "GITLAB_WEBHOOK_SECRET",
        "AZURE_PAT",
        "AZURE_WEBHOOK_SECRET",
        "AZURE_HOST_PATS",
        "AZURE_COLLECTION_PATS",
        "DASHBOARD_PASSWORD",
    }
    out: Dict[str, str] = {}
    for key, val in sorted(os.environ.items()):
        upper = key.upper()
        if upper in deny_keys:
            out[key] = "***"
            continue
        if any(upper.startswith(p) for p in allow_prefixes):
            text = str(val)
            if any(
                token in upper
                for token in ("TOKEN", "PAT", "SECRET", "PASSWORD", "PASS")
            ):
                out[key] = "***"
            else:
                out[key] = text[:500]
    _write_json(zf, "environ.json", out)


def _add_opencode_home(zf: zipfile.ZipFile) -> None:
    homes = _opencode_home_candidates()
    found: List[Dict[str, Any]] = []
    for home in homes:
        if not home.is_dir():
            continue
        cfg = home / "opencode.json"
        entry: Dict[str, Any] = {
            "path": str(home),
            "exists": True,
            "opencode_json": str(cfg) if cfg.is_file() else None,
        }
        if cfg.is_file():
            try:
                raw = json.loads(cfg.read_text(encoding="utf-8"))
            except Exception as e:
                entry["opencode_json_error"] = str(e)
            else:
                if isinstance(raw, dict):
                    entry["autoupdate"] = raw.get("autoupdate")
                    entry["plugin"] = raw.get("plugin")
                    entry["model"] = raw.get("model")
                    keys = [k for k in raw.keys() if k not in {"provider", "mcp"}]
                    entry["top_level_keys"] = keys
        found.append(entry)
    _write_json(
        zf,
        "opencode-home.json",
        {"homes": found, "searched": [str(p) for p in homes]},
    )


def _add_settings(zf: zipfile.ZipFile) -> None:
    view = _safe_call("settings", lambda: build_settings_view().model_dump())
    _write_json(zf, "settings.json", view)
    try:
        from src.config import load_runtime_settings

        runtime = load_runtime_settings()
    except Exception as e:
        runtime = {"error": str(e)}
    _write_json(zf, "runtime_settings.json", runtime)


def _add_poll(
    zf: zipfile.ZipFile, *, state_manager: Optional["JiraStateManager"]
) -> None:
    def _collect() -> Dict[str, Any]:
        from src.dashboard.snapshot import poll_snapshot_store

        filtered = build_poll_status(
            poll_snapshot_store, state_manager
        ).model_dump()
        raw = poll_snapshot_store.snapshot()
        return {"filtered": filtered, "raw": raw}

    _write_json(zf, "poll.json", _safe_call("poll", _collect))


def _add_queue(zf: zipfile.ZipFile) -> None:
    def _collect() -> Dict[str, Any]:
        q = build_queue(limit=200)
        return q.model_dump() if hasattr(q, "model_dump") else q

    _write_json(zf, "queue.json", _safe_call("queue", _collect))


def _add_schedules(zf: zipfile.ZipFile) -> None:
    def _collect() -> Dict[str, Any]:
        from src.scheduler.service import list_scheduled_jobs

        items = list_scheduled_jobs(limit=200)
        return {"schedules": items, "total": len(items)}

    _write_json(zf, "schedules.json", _safe_call("schedules", _collect))


def _add_session_binds(zf: zipfile.ZipFile) -> None:
    def _collect() -> Dict[str, Any]:
        from src.state.session_bind_store import session_bind_store

        items = session_bind_store.list_binds(limit=200)
        return {"sessions": items, "total": len(items)}

    _write_json(zf, "sessions.json", _safe_call("session_binds", _collect))


def _add_issue_states(
    zf: zipfile.ZipFile,
    *,
    state_manager: Optional["JiraStateManager"],
    job_items: Optional[List[Any]] = None,
) -> None:
    def _collect() -> Dict[str, Any]:
        if state_manager is None:
            from src.state.manager import JiraStateManager

            sm = JiraStateManager()
        else:
            sm = state_manager
        rows = []
        for st in sm.get_all_states():
            dumped = st.to_dict() if hasattr(st, "to_dict") else {}
            rows.append(
                {
                    "issue_key": dumped.get("issue_key") or st.issue_key,
                    "status": dumped.get("status"),
                    "summary": dumped.get("issue_summary"),
                    "error_message": dumped.get("error_message"),
                    "current_task_id": dumped.get("current_task_id"),
                    "current_opencode_session_id": dumped.get(
                        "current_opencode_session_id"
                    ),
                    "started_at": dumped.get("started_at"),
                    "completed_at": dumped.get("completed_at"),
                    "current_job_id": (dumped.get("metadata") or {}).get(
                        "current_job_id"
                    ),
                    "workflow_type": (dumped.get("metadata") or {}).get(
                        "workflow_type"
                    ),
                }
            )
        selected_rows = []
        keys: List[str] = []
        for job_item in job_items or []:
            key = str(getattr(job_item, "issue_key", "") or "").strip()
            if not key or key in keys:
                continue
            keys.append(key)
            st = sm.get_state(key)
            if st is not None:
                selected_rows.append(st.to_dict())
        first = keys[0] if keys else ""
        first_selected = selected_rows[0] if selected_rows else None
        return {
            "issues": rows,
            "total": len(rows),
            "selected_issue_key": first or None,
            "selected_issue_keys": keys,
            "selected": first_selected,
            "selected_issues": selected_rows,
        }

    _write_json(zf, "states.json", _safe_call("states", _collect))


def _add_system_logs(zf: zipfile.ZipFile) -> None:
    rows = issue_log_ring.recent(limit=_MAX_SYSTEM_LINES)
    lines = []
    for row in rows:
        ts = row.get("timestamp") or ""
        msg = row.get("message") or ""
        lines.append(f"{ts}  {msg}".rstrip() if ts else msg)
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    if not text.strip():
        text = "(no in-process daemon lines in this session)\n"
    _write_text(zf, "system/daemon.log", text)

    durable = _read_capped_file(daemon_log_path())
    if durable:
        _write_text(zf, "system/daemon-file.log", durable)

    from src.paths import logs_dir

    seen_logs: set[str] = set()
    for folder in (logs_dir(), Path.cwd() / "logs"):
        try:
            key = str(folder.resolve())
        except OSError:
            key = str(folder)
        if key in seen_logs:
            continue
        seen_logs.add(key)
        _add_dir_logs(zf, folder, "system/logs")
    _add_job_system_log_files(zf)
    _add_opencode_cli_logs(zf)


def _add_dir_logs(zf: zipfile.ZipFile, logs_dir: Path, prefix: str) -> None:
    if not logs_dir.is_dir():
        return
    added = 0
    try:
        candidates = sorted(
            [p for p in logs_dir.iterdir() if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in candidates:
        if added >= _MAX_LOG_DIR_FILES:
            break
        if path.suffix.lower() not in {".log", ".txt"}:
            continue
        raw = _read_capped_file(path)
        if raw is None:
            continue
        _write_text(zf, f"{prefix}/{_safe_name(path.name)}", raw)
        added += 1


def _add_job_system_log_files(zf: zipfile.ZipFile) -> None:
    from src.paths import agent_subdir

    jobs_dir = agent_subdir("jobs")
    if not jobs_dir.is_dir():
        return
    added = 0
    try:
        files = sorted(
            jobs_dir.glob("job_*.system.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in files[:30]:
        raw = _read_capped_file(path)
        if raw is None:
            continue
        _write_text(zf, f"system/job-logs/{_safe_name(path.name)}", raw)
        added += 1
        if added >= 30:
            break


def _opencode_home_candidates() -> List[Path]:
    homes = [
        Path.home() / ".opencode",
        Path.home() / ".config" / "opencode",
    ]
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        homes.append(Path(local) / "opencode")
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    if xdg:
        homes.append(Path(xdg) / "opencode")
    return homes


def _opencode_log_roots() -> List[Path]:
    roots = [
        Path.home() / ".local" / "share" / "opencode" / "log",
        Path.home() / ".opencode" / "log",
        Path.home() / ".local" / "share" / "opencode" / "logs",
        Path.home() / ".opencode" / "logs",
    ]
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        roots.append(Path(local) / "opencode" / "log")
        roots.append(Path(local) / "opencode" / "logs")
    roaming = os.environ.get("APPDATA") or ""
    if roaming:
        roots.append(Path(roaming) / "opencode" / "log")
    xdg_data = os.environ.get("XDG_DATA_HOME") or ""
    if xdg_data:
        roots.append(Path(xdg_data) / "opencode" / "log")
    return roots


def _add_opencode_cli_logs(zf: zipfile.ZipFile) -> None:
    roots = _opencode_log_roots()
    added = 0
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            files = sorted(
                [p for p in root.iterdir() if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            continue
        for path in files:
            if added >= _MAX_OPENCODE_LOG_FILES:
                return
            key = str(path.resolve()) if path.exists() else str(path)
            if key in seen:
                continue
            seen.add(key)
            raw = _read_capped_file(path, max_bytes=_MAX_OPENCODE_LOG_BYTES)
            if raw is None:
                continue
            _write_text(
                zf,
                f"system/opencode-logs/{_safe_name(path.name)}",
                raw,
            )
            added += 1


def _add_job_bundle(
    zf: zipfile.ZipFile,
    job_item: Any,
    job_raw: Dict[str, Any],
    *,
    processor: Optional["JobProcessor"] = None,
    state_manager: Optional["JiraStateManager"] = None,
    prefix: str = "job",
) -> None:
    dumped = job_item.model_dump() if hasattr(job_item, "model_dump") else dict(job_item)
    _write_json(zf, f"{prefix}/record.json", dumped)
    _write_json(zf, f"{prefix}/parameters.json", _job_parameters(dumped, job_raw))
    desc = str(dumped.get("description") or job_raw.get("description") or "")
    _write_text(zf, f"{prefix}/description.txt", desc + ("\n" if desc else ""))
    _write_json(zf, f"{prefix}/retry_attempts.json", dumped.get("retry_attempts") or [])

    job_id = str(dumped.get("job_id") or "")
    job_lines = issue_log_ring.for_job(job_id, limit=_MAX_SYSTEM_LINES)
    if job_lines:
        body = "\n".join(
            f"{r.get('timestamp') or ''}  {r.get('message') or ''}".rstrip()
            for r in job_lines
        )
        _write_text(zf, f"{prefix}/system.log", body + "\n")
    else:
        disk = job_system_log_path(job_id)
        if disk is not None:
            raw = _read_capped_file(disk)
            if raw:
                _write_text(zf, f"{prefix}/system.log", raw)

    artifacts = collect_job_text_artifacts(dumped)
    _write_artifact_dir(zf, f"{prefix}/prompts", artifacts.get("prompts") or [])
    _write_artifact_dir(zf, f"{prefix}/session_logs", artifacts.get("session_logs") or [])

    seen = {
        str(a.get("path") or "")
        for a in (artifacts.get("prompts") or []) + (artifacts.get("session_logs") or [])
    }
    extra_prompts = [p for p in _job_prompt_paths(job_raw or dumped) if p not in seen]
    extra_logs = [p for p in _job_session_log_paths(job_raw or dumped) if p not in seen]
    for i, path_s in enumerate(extra_prompts, start=1):
        raw = _read_capped_file(Path(path_s))
        if raw is None:
            continue
        _write_text(
            zf,
            f"{prefix}/prompts/extra-{i:02d}-{_safe_name(Path(path_s).name)}",
            raw,
        )
    for i, path_s in enumerate(extra_logs, start=1):
        raw = _read_capped_file(Path(path_s))
        if raw is None:
            continue
        _write_text(
            zf,
            f"{prefix}/session_logs/extra-{i:02d}-{_safe_name(Path(path_s).name)}",
            raw,
        )

    chat = _safe_call("chat", lambda: collect_job_chat(dumped))
    chat_json = json.dumps(chat, indent=2, default=str, ensure_ascii=False)
    if len(chat_json) > _MAX_CHAT_JSON_CHARS:
        chat = {
            "job_id": dumped.get("job_id"),
            "session_ids": (chat or {}).get("session_ids") or [],
            "truncated": True,
            "error": f"chat JSON exceeded {_MAX_CHAT_JSON_CHARS} chars",
            "sessions": (chat or {}).get("sessions") or [],
            "messages": ((chat or {}).get("messages") or [])[:80],
        }
    _write_json(zf, f"{prefix}/chat.json", chat)
    _write_text(zf, f"{prefix}/chat.md", _chat_markdown(chat))

    def _issue() -> Any:
        key = str(dumped.get("issue_key") or "").strip()
        if not key:
            return {"error": "no issue_key on job"}
        return build_task_detail(
            key,
            state_manager=state_manager,
            processor=processor,
            include_artifacts=False,
            include_live_jira=processor is not None,
            jobs=[job_item],
        )

    _write_json(zf, f"{prefix}/issue.json", _safe_call("issue_detail", _issue))
    _add_job_plan(zf, dumped, state_manager=state_manager, prefix=prefix)
    _add_job_serve(zf, dumped, prefix=prefix)
    wd = dumped.get("working_directory") or ""
    if not str(wd).strip():
        try:
            from src.dashboard.service import _job_working_directory

            wd = _job_working_directory({**job_raw, **dumped})
        except Exception:
            wd = ""
    _write_text(zf, f"{prefix}/git.txt", _git_snapshot(wd, job=dumped, raw=job_raw))


def _job_parameters(job: Dict[str, Any], raw: Dict[str, Any]) -> Dict[str, Any]:
    """Issue {params} plus run-time job fields operators need to reproduce a run."""
    description = str(job.get("description") or raw.get("description") or "")
    spec_fields: Dict[str, Any] = {}
    spec_error: Optional[str] = None
    try:
        from src.issue_git_spec import parse_issue_git_spec

        spec, err = parse_issue_git_spec(str(job.get("summary") or ""), description)
        spec_error = err
        if spec is not None:
            spec_fields = {
                "repository_url": spec.repository_url,
                "source_branch": spec.source_branch,
                "target_branch": spec.target_branch,
                "mode": spec.mode,
                "model": spec.model,
                "backend": spec.backend,
            }
    except Exception as e:
        spec_error = str(e)

    retries = job.get("retry_attempts") or []
    return {
        "job_id": job.get("job_id"),
        "issue_key": job.get("issue_key"),
        "summary": job.get("summary"),
        "workflow_type": job.get("workflow_type"),
        "agent": job.get("agent"),
        "model": job.get("model"),
        "backend": job.get("backend"),
        "status": job.get("status"),
        "source": job.get("source"),
        "live": job.get("live"),
        "task_id": job.get("task_id"),
        "task_ids": job.get("task_ids") or [],
        "opencode_session_id": job.get("opencode_session_id"),
        "opencode_session_ids": job.get("opencode_session_ids") or [],
        "working_directory": job.get("working_directory"),
        "feature_branch": job.get("feature_branch"),
        "merge_request_url": job.get("merge_request_url"),
        "commit_sha": job.get("commit_sha"),
        "commit_subject": job.get("commit_subject"),
        "delivery_status": job.get("delivery_status"),
        "delivery_note": job.get("delivery_note"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "error_message": job.get("error_message"),
        "prompt_path": job.get("prompt_path"),
        "prompt_paths": job.get("prompt_paths") or [],
        "session_log_path": job.get("session_log_path"),
        "session_log_paths": job.get("session_log_paths") or [],
        "retry_count": len(retries) if isinstance(retries, list) else 0,
        "issue_params": spec_fields,
        "issue_params_error": spec_error,
    }


def _chat_markdown(chat: Any) -> str:
    if not isinstance(chat, dict):
        return str(chat or "")
    lines = [
        f"# Chat for {chat.get('job_id') or 'job'}",
        "",
        f"Sessions: {', '.join(chat.get('session_ids') or []) or '(none)'}",
        "",
    ]
    if chat.get("error"):
        lines.append(f"Error: {chat.get('error')}")
        lines.append("")
    for msg in chat.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or msg.get("raw_role") or "unknown"
        when = msg.get("created_at") or ""
        lines.append(f"## {role} {when}".rstrip())
        for part in msg.get("parts") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type") or ""
            if ptype == "text" or part.get("text"):
                text = str(part.get("text") or "")
                if len(text) > 20_000:
                    text = text[:20_000] + "\n…[truncated]…"
                lines.append(text)
            elif ptype == "tool" or part.get("tool"):
                lines.append(
                    f"- tool `{part.get('tool')}` status={part.get('status') or ''} "
                    f"{part.get('title') or ''}".rstrip()
                )
                out = part.get("output")
                if out:
                    chunk = str(out)
                    if len(chunk) > 4000:
                        chunk = chunk[:4000] + "\n…[truncated]…"
                    lines.append("```")
                    lines.append(chunk)
                    lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def _git_snapshot(
    working_directory: Any,
    *,
    job: Optional[Dict[str, Any]] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> str:
    wd = str(working_directory or "").strip()
    if not wd:
        return _git_missing_explanation(job or {}, raw or {}, path=None)
    root = Path(wd)
    if not root.is_dir():
        return _git_missing_explanation(job or {}, raw or {}, path=wd)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    chunks: List[str] = [f"directory: {wd}", ""]
    commands = (
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "rev-parse", "HEAD"],
        ["git", "status", "--short", "--branch"],
        ["git", "log", "-15", "--oneline", "--decorate"],
        ["git", "log", "-5", "--format=%H %s"],
        ["git", "remote", "-v"],
        ["git", "diff", "--stat", "HEAD"],
        ["git", "branch", "-vv"],
    )
    for cmd in commands:
        chunks.append("$ " + " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
                check=False,
            )
        except Exception as e:
            chunks.append(f"(failed: {e})")
            chunks.append("")
            continue
        out = (proc.stdout or "") + (proc.stderr or "")
        chunks.append(out.rstrip() or f"(exit {proc.returncode})")
        chunks.append("")
    return "\n".join(chunks) + "\n"


def _git_missing_explanation(
    job: Dict[str, Any],
    raw: Dict[str, Any],
    *,
    path: Optional[str],
) -> str:
    """Explain a missing clone — do not imply the product repo has no history."""
    params = _job_parameters(job or raw, raw or job)
    repo = ((params.get("issue_params") or {}) or {}).get("repository_url") or ""
    project_root = str(getattr(settings, "project_root", "sample_project") or "")
    started = job.get("started_at") or raw.get("started_at") or "?"
    completed = job.get("completed_at") or raw.get("completed_at") or "?"
    lines = [
        "No git snapshot is available for this job.",
        "",
        "This is not the Yaver repo and not sample_project/.",
        "Jira/GitLab jobs clone the Repository URL from the issue {params}",
        "into a temp folder under TEMP_DIR_BASE (C:\\vd\\t, /vd/t, or ~/vd/t).",
        f"sample_project/ (PROJECT_ROOT={project_root}) is only used by",
        "`cli.py test-issue`. It is never this job's working tree.",
        "",
    ]
    if path:
        lines.append(f"Recorded working_directory: {path}")
        lines.append("That folder is gone (deleted, moved, or already purged).")
    else:
        lines.append("This job record has no working_directory field.")
        lines.append("Older jobs did not persist the clone path.")
    lines.extend(
        [
            "",
            f"Issue repository from {{params}}: {repo or '(none parsed)'}",
            f"Job started: {started}",
            f"Job completed: {completed}",
            "Clones are not auto-deleted; remove them from dashboard Storage.",
            "",
            "Temp folders still on disk:",
        ]
    )
    from src.paths import resolve_temp_dir_base

    temp_base = resolve_temp_dir_base(getattr(settings, "temp_dir_base", None))
    if not temp_base.is_absolute():
        temp_base = Path.cwd() / temp_base
    leftover = []
    try:
        if temp_base.is_dir():
            leftover = sorted(
                [p for p in temp_base.iterdir() if p.is_dir()],
                key=lambda p: p.name,
            )
    except OSError:
        leftover = []
    if leftover:
        for p in leftover[:20]:
            lines.append(f"  - {p}")
    else:
        lines.append("  (none)")
    return "\n".join(lines) + "\n"


def _runtime_paths() -> Dict[str, str]:
    from src.paths import agent_data_dir, logs_dir, plans_dir, resolve_temp_dir_base

    return {
        "data_dir": str(agent_data_dir()),
        "temp_dir": str(resolve_temp_dir_base()),
        "plans_dir": str(plans_dir()),
        "logs_dir": str(logs_dir()),
        "daemon_log": str(daemon_log_path()),
    }


def _add_job_plan(
    zf: zipfile.ZipFile,
    dumped: Dict[str, Any],
    *,
    state_manager: Optional["JiraStateManager"] = None,
    prefix: str = "job",
) -> None:
    """Attach the durable plan markdown (YAVER_DATA_DIR/plans)."""
    from src.paths import plans_dir

    candidates: List[Path] = []
    key = str(dumped.get("issue_key") or "").strip()
    if key:
        candidates.append(plans_dir() / f"{key}.md")
    if state_manager is not None and key:
        try:
            st = state_manager.get_state(key)
        except Exception:
            st = None
        raw = (st.plan_path if st is not None else None) or ""
        if str(raw).strip():
            candidates.append(Path(str(raw)))
    seen: set[str] = set()
    for path in candidates:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        raw = _read_capped_file(path)
        if raw:
            _write_text(zf, f"{prefix}/plan.md", raw)
            return


def _cli_version(binary: str) -> Dict[str, Any]:
    path = shutil.which(binary) or binary
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {
            "path": path,
            "exit_code": proc.returncode,
            "output": text.splitlines()[0] if text else "",
        }
    except FileNotFoundError:
        return {"path": path, "error": "not found"}
    except Exception as e:
        return {"path": path, "error": str(e)}


def _probe_serve(base_url: str) -> Dict[str, Any]:
    return _serve_get(base_url, "/global/health")


def _serve_get(
    base_url: str,
    path: str,
    *,
    params: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    cache_key = url
    if params:
        cache_key += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    cached = _serve_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        with httpx.Client(verify=False, timeout=_SERVE_TIMEOUT) as client:
            resp = client.get(url, params=params)
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = (resp.text or "")[:2000]
        if isinstance(body, (list, dict)) and _json_size(body) > 400_000:
            if isinstance(body, list):
                body = body[:40]
            elif isinstance(body, dict) and "messages" in body:
                body = {**body, "messages": (body.get("messages") or [])[:30]}
        result = {"url": url, "http_status": resp.status_code, "body": body}
        _serve_cache[cache_key] = result
        return result
    except Exception as e:
        result = {"url": url, "error": str(e)}
        _serve_cache[cache_key] = result
        return result


def _json_size(payload: Any) -> int:
    try:
        return len(json.dumps(payload, default=str))
    except Exception:
        return 0


def _serve_provider_ids(base_url: str) -> Dict[str, Any]:
    from src.opencode_serve import known_model_ids_from_payload

    out: Dict[str, Any] = {"models": []}
    for path in ("/config/providers", "/provider"):
        hit = _serve_get(base_url, path)
        out[path] = {"http_status": hit.get("http_status"), "error": hit.get("error")}
        body = hit.get("body")
        if isinstance(body, (dict, list)):
            try:
                ids = known_model_ids_from_payload(body)
            except Exception:
                ids = []
            if ids:
                out["models"] = ids
                break
    return out


def _serve_safe_config(base_url: str) -> Dict[str, Any]:
    hit = _serve_get(base_url, "/config")
    body = hit.get("body")
    if not isinstance(body, dict):
        return hit
    safe = {
        "url": hit.get("url"),
        "http_status": hit.get("http_status"),
        "autoupdate": body.get("autoupdate"),
        "plugin": body.get("plugin"),
        "model": body.get("model"),
        "keys": [k for k in body.keys() if k not in {"provider", "mcp", "token"}],
    }
    return safe


def _product_version() -> str:
    try:
        from src.install_paths import resource_root

        path = resource_root() / "VERSION"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    try:
        return Path("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _add_job_serve(
    zf: zipfile.ZipFile, dumped: Dict[str, Any], *, prefix: str = "job"
) -> None:
    serve_url = (
        getattr(settings, "opencode_serve_url", None) or "http://127.0.0.1:4096"
    ).rstrip("/")
    sids = list(dumped.get("opencode_session_ids") or [])
    sid = str(dumped.get("opencode_session_id") or "").strip()
    if sid and sid not in sids:
        sids.insert(0, sid)
    payload: Dict[str, Any] = {
        "base_url": serve_url,
        "health": _serve_get(serve_url, "/global/health"),
        "session_status": _serve_get(serve_url, "/session/status"),
        "sessions": {},
    }
    for one in sids[:8]:
        payload["sessions"][one] = {
            "todos": _serve_get(serve_url, f"/session/{one}/todo"),
            "messages_tail": _serve_get(
                serve_url, f"/session/{one}/message", params={"limit": "40"}
            ),
        }
    _write_json(zf, f"{prefix}/serve.json", payload)


def _safe_call(name: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as e:
        logger.debug(f"Issue report skipped {name}: {e}")
        return {"error": f"{name}: {e}"}


def _write_artifact_dir(
    zf: zipfile.ZipFile,
    folder: str,
    artifacts: Iterable[Dict[str, Any]],
) -> None:
    for i, art in enumerate(artifacts, start=1):
        name = _safe_name(str(art.get("name") or Path(str(art.get("path") or "file")).name))
        header_bits = [
            f"path: {art.get('path') or ''}",
            f"truncated: {bool(art.get('truncated'))}",
        ]
        if art.get("error"):
            header_bits.append(f"error: {art.get('error')}")
        body = art.get("content") or ""
        text = "\n".join(header_bits) + "\n\n" + str(body)
        if not str(body) and art.get("error"):
            text += "\n"
        _write_text(zf, f"{folder}/{i:02d}-{name}", text)


def _read_capped_file(
    path: Path, *, max_bytes: int = _MAX_FILE_BYTES
) -> Optional[str]:
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    truncated = len(data) > max_bytes
    if truncated:
        data = data[-max_bytes:]
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text = f"[truncated to last {max_bytes} bytes]\n" + text
    return text


def _write_text(zf: zipfile.ZipFile, name: str, text: str) -> None:
    zf.writestr(name, _redact_report_text(text or ""))


def _write_json(zf: zipfile.ZipFile, name: str, payload: Any) -> None:
    raw = json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    _write_text(zf, name, raw + "\n")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "")
    return cleaned.strip("._") or "file"


def _redact_report_text(text: str) -> str:
    """Strip known tokens and common PAT shapes. Never ship secrets in a zip."""
    if not text:
        return text
    secrets: List[str] = []
    for attr in (
        "jira_api_token",
        "gitlab_pat",
        "gitlab_webhook_secret",
        "azure_pat",
        "azure_webhook_secret",
    ):
        val = str(getattr(settings, attr, "") or "").strip()
        if val:
            secrets.append(val)
    try:
        extra = settings.all_gitlab_pats() if hasattr(settings, "all_gitlab_pats") else []
        secrets.extend([str(p) for p in extra if p])
        extra_az = (
            settings.all_azure_pats() if hasattr(settings, "all_azure_pats") else []
        )
        secrets.extend([str(p) for p in extra_az if p])
    except Exception:
        pass
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(
        r"(https?://)([^/@\s]+):([^@/\s]+)@",
        r"\1\2:***@",
        text,
    )
    text = _GLPAT.sub("glpat-***", text)
    text = _BEARER.sub(r"\1***", text)
    text = _BASIC.sub(r"\1***", text)
    return text
