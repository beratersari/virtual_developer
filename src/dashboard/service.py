"""Dashboard business assembly: tasks, poll view, safe settings."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.config import settings
from src.dashboard.issue_logs import issue_log_ring
from src.dashboard.schemas import (
    JobItem,
    JobsResponse,
    MetaResponse,
    ModelOption,
    ModelsResponse,
    PolledIssueItem,
    PollStatusResponse,
    SettingsUpdate,
    SettingsView,
    TaskItem,
    TasksResponse,
)
from src.opencode_models import list_available_models
from src.dashboard.snapshot import PollSnapshotStore, poll_snapshot_store
from src.opencode_sessions import find_sessions_for_issue
from src.orchestrator.workflow_router import WorkflowType
from src.state.job_store import (
    JobStore,
    description_from_prompt_path,
    job_store as default_job_store,
)
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus

if TYPE_CHECKING:
    from src.processor import JobProcessor

# Cap large payloads for API safety
_MAX_SESSION_CHARS = 400_000
_MAX_PROMPT_CHARS = 200_000
_MAX_SESSION_LOG_FILES = 5
_MAX_PROMPT_FILES = 5


def read_app_version() -> str:
    """Read SemVer from repo VERSION file."""
    candidates = [
        Path.cwd() / "VERSION",
        Path(__file__).resolve().parents[2] / "VERSION",
    ]
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip() or "0.0.0"
        except OSError:
            continue
    return "0.0.0"


def build_meta() -> MetaResponse:
    return MetaResponse(
        version=read_app_version(),
        server_time=datetime.now().isoformat(timespec="seconds"),
    )


def build_tasks(
    state_manager: Optional[JiraStateManager] = None,
    processor: Optional["JobProcessor"] = None,
) -> TasksResponse:
    sm = state_manager or JiraStateManager()
    live_keys = set()
    if processor is not None:
        live_keys = set(processor.list_live_processing_keys())

    items: List[TaskItem] = []
    for st in sm.get_all_states():
        meta = st.metadata or {}
        session_ids = list(meta.get("opencode_session_ids") or [])
        current_sid = st.current_opencode_session_id
        # While in-flight, do not fall back to previous run's session (stale UI)
        in_flight = st.status in (TaskStatus.PLANNING, TaskStatus.EXECUTING)
        if not current_sid and not in_flight:
            current_sid = meta.get("last_opencode_session_id")
        if current_sid and current_sid not in session_ids:
            session_ids = [*session_ids, current_sid]
        # Backfill from OpenCode DB only when not mid-run
        if not current_sid and not in_flight:
            found = find_sessions_for_issue(st.issue_key, limit=1)
            if found:
                current_sid = found[0]["id"]
                if current_sid not in session_ids:
                    session_ids.append(current_sid)
        task_id = st.current_task_id
        if not task_id and not in_flight:
            task_id = meta.get("last_task_id")
        items.append(
            TaskItem(
                issue_key=st.issue_key,
                summary=st.issue_summary or "",
                status=st.status.value,
                progress_percentage=int(st.progress_percentage or 0),
                workflow_type=meta.get("workflow_type"),
                jira_assignee=st.jira_assignee,
                error_message=st.error_message,
                started_at=st.started_at.isoformat(timespec="seconds") if st.started_at else None,
                completed_at=(
                    st.completed_at.isoformat(timespec="seconds") if st.completed_at else None
                ),
                feature_branch=meta.get("feature_branch"),
                merge_request_url=meta.get("merge_request_url"),
                live=st.issue_key in live_keys,
                task_id=task_id,
                opencode_session_id=current_sid,
                opencode_session_ids=session_ids,
            )
        )

    # Active first, then by key
    order = {
        "planning": 0,
        "executing": 1,
        "pending": 2,
        "plan_ready": 3,
        "error": 4,
        "cancelled": 5,
        "completed": 6,
    }
    items.sort(key=lambda t: (order.get(t.status, 9), t.issue_key))
    return TasksResponse(
        tasks=items,
        total=len(items),
        server_time=datetime.now().isoformat(timespec="seconds"),
    )


def build_poll_status(
    store: Optional[PollSnapshotStore] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> PollStatusResponse:
    store = store or poll_snapshot_store
    sm = state_manager or JiraStateManager()
    raw = store.snapshot()

    issues: List[PolledIssueItem] = []
    for row in raw.get("issues") or []:
        # Ops list: only issues the Virtual Developer can act on (trigger
        # label and/or bot assignee). Full board rows stay in the raw
        # snapshot for counts / debug; UI must not show noise.
        matched_label = bool(row.get("matched_label"))
        matched_assignee = bool(row.get("matched_assignee"))
        if not (matched_label or matched_assignee):
            continue
        key = row.get("key") or ""
        local = sm.get_state(key) if key else None
        issues.append(
            PolledIssueItem(
                key=key,
                summary=row.get("summary") or "",
                jira_status=row.get("jira_status") or "",
                labels=list(row.get("labels") or []),
                assignee=row.get("assignee"),
                matched_label=matched_label,
                matched_assignee=matched_assignee,
                is_todo=bool(row.get("is_todo")),
                will_process=bool(row.get("will_process")),
                local_status=local.status.value if local else row.get("local_status"),
                matched_labels=list(row.get("matched_labels") or []),
            )
        )

    return PollStatusResponse(
        phase=raw.get("phase") or "idle",
        last_poll_at=raw.get("last_poll_at"),
        next_poll_at=raw.get("next_poll_at"),
        seconds_until_next_poll=raw.get("seconds_until_next_poll"),
        poll_interval_seconds=int(
            raw.get("poll_interval_seconds") or settings.poll_interval_seconds or 30
        ),
        source=raw.get("source"),
        board_id=raw.get("board_id") or settings.jira_board_id or None,
        issues=issues,
        matched_count=int(raw.get("matched_count") or 0),
        will_process_count=int(raw.get("will_process_count") or 0),
        error=raw.get("error"),
        cycle=int(raw.get("cycle") or 0),
        server_time=raw.get("server_time")
        or datetime.now().isoformat(timespec="seconds"),
    )


def build_settings_view() -> SettingsView:
    """Safe settings projection. Does not inventory OpenCode models (see build_models_response)."""
    return SettingsView(
        jira_host=settings.jira_host or "",
        jira_board_id=settings.jira_board_id or "",
        jira_projects=settings.jira_projects or "",
        poll_interval_seconds=int(settings.poll_interval_seconds or 30),
        trigger_labels=settings.trigger_labels or "",
        trigger_on_assignment=bool(settings.trigger_on_assignment),
        auto_start_plans=bool(settings.auto_start_plans),
        max_concurrent_jobs=int(settings.max_concurrent_jobs or 1),
        default_branch="(from Jira issue)",
        dashboard_host=getattr(settings, "dashboard_host", "127.0.0.1") or "127.0.0.1",
        dashboard_port=int(getattr(settings, "dashboard_port", 8080) or 8080),
        jira_token_configured=bool(settings.jira_api_token),
        gitlab_pat_configured=bool(settings.gitlab_pat),
        jira_email_configured=bool(getattr(settings, "jira_email", "") or ""),
        default_model=(settings.default_model or "").strip(),
    )


def build_models_response(*, refresh: bool = False) -> ModelsResponse:
    """Inventory OpenCode models (CLI + opencode.json). Backend-only business logic."""
    models, models_err, cfg_path, cfg_model = list_available_models(refresh=refresh)
    options: List[ModelOption] = []
    for m in models:
        mid = m.id
        name = m.name or mid
        # Prefer human label from inventory; always include source hint for config rows
        if name and name != mid and name != mid.split("/")[-1]:
            label = f"{mid} — {name}"
        else:
            label = mid
        if m.source in ("config", "config_default"):
            label = f"{label} · config"
        options.append(
            ModelOption(
                id=mid,
                name=name,
                provider=m.provider or "",
                source=m.source,
                label=label,
            )
        )
    return ModelsResponse(
        default_model=(settings.default_model or "").strip(),
        models=options,
        opencode_config_model=cfg_model,
        opencode_config_path=cfg_path,
        error=models_err,
        server_time=datetime.now().isoformat(timespec="seconds"),
    )


def apply_settings_update(body: SettingsUpdate) -> SettingsView:
    """Apply non-secret runtime settings. Does not rewrite .env."""
    data = body.model_dump(exclude_unset=True)
    if "jira_board_id" in data and data["jira_board_id"] is not None:
        settings.jira_board_id = str(data["jira_board_id"]).strip()
    if "poll_interval_seconds" in data and data["poll_interval_seconds"] is not None:
        settings.poll_interval_seconds = int(data["poll_interval_seconds"])
    if "trigger_labels" in data and data["trigger_labels"] is not None:
        settings.trigger_labels = str(data["trigger_labels"])
    if "trigger_on_assignment" in data and data["trigger_on_assignment"] is not None:
        settings.trigger_on_assignment = bool(data["trigger_on_assignment"])
    if "auto_start_plans" in data and data["auto_start_plans"] is not None:
        settings.auto_start_plans = bool(data["auto_start_plans"])
    if "max_concurrent_jobs" in data and data["max_concurrent_jobs"] is not None:
        settings.max_concurrent_jobs = int(data["max_concurrent_jobs"])
    if "default_model" in data and data["default_model"] is not None:
        model = str(data["default_model"]).strip()
        if model:
            settings.default_model = model
    return build_settings_view()


def _parse_session_log_name(name: str) -> Optional[tuple]:
    """Parse ISSUE_YYYYMMDD_HHMMSS_n.log → (issue_key, started_at iso)."""
    m = re.match(
        r"^([A-Z][A-Z0-9]+-\d+)_(\d{8})_(\d{6})_\d+\.log$",
        name,
        re.IGNORECASE,
    )
    if not m:
        return None
    key = m.group(1).upper()
    ymd, hms = m.group(2), m.group(3)
    started = f"{ymd[0:4]}-{ymd[4:6]}-{ymd[6:8]}T{hms[0:2]}:{hms[2:4]}:{hms[4:6]}"
    return key, started


def _legacy_jobs_from_sessions(
    *,
    issue_key: Optional[str] = None,
    covered_paths: set,
    summaries: Dict[str, str],
    limit: int = 200,
    suppress_logs_after: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Synthesize job rows from session logs not already linked to a stored job.

    Lets the dashboard show historical runs that finished before JobStore existed.

    ``suppress_logs_after`` maps issue_key → job started_at ISO. Session logs for
    that issue with started_at >= cutoff are skipped so an in-flight JobStore row
    is not duplicated as a fake ``legacy_*`` completed job while the agent runs
    (session file exists before ``session_log_path`` is written on the job).
    """
    sessions_dir = _sessions_dir()
    if not sessions_dir.is_dir():
        return []
    needle = (issue_key or "").strip().upper()
    suppress = {k.upper(): v for k, v in (suppress_logs_after or {}).items()}
    out: List[Dict[str, Any]] = []
    paths = (
        sorted(sessions_dir.glob(f"{needle}_*.log"), key=lambda p: p.name, reverse=True)
        if needle
        else sorted(sessions_dir.glob("*.log"), key=lambda p: p.name, reverse=True)
    )
    for path in paths:
        if path.name.endswith(".prompt.txt") or not path.is_file():
            continue
        if path.suffix != ".log":
            continue
        resolved = str(path.resolve())
        # Also match by basename in case absolute paths differ
        if resolved in covered_paths or str(path) in covered_paths:
            continue
        # Basename coverage (job may store relative path)
        if path.name in covered_paths or path.stem in covered_paths:
            continue
        parsed = _parse_session_log_name(path.name)
        if not parsed:
            continue
        ik, started = parsed
        if needle and ik != needle:
            continue
        cutoff = suppress.get(ik)
        if cutoff is not None:
            # Log timestamp from filename vs job start — suppress overlap window
            if not cutoff or started >= cutoff[:19]:
                continue
        sid = None
        sid_file = path.with_suffix(path.suffix + ".session_id")
        if sid_file.is_file():
            try:
                sid = sid_file.read_text(encoding="utf-8").strip() or None
            except OSError:
                sid = None
        prompt = path.with_suffix(".prompt.txt")
        # Prefer sibling named like foo.prompt.txt next to foo.log
        prompt_alt = Path(str(path) + ".prompt.txt")  # unlikely
        prompt_path = None
        for candidate in (
            path.parent / f"{path.stem}.prompt.txt",
            prompt,
            prompt_alt,
        ):
            if candidate.is_file():
                prompt_path = str(candidate)
                break
        desc = description_from_prompt_path(prompt_path) if prompt_path else ""
        out.append(
            {
                "job_id": f"legacy_{path.stem}",
                "issue_key": ik,
                "summary": summaries.get(ik, ""),
                "description": desc,
                "workflow_type": "direct",
                "agent": "",
                "status": "completed",
                "task_id": None,
                "opencode_session_id": sid,
                "opencode_session_ids": [sid] if sid else [],
                "session_log_path": resolved,
                "prompt_path": prompt_path,
                "progress_percentage": 100,
                "error_message": None,
                "started_at": started,
                "completed_at": started,
                "updated_at": started,
            }
        )
        if len(out) >= limit:
            break
    return out


def build_jobs(
    *,
    issue_key: Optional[str] = None,
    limit: int = 200,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> JobsResponse:
    js = store or default_job_store
    live_keys = set()
    active_job_ids = set()
    if processor is not None:
        live_keys = set(processor.list_live_processing_keys())
        active_job_ids = set((processor._active_jobs or {}).values())

    summaries: Dict[str, str] = {}
    if state_manager is not None:
        for st in state_manager.get_all_states():
            summaries[st.issue_key] = st.issue_summary or ""

    raw = js.list_jobs(issue_key=issue_key, limit=limit)
    covered_paths: set = set()
    # Open JobStore runs without session_log_path yet — suppress matching session logs
    suppress_logs_after: Dict[str, str] = {}
    for j in raw:
        for key in ("session_log_path", "prompt_path"):
            p = j.get(key)
            if p:
                covered_paths.add(str(p))
                try:
                    pp = Path(p)
                    covered_paths.add(str(pp.resolve()))
                    covered_paths.add(pp.name)
                    covered_paths.add(pp.stem)
                    # Log sibling of .prompt.txt
                    if pp.name.endswith(".prompt.txt"):
                        covered_paths.add(pp.name[: -len(".prompt.txt")] + ".log")
                        covered_paths.add(pp.stem.replace(".prompt", ""))
                except Exception:
                    pass
        st = (j.get("status") or "").lower()
        if st in ("running", "planning", "executing") and not j.get("session_log_path"):
            ik = (j.get("issue_key") or "").upper()
            started = j.get("started_at") or ""
            if ik and (ik not in suppress_logs_after or started < suppress_logs_after[ik]):
                suppress_logs_after[ik] = started

    # Merge stored jobs with legacy session-derived rows (newest first after merge)
    remaining = max(1, limit) - len(raw)
    if remaining > 0:
        legacy = _legacy_jobs_from_sessions(
            issue_key=issue_key,
            covered_paths=covered_paths,
            summaries=summaries,
            limit=remaining,
            suppress_logs_after=suppress_logs_after,
        )
        raw = list(raw) + legacy
        raw.sort(
            key=lambda j: j.get("started_at") or j.get("updated_at") or "",
            reverse=True,
        )
        raw = raw[: max(1, limit)]

    items: List[JobItem] = []
    for j in raw:
        jid = j.get("job_id") or ""
        ik = j.get("issue_key") or ""
        if not j.get("summary") and ik in summaries:
            j = {**j, "summary": summaries[ik]}
        # Recover description from this job's prompt — never from live issue state
        j = js.ensure_description(j, persist=jid.startswith("job_"))
        if not (j.get("description") or "").strip() and j.get("prompt_path"):
            recovered = description_from_prompt_path(j.get("prompt_path"))
            if recovered:
                j = {**j, "description": recovered}
        live = jid in active_job_ids or (
            ik in live_keys and (j.get("status") or "") in ("running", "planning", "executing")
        )
        items.append(
            JobItem(
                job_id=jid,
                issue_key=ik,
                summary=j.get("summary") or "",
                # Per-job snapshot only (or recovered from that job's prompt file)
                description=j.get("description") or "",
                workflow_type=j.get("workflow_type") or "direct",
                agent=j.get("agent") or "",
                status=j.get("status") or "unknown",
                task_id=j.get("task_id"),
                task_ids=list(j.get("task_ids") or ([j["task_id"]] if j.get("task_id") else [])),
                opencode_session_id=j.get("opencode_session_id"),
                opencode_session_ids=list(j.get("opencode_session_ids") or []),
                session_log_path=j.get("session_log_path"),
                prompt_path=j.get("prompt_path"),
                progress_percentage=int(j.get("progress_percentage") or 0),
                error_message=j.get("error_message"),
                started_at=j.get("started_at"),
                completed_at=j.get("completed_at"),
                updated_at=j.get("updated_at"),
                live=live,
            )
        )
    return JobsResponse(
        jobs=items,
        total=len(items),
        issue_key_filter=(issue_key or None),
        server_time=datetime.now().isoformat(timespec="seconds"),
    )


def build_dashboard_payload(
    *,
    state_manager: Optional[JiraStateManager] = None,
    processor: Optional["JobProcessor"] = None,
    store: Optional[PollSnapshotStore] = None,
    issue_key_filter: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "type": "dashboard",
        "meta": build_meta().model_dump(),
        "tasks": build_tasks(state_manager, processor).model_dump(),
        "jobs": build_jobs(
            issue_key=issue_key_filter,
            processor=processor,
            state_manager=state_manager,
        ).model_dump(),
        "poll": build_poll_status(store, state_manager).model_dump(),
        "settings": build_settings_view().model_dump(),
    }


def _sessions_dir() -> Path:
    """Session logs root (same default as AgentRunner; tests may patch)."""
    try:
        from src.orchestrator.agent_runner import _default_sessions_dir

        return _default_sessions_dir()
    except Exception:
        return (Path.cwd() / ".jira-agent" / "sessions").resolve()


def _path_under(root: Path, path: Path) -> bool:
    """True if path resolves under root (blocks symlink escape)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _read_text_capped(path: Path, max_chars: int, *, root: Optional[Path] = None) -> Dict[str, Any]:
    if root is not None and not _path_under(root, path):
        return {
            "path": str(path),
            "error": "path outside allowed directory",
            "content": "",
            "truncated": False,
        }
    # Refuse to follow symlinks outside root: open only regular files after resolve check
    try:
        if path.is_symlink():
            resolved = path.resolve()
            if root is not None and not _path_under(root, resolved):
                return {
                    "path": str(path),
                    "error": "symlink escape blocked",
                    "content": "",
                    "truncated": False,
                }
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"path": str(path), "error": str(e), "content": "", "truncated": False}
    truncated = len(text) > max_chars
    if truncated:
        text = text[-max_chars:]
    return {
        "path": str(path),
        "name": path.name,
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "content": text,
        "truncated": truncated,
        "error": None,
    }


def _collect_session_artifacts(issue_key: str) -> Dict[str, Any]:
    """Find OpenCode session logs and sibling prompt files for an issue."""
    sessions_dir = _sessions_dir()
    logs: List[Dict[str, Any]] = []
    prompts: List[Dict[str, Any]] = []
    if not sessions_dir.is_dir():
        return {"session_logs": logs, "prompt_files": prompts}

    # Escape glob metacharacters in issue_key (never use raw user key as glob)
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", (issue_key or "").strip())
    if not safe_key:
        return {"session_logs": logs, "prompt_files": prompts}

    candidates = sorted(
        sessions_dir.glob(f"{safe_key}_*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if not _path_under(sessions_dir, path):
            continue
        if path.suffix == ".log" and len(logs) < _MAX_SESSION_LOG_FILES:
            logs.append(_read_text_capped(path, _MAX_SESSION_CHARS, root=sessions_dir))
        elif (
            path.name.endswith(".prompt.txt")
            or (len(path.suffixes) >= 2 and path.suffixes[-2:] == [".prompt", ".txt"])
            or (path.suffix == ".txt" and ".prompt" in path.name)
        ) and len(prompts) < _MAX_PROMPT_FILES:
            prompts.append(_read_text_capped(path, _MAX_PROMPT_CHARS, root=sessions_dir))

    return {"session_logs": logs, "prompt_files": prompts}


def _reconstruct_prompts(state) -> Dict[str, Any]:
    """Metadata for the prompts tab (agent/workflow only).

    The UI shows captured ``*.prompt.txt`` files — the exact text sent to the
    agent. We do not rebuild a live “assembled” prompt for display.
    """
    workflow = (state.metadata or {}).get("workflow_type") or "direct"
    agent_name = settings.default_agent
    if workflow == WorkflowType.PLANNING.value or workflow == "planning":
        agent_name = settings.planning_agent
    elif workflow == WorkflowType.ORACLE_CONSULT.value or workflow == "oracle":
        agent_name = "oracle"
    elif workflow == "execution" or (
        state.status == TaskStatus.EXECUTING
        and state.plan_path
        and workflow == WorkflowType.PLANNING.value
    ):
        agent_name = settings.orchestrator_agent
    elif state.plan_path and state.status in (
        TaskStatus.EXECUTING,
        TaskStatus.PLAN_READY,
        TaskStatus.COMPLETED,
    ):
        agent_name = settings.orchestrator_agent

    return {
        "workflow_type": workflow,
        "agent": agent_name,
    }


def _jira_plain_text(value: Any) -> str:
    """Normalize Jira description (plain string or ADF document) to text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        # Atlassian Document Format — walk content nodes for text
        parts: List[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text" and isinstance(node.get("text"), str):
                    parts.append(node["text"])
                for child in node.get("content") or []:
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(value)
        return "\n".join(parts).strip() if parts else str(value)
    return str(value)


def _fetch_live_jira_fields(
    issue_key: str,
    *,
    processor: Optional["JobProcessor"] = None,
) -> Dict[str, Any]:
    """Best-effort live summary/description/status from Jira REST.

    Returns empty dict on failure so the dashboard still works offline.
    """
    client = None
    if processor is not None:
        client = getattr(processor, "jira_client", None)
    if client is None or not hasattr(client, "get_issue"):
        try:
            from src.jira.client import create_jira_client

            client = create_jira_client()
        except Exception as e:
            from src.logger import logger

            logger.debug(f"Live Jira client unavailable for {issue_key}: {e}")
            return {}
    try:
        issue = client.get_issue(issue_key)
    except Exception as e:
        from src.logger import logger

        logger.debug(f"Live Jira fetch failed for {issue_key}: {e}")
        return {}
    if not issue:
        return {}
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    return {
        "summary": (fields.get("summary") or "").strip(),
        "description": _jira_plain_text(fields.get("description")),
        "jira_status": (status.get("name") or "").strip(),
    }


def build_task_detail(
    issue_key: str,
    *,
    state_manager: Optional[JiraStateManager] = None,
    processor: Optional["JobProcessor"] = None,
) -> Optional[Dict[str, Any]]:
    """Full task detail for dashboard (prompts, sessions, logs, cancel eligibility)."""
    sm = state_manager or JiraStateManager()
    state = sm.get_state(issue_key)
    if not state:
        return None

    live = False
    if processor is not None:
        live = processor._is_live_processing(issue_key)

    # Live fields from Jira (not frozen local state / job snapshot)
    jira_live = _fetch_live_jira_fields(issue_key, processor=processor)
    if jira_live:
        live_summary = jira_live.get("summary") or state.issue_summary or ""
        live_description = jira_live.get("description", "")
    else:
        live_summary = state.issue_summary or ""
        live_description = state.description or ""

    artifacts = _collect_session_artifacts(issue_key)
    prompts = _reconstruct_prompts(state)
    # Prefer on-disk prompt captures when present (actual text sent to agent)
    if artifacts["prompt_files"]:
        prompts["captured_prompt_files"] = artifacts["prompt_files"]
    else:
        prompts["captured_prompt_files"] = []

    retry_history = []
    for attempt in state.retry_history or []:
        retry_history.append(attempt.to_dict() if hasattr(attempt, "to_dict") else dict(attempt))

    can_cancel = state.status not in {
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
        TaskStatus.CANCELLED,
    }
    # plan_ready needs an operator kick when auto_start_plans is false
    # (comments are not polled — AGENTS.md)
    can_start = state.status == TaskStatus.PLAN_READY and not live

    meta = state.metadata or {}
    session_ids = list(meta.get("opencode_session_ids") or [])
    current_sid = (
        state.current_opencode_session_id
        or meta.get("last_opencode_session_id")
    )
    if current_sid and current_sid not in session_ids:
        session_ids.append(current_sid)
    display_task_id = state.current_task_id or meta.get("last_task_id")
    task_ids = list(meta.get("task_ids") or [])
    if display_task_id and display_task_id not in task_ids:
        task_ids = [*task_ids, display_task_id]
    job_ids = list(meta.get("job_ids") or [])
    if meta.get("current_job_id") and meta["current_job_id"] not in job_ids:
        job_ids = [*job_ids, meta["current_job_id"]]
    # Sibling .session_id files next to session logs
    for log in artifacts["session_logs"]:
        sid_file = Path(log["path"] + ".session_id")
        if sid_file.is_file():
            try:
                sid = sid_file.read_text(encoding="utf-8").strip()
                if sid and sid not in session_ids:
                    session_ids.append(sid)
                if not current_sid:
                    current_sid = sid
            except OSError:
                pass
    db_sessions = find_sessions_for_issue(issue_key, limit=20)
    for s in db_sessions:
        sid = s.get("id")
        if sid and sid not in session_ids:
            session_ids.append(sid)
    if not current_sid and session_ids:
        current_sid = session_ids[-1]

    return {
        "issue_key": state.issue_key,
        "summary": live_summary,
        "description": live_description,
        "jira_status": jira_live.get("jira_status") or None,
        "jira_live": bool(jira_live),
        "status": state.status.value,
        "progress_percentage": int(state.progress_percentage or 0),
        "live": live,
        "can_cancel": can_cancel,
        "can_start": can_start,
        "workflow_type": meta.get("workflow_type"),
        "plan_path": state.plan_path,
        "current_task_id": display_task_id,
        "current_opencode_session_id": current_sid,
        "task_ids": task_ids,
        "job_ids": job_ids,
        "current_job_id": meta.get("current_job_id"),
        "opencode_session_ids": session_ids,
        "opencode_sessions": db_sessions,
        "error_message": state.error_message,
        "started_at": state.started_at.isoformat(timespec="seconds") if state.started_at else None,
        "completed_at": (
            state.completed_at.isoformat(timespec="seconds") if state.completed_at else None
        ),
        "feature_branch": meta.get("feature_branch"),
        "merge_request_url": meta.get("merge_request_url"),
        "retry_history": retry_history,
        "prompts": prompts,
        "session_logs": artifacts["session_logs"],
        "system_logs": issue_log_ring.for_issue(issue_key, limit=500),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }
