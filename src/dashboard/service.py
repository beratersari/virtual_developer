"""Dashboard business assembly: tasks, poll view, safe settings."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.config import save_runtime_settings, settings, upsert_dotenv_keys
from src.dashboard.issue_logs import issue_log_ring
from src.logger import logger
from src.dashboard.project_repos import (
    parse_project_repositories,
    project_repositories_to_json,
)
from src.dashboard.schemas import (
    GitDeliveryItem,
    GitlabHostCredentialView,
    JobItem,
    JobRetryAttempt,
    JobsResponse,
    MetaResponse,
    ModelOption,
    ModelsResponse,
    PolledIssueItem,
    PollStatusResponse,
    ProjectRepositoryItem,
    SettingsUpdate,
    SettingsView,
    TaskItem,
    TasksResponse,
    QueueItem,
    QueueResponse,
)
from src.opencode_models import list_available_models
from src.dashboard.snapshot import PollSnapshotStore, poll_snapshot_store
from src.opencode_sessions import (
    extract_session_ids_from_text,
    find_sessions_for_issue,
    list_session_chat,
    strip_internal_markup,
    strip_omo_mode_wrap,
    is_omo_mode_wrap_text,
)
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
# Higher than one-run: initial + several _retryN session logs per job
_MAX_SESSION_LOG_FILES = 20
_MAX_PROMPT_FILES = 20


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
        if not (matched_label or matched_assignee or row.get("will_process")):
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


def _settings_project_repositories() -> List[ProjectRepositoryItem]:
    raw = getattr(settings, "project_repositories", "") or ""
    return [ProjectRepositoryItem(**item) for item in parse_project_repositories(raw)]


def build_settings_view() -> SettingsView:
    """Safe settings projection. Does not inventory OpenCode models (see build_models_response).

    Never includes ``jira_api_token`` or ``gitlab_pat`` values — only booleans.
    """
    return SettingsView(
        jira_host=settings.jira_host or "",
        jira_board_id=settings.jira_board_id or "",
        jira_projects=settings.jira_projects or "",
        poll_interval_seconds=int(settings.poll_interval_seconds or 30),
        trigger_labels=settings.trigger_labels or "",
        trigger_on_assignment=bool(settings.trigger_on_assignment),
        max_concurrent_jobs=int(settings.max_concurrent_jobs or 1),
        agent_task_timeout_seconds=int(
            getattr(settings, "agent_task_timeout_seconds", 1800) or 1800
        ),
        agent_task_max_retries=int(
            getattr(settings, "agent_task_max_retries", 3) or 0
        ),
        agent_task_max_incomplete_retries=int(
            getattr(settings, "agent_task_max_incomplete_retries", 256) or 0
        ),
        opencode_serve_max_compact_continues=int(
            getattr(settings, "opencode_serve_max_compact_continues", 256) or 0
        ),
        default_branch="(from Jira issue)",
        dashboard_host=getattr(settings, "dashboard_host", "127.0.0.1") or "127.0.0.1",
        dashboard_port=int(getattr(settings, "dashboard_port", 8080) or 8080),
        jira_token_configured=bool((settings.jira_api_token or "").strip()),
        gitlab_pat_configured=bool(
            settings.gitlab_has_any_pat()
            if hasattr(settings, "gitlab_has_any_pat")
            else (settings.gitlab_pat or "").strip()
        ),
        jira_email_configured=bool((getattr(settings, "jira_email", "") or "").strip()),
        jira_email=(getattr(settings, "jira_email", "") or "").strip(),
        gitlab_allowed_hosts=",".join(settings.gitlab_allowed_hosts_list),
        gitlab_credentials=[
            GitlabHostCredentialView(host=h, pat_configured=True)
            for h in settings.gitlab_allowed_hosts_list
        ],
        default_model=(settings.default_model or "").strip(),
        gitlab_webhook_enabled=bool(
            getattr(settings, "gitlab_webhook_enabled", False)
        ),
        gitlab_bot_mentions=(
            getattr(settings, "gitlab_bot_mentions", "") or ""
        ).strip(),
        gitlab_webhook_secret_configured=bool(
            (getattr(settings, "gitlab_webhook_secret", "") or "").strip()
        ),
        gitlab_webhook_path="/webhooks/gitlab",
        project_repositories=_settings_project_repositories(),
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


def _normalize_gitlab_host(raw: Any) -> str:
    """Lowercase host; strip scheme/path if the operator pasted a URL."""
    host = str(raw or "").strip().lower()
    if not host:
        return ""
    if "://" in host or "/" in host:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(host if "://" in host else f"https://{host}")
            host = (parsed.hostname or host).lower()
        except Exception:
            host = host.split("/")[0]
    return host.strip()


def apply_settings_update(body: SettingsUpdate) -> SettingsView:
    """Apply runtime settings (including write-only secrets).

    Non-secret fields persist in runtime_settings.json. Jira host/email/token
    and GitLab host PATs are also written to ``.env`` so Test connection and
    the next daemon start
    use the saved values. Token is never returned in the view.

    Callers should refresh live Jira clients when host/token/email change
    (see ``refresh_runtime_jira_clients``).
    """
    data = body.model_dump(exclude_unset=True)
    dotenv_updates: Dict[str, str] = {}

    if "jira_host" in data and data["jira_host"] is not None:
        host = str(data["jira_host"]).strip().rstrip("/")
        from src.jira_connection import _normalize_jira_host

        old_h = _normalize_jira_host(settings.jira_host or "")
        new_h = _normalize_jira_host(host)
        token_in_patch = bool(str(data.get("jira_api_token") or "").strip())
        if new_h and old_h and new_h != old_h and not token_in_patch:
            raise ValueError(
                "Changing Jira host requires an API token in the same save "
                "so the stored token is not sent to a new host."
            )
        settings.jira_host = host
        dotenv_updates["JIRA_HOST"] = host
        # Non-secret: also survive restart via runtime_settings.json
        # (applied below with other persist keys)
    if "jira_email" in data and data["jira_email"] is not None:
        # Empty string clears Cloud email → Bearer PAT mode
        settings.jira_email = str(data["jira_email"]).strip()
        dotenv_updates["JIRA_EMAIL"] = settings.jira_email
    if "jira_api_token" in data and data["jira_api_token"] is not None:
        # Write-only: only apply non-empty values so blank UI fields keep current
        tok = str(data["jira_api_token"])
        if tok.strip():
            settings.jira_api_token = tok.strip()
            dotenv_updates["JIRA_API_TOKEN"] = settings.jira_api_token

    # Preferred: full list of per-host credentials from the dashboard
    if "gitlab_credentials" in data and data["gitlab_credentials"] is not None:
        current = (
            settings.gitlab_host_pat_map()
            if hasattr(settings, "gitlab_host_pat_map")
            else {}
        )
        new_map: Dict[str, str] = {}
        for item in data["gitlab_credentials"] or []:
            if isinstance(item, dict):
                host = _normalize_gitlab_host(item.get("host"))
                pat_raw = item.get("pat")
                previous_host = _normalize_gitlab_host(item.get("previous_host"))
            else:
                host = _normalize_gitlab_host(getattr(item, "host", None))
                pat_raw = getattr(item, "pat", None)
                previous_host = _normalize_gitlab_host(
                    getattr(item, "previous_host", None)
                )
            if not host:
                continue
            pat = str(pat_raw or "").strip()
            if pat:
                new_map[host] = pat
            elif host in current:
                new_map[host] = current[host]
            elif previous_host and previous_host in current:
                # Explicit rename from the Settings UI — not an inferred swap.
                new_map[host] = current[previous_host]
        if hasattr(settings, "set_gitlab_host_pat_map"):
            settings.set_gitlab_host_pat_map(new_map)
        else:
            settings.gitlab_allowed_hosts = ",".join(sorted(new_map.keys()))
            settings.gitlab_pat = next(iter(new_map.values()), "") if new_map else ""
        dotenv_updates["GITLAB_HOST_PATS"] = getattr(
            settings, "gitlab_host_pats", ""
        ) or ""
        dotenv_updates["GITLAB_ALLOWED_HOSTS"] = (
            settings.gitlab_allowed_hosts or ""
        )
        dotenv_updates["GITLAB_PAT"] = settings.gitlab_pat or ""
    else:
        # Legacy single PAT + host list (still supported)
        if "gitlab_pat" in data and data["gitlab_pat"] is not None:
            pat = str(data["gitlab_pat"])
            if pat.strip():
                settings.gitlab_pat = pat.strip()
        if "gitlab_allowed_hosts" in data and data["gitlab_allowed_hosts"] is not None:
            raw = str(data["gitlab_allowed_hosts"])
            hosts = [h.strip().lower() for h in raw.split(",") if h.strip()]
            settings.gitlab_allowed_hosts = ",".join(hosts)
            # If we have a single legacy PAT, expand into host map for runtime use
            if (
                hasattr(settings, "set_gitlab_host_pat_map")
                and (settings.gitlab_pat or "").strip()
                and hosts
            ):
                settings.set_gitlab_host_pat_map(
                    {h: settings.gitlab_pat.strip() for h in hosts}
                )
        if "gitlab_pat" in data or "gitlab_allowed_hosts" in data:
            dotenv_updates["GITLAB_HOST_PATS"] = getattr(
                settings, "gitlab_host_pats", ""
            ) or ""
            dotenv_updates["GITLAB_ALLOWED_HOSTS"] = (
                settings.gitlab_allowed_hosts or ""
            )
            dotenv_updates["GITLAB_PAT"] = settings.gitlab_pat or ""

    # Runtime-persisted fields (survive restart; win over .env)
    runtime_persist: Dict[str, Any] = {}

    if "jira_host" in data and data["jira_host"] is not None:
        runtime_persist["jira_host"] = settings.jira_host
    if "jira_email" in data and data["jira_email"] is not None:
        runtime_persist["jira_email"] = settings.jira_email
    if "jira_board_id" in data and data["jira_board_id"] is not None:
        settings.jira_board_id = str(data["jira_board_id"]).strip()
        runtime_persist["jira_board_id"] = settings.jira_board_id
    if "poll_interval_seconds" in data and data["poll_interval_seconds"] is not None:
        settings.poll_interval_seconds = int(data["poll_interval_seconds"])
        runtime_persist["poll_interval_seconds"] = settings.poll_interval_seconds
    if "trigger_labels" in data and data["trigger_labels"] is not None:
        settings.trigger_labels = str(data["trigger_labels"])
        runtime_persist["trigger_labels"] = settings.trigger_labels
    if "trigger_on_assignment" in data and data["trigger_on_assignment"] is not None:
        settings.trigger_on_assignment = bool(data["trigger_on_assignment"])
        runtime_persist["trigger_on_assignment"] = settings.trigger_on_assignment
    if "max_concurrent_jobs" in data and data["max_concurrent_jobs"] is not None:
        settings.max_concurrent_jobs = int(data["max_concurrent_jobs"])
        runtime_persist["max_concurrent_jobs"] = settings.max_concurrent_jobs
    if (
        "agent_task_timeout_seconds" in data
        and data["agent_task_timeout_seconds"] is not None
    ):
        # Single budget for agent runner + OpenCode serve turn
        settings.agent_task_timeout_seconds = int(data["agent_task_timeout_seconds"])
        runtime_persist["agent_task_timeout_seconds"] = (
            settings.agent_task_timeout_seconds
        )
        logger.info(
            f"Agent/OpenCode timeout set to "
            f"{settings.agent_task_timeout_seconds}s (next job uses this)"
        )
    if "agent_task_max_retries" in data and data["agent_task_max_retries"] is not None:
        settings.agent_task_max_retries = int(data["agent_task_max_retries"])
        runtime_persist["agent_task_max_retries"] = settings.agent_task_max_retries
        logger.info(
            f"Agent max error retries set to {settings.agent_task_max_retries} "
            f"(next job uses this)"
        )
    if (
        "agent_task_max_incomplete_retries" in data
        and data["agent_task_max_incomplete_retries"] is not None
    ):
        settings.agent_task_max_incomplete_retries = int(
            data["agent_task_max_incomplete_retries"]
        )
        runtime_persist["agent_task_max_incomplete_retries"] = (
            settings.agent_task_max_incomplete_retries
        )
        logger.info(
            f"Agent compact/incomplete retries set to "
            f"{settings.agent_task_max_incomplete_retries} (next job uses this)"
        )
    if (
        "opencode_serve_max_compact_continues" in data
        and data["opencode_serve_max_compact_continues"] is not None
    ):
        settings.opencode_serve_max_compact_continues = int(
            data["opencode_serve_max_compact_continues"]
        )
        runtime_persist["opencode_serve_max_compact_continues"] = (
            settings.opencode_serve_max_compact_continues
        )
        logger.info(
            f"Serve compact continues set to "
            f"{settings.opencode_serve_max_compact_continues} (next job uses this)"
        )
    if "default_model" in data and data["default_model"] is not None:
        model = str(data["default_model"]).strip()
        if model:
            settings.default_model = model
            runtime_persist["default_model"] = settings.default_model
    if "project_repositories" in data and data["project_repositories"] is not None:
        encoded = project_repositories_to_json(data["project_repositories"])
        settings.project_repositories = encoded
        runtime_persist["project_repositories"] = encoded

    if runtime_persist:
        # Persist so the next job (and process restart) does not fall back to .env
        save_runtime_settings(runtime_persist)

    if dotenv_updates:
        # Token/host/email must land in .env + os.environ so Test + restart work.
        upsert_dotenv_keys(dotenv_updates)

    return build_settings_view()


def refresh_runtime_jira_clients(
    *,
    processor: Any = None,
    poller: Any = None,
) -> None:
    """Rebuild live Jira clients after host/token/email settings change.

    Best-effort: logs and continues if a client cannot be closed/recreated.
    GitLab PAT is read from ``settings`` on each git operation — no refresh needed.
    """
    from src.jira.client import create_jira_client

    use_simulated = (
        not settings.is_configured()
        or (settings.jira_host or "").strip()
        in ("", "a", "https://yourcompany.atlassian.net")
    )

    if processor is not None:
        try:
            old = getattr(processor, "jira_client", None)
            if old is not None and hasattr(old, "close"):
                try:
                    old.close()
                except Exception:
                    pass
            new_client = create_jira_client(simulated=use_simulated)
            processor.jira_client = new_client
            reporter = getattr(processor, "reporter", None)
            if reporter is not None:
                reporter.client = new_client
            logger.info(
                "Refreshed processor Jira client "
                f"(simulated={use_simulated}, host={settings.jira_host!r})"
            )
        except Exception as e:
            logger.warning(f"Could not refresh processor Jira client: {e}")

    if poller is not None:
        try:
            old = getattr(poller, "client", None)
            if old is not None and hasattr(old, "close"):
                try:
                    old.close()
                except Exception:
                    pass
            poller.client = create_jira_client(simulated=use_simulated)
            logger.info(
                "Refreshed poller Jira client "
                f"(simulated={use_simulated}, host={settings.jira_host!r})"
            )
        except Exception as e:
            logger.warning(f"Could not refresh poller Jira client: {e}")


def _parse_session_log_name(name: str) -> Optional[tuple]:
    """Parse session log basename → (issue_key, started_at iso).

    Accepts:
      ISSUE_YYYYMMDD_HHMMSS.log
      ISSUE_YYYYMMDD_HHMMSS_retryN.log
      ISSUE_YYYYMMDD_HHMMSS_N.log  (legacy numeric suffix)
      ISSUE_type_YYYYMMDD_HHMMSS[.log|_retryN.log]
    """
    m = re.match(
        r"^([A-Z][A-Z0-9]+-\d+)"
        r"(?:_[A-Za-z][A-Za-z0-9._-]*)?"  # optional task_type
        r"_(\d{8})_(\d{6})"
        r"(?:_retry\d+|_\d+)?"
        r"\.log$",
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
    """Deprecated: legacy session rows are no longer merged into the jobs list.

    Kept as a no-op so older tests/call sites that import the symbol still work.
    Retries and multi-attempt OpenCode logs belong on the parent JobStore job
    (``session_log_paths`` / ``retry_attempts``), not as ``legacy_*`` rows.
    """
    return []


def _job_session_log_paths(j: Dict[str, Any]) -> List[str]:
    """Ordered unique session log paths for a job dict."""
    paths: List[str] = []
    for p in j.get("session_log_paths") or []:
        if p and p not in paths:
            paths.append(str(p))
    latest = j.get("session_log_path")
    if latest and latest not in paths:
        paths.append(str(latest))
    return paths


def _job_prompt_paths(j: Dict[str, Any]) -> List[str]:
    paths: List[str] = []
    for p in j.get("prompt_paths") or []:
        if p and p not in paths:
            paths.append(str(p))
    latest = j.get("prompt_path")
    if latest and latest not in paths:
        paths.append(str(latest))
    return paths


def job_dict_to_item(
    j: Dict[str, Any],
    *,
    summaries: Optional[Dict[str, str]] = None,
    live_keys: Optional[set] = None,
    active_job_ids: Optional[set] = None,
    store: Optional[JobStore] = None,
    include_description: bool = True,
) -> JobItem:
    """Enrich one JobStore dict into a JobItem (list or single-job detail)."""
    summaries = summaries or {}
    live_keys = live_keys or set()
    active_job_ids = active_job_ids or set()
    js = store or default_job_store
    jid = j.get("job_id") or ""
    ik = j.get("issue_key") or ""
    if not j.get("summary") and ik in summaries:
        j = {**j, "summary": summaries[ik]}
    description = ""
    if include_description:
        j = js.ensure_description(j, persist=str(jid).startswith("job_"))
        if not (j.get("description") or "").strip() and j.get("prompt_path"):
            recovered = description_from_prompt_path(j.get("prompt_path"))
            if recovered:
                j = {**j, "description": recovered}
        description = j.get("description") or ""
    live = jid in active_job_ids or (
        ik in live_keys and (j.get("status") or "") in ("running", "planning", "executing")
    )
    session_paths = _job_session_log_paths(j)
    prompt_paths = _job_prompt_paths(j)
    return JobItem(
        job_id=jid,
        issue_key=ik,
        summary=j.get("summary") or "",
        description=description,
        workflow_type=j.get("workflow_type") or "execution",
        agent=j.get("agent") or "",
        model=(j.get("model") or None),
        status=j.get("status") or "unknown",
        task_id=j.get("task_id"),
        task_ids=list(j.get("task_ids") or ([j["task_id"]] if j.get("task_id") else [])),
        opencode_session_id=j.get("opencode_session_id"),
        opencode_session_ids=list(j.get("opencode_session_ids") or []),
        session_log_path=j.get("session_log_path") or (
            session_paths[-1] if session_paths else None
        ),
        session_log_paths=session_paths,
        prompt_path=j.get("prompt_path") or (prompt_paths[-1] if prompt_paths else None),
        prompt_paths=prompt_paths,
        retry_attempts=_job_retry_attempts(j),
        progress_percentage=int(j.get("progress_percentage") or 0),
        error_message=j.get("error_message"),
        started_at=j.get("started_at"),
        completed_at=j.get("completed_at"),
        updated_at=j.get("updated_at"),
        live=live,
        feature_branch=j.get("feature_branch") or None,
        merge_request_url=j.get("merge_request_url") or None,
        commit_sha=j.get("commit_sha") or None,
        commit_subject=j.get("commit_subject") or None,
        commit_url=j.get("commit_url") or None,
        delivery_status=j.get("delivery_status") or None,
        delivery_note=j.get("delivery_note") or None,
        working_directory=(j.get("working_directory") or None),
        source=str(j.get("source") or "jira"),
        gitlab_project=j.get("gitlab_project") or None,
        gitlab_mr_iid=j.get("gitlab_mr_iid"),
    )


def _job_working_directory(j: Dict[str, Any]) -> str:
    """Clone path stored on the job, else the matching OpenCode session bind."""
    raw = (j.get("working_directory") or "").strip()
    if raw:
        return raw
    jid = (j.get("job_id") or "").strip()
    sid = (j.get("opencode_session_id") or "").strip()
    if not jid and not sid:
        return ""
    try:
        from src.state.session_bind_store import session_bind_store

        for rec in session_bind_store.list_binds(limit=500):
            wd = (rec.get("working_directory") or "").strip()
            if not wd:
                continue
            if jid and rec.get("job_id") == jid:
                return wd
            if sid and rec.get("session_id") == sid:
                return wd
    except Exception:
        return ""
    return ""


def build_one_job(
    job_id: str,
    *,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> Optional[JobItem]:
    """Single enriched job without scanning the whole JobStore list."""
    js = store or default_job_store
    raw = js.get_job((job_id or "").strip())
    if not raw:
        return None
    live_keys: set = set()
    active_job_ids: set = set()
    if processor is not None:
        live_keys = set(processor.list_live_processing_keys())
        active_job_ids = set((processor._active_jobs or {}).values())
    summaries: Dict[str, str] = {}
    live_sid = ""
    ik = (raw.get("issue_key") or "").strip()
    if state_manager is not None and ik:
        st = state_manager.get_state(ik)
        if st and st.issue_summary:
            summaries[st.issue_key] = st.issue_summary
        if st:
            live_sid = (st.current_opencode_session_id or "").strip()
            if not live_sid:
                live_sid = str((st.metadata or {}).get("last_opencode_session_id") or "").strip()
            current_jid = str((st.metadata or {}).get("current_job_id") or "").strip()
            if current_jid and current_jid != (raw.get("job_id") or "").strip():
                live_sid = ""
    if live_sid and live_sid.startswith("ses_") and not (raw.get("opencode_session_id") or "").strip():
        raw = {**raw, "opencode_session_id": live_sid}
        ids = list(raw.get("opencode_session_ids") or [])
        if live_sid not in ids:
            raw = {**raw, "opencode_session_ids": ids + [live_sid]}
    item = job_dict_to_item(
        raw,
        summaries=summaries,
        live_keys=live_keys,
        active_job_ids=active_job_ids,
        store=js,
        include_description=True,
    )
    wd = _job_working_directory({**raw, "opencode_session_id": item.opencode_session_id})
    if not wd and processor is not None and ik:
        try:
            git = processor._git_for(ik)
            got = git.get_working_directory() if git is not None else None
            if got:
                wd = str(got)
        except Exception:
            wd = ""
    if wd and wd != item.working_directory:
        item = item.model_copy(update={"working_directory": wd})
    return item


def _job_retry_attempts(j: Dict[str, Any]) -> List[JobRetryAttempt]:
    out: List[JobRetryAttempt] = []
    for raw in j.get("retry_attempts") or []:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(
                JobRetryAttempt(
                    attempt_number=int(raw.get("attempt_number") or 0),
                    label=str(raw.get("label") or ""),
                    reason=str(raw.get("reason") or ""),
                    delay_seconds=float(raw.get("delay_seconds") or 0),
                    failed_session_log_path=raw.get("failed_session_log_path"),
                    error_message=raw.get("error_message"),
                    return_code=raw.get("return_code"),
                    opencode_session_id=raw.get("opencode_session_id"),
                    task_id=raw.get("task_id"),
                    timestamp=raw.get("timestamp"),
                )
            )
        except Exception:
            continue
    return out


def build_jobs(
    *,
    issue_key: Optional[str] = None,
    limit: int = 25,
    page: int = 1,
    page_size: Optional[int] = None,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> JobsResponse:
    """List agent jobs with server-side pagination.

    ``page`` / ``page_size`` are preferred. ``limit`` alone is treated as page_size
    on page 1 (backward compatible for callers that only pass limit).
    """
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

    size = int(page_size if page_size is not None else limit or 25)
    size = max(1, min(size, 100))
    page_n = max(1, int(page or 1))
    offset = (page_n - 1) * size

    # JobStore only — never synthesize legacy_* rows from session files.
    # Retries live under the parent job (session_log_paths / retry_attempts).
    fetch_cap = 2000
    raw = js.list_jobs(issue_key=issue_key, limit=fetch_cap, offset=0)
    raw = [j for j in raw if not str(j.get("job_id") or "").startswith("legacy_")]
    inflight: List[Dict[str, Any]] = []
    rest: List[Dict[str, Any]] = []
    for j in raw:
        st = str(j.get("status") or "").lower()
        live = (
            j.get("issue_key") in live_keys
            or j.get("job_id") in active_job_ids
            or st in {"executing", "planning", "running", "pending"}
        )
        (inflight if live else rest).append(j)
    inflight.sort(
        key=lambda j: j.get("started_at") or j.get("updated_at") or "",
        reverse=True,
    )
    rest.sort(
        key=lambda j: j.get("started_at") or j.get("updated_at") or "",
        reverse=True,
    )
    raw = inflight + rest

    total = len(raw)
    page_raw = raw[offset : offset + size]

    items: List[JobItem] = []
    for j in page_raw:
        items.append(
            job_dict_to_item(
                j,
                summaries=summaries,
                live_keys=live_keys,
                active_job_ids=active_job_ids,
                store=js,
                include_description=bool(issue_key),
            )
        )
    return JobsResponse(
        jobs=items,
        total=total,
        page=page_n,
        page_size=size,
        issue_key_filter=(issue_key or None),
        server_time=datetime.now().isoformat(timespec="seconds"),
    )


def build_queue(
    *,
    status: Optional[str] = None,
    limit: int = 200,
    store: Any = None,
) -> QueueResponse:
    """List work-queue items for the dashboard (Jira + GitLab)."""
    from src.state.queue_store import work_queue_store as default_queue

    qs = store or default_queue
    if status:
        raw = qs.list_items(status=status, limit=limit)
    else:
        raw = qs.list_items(status="queued", limit=limit) + qs.list_items(
            status="running", limit=limit
        )
    items: List[QueueItem] = []
    queued = len(qs.list_items(status="queued", limit=500))
    running = len(qs.list_items(status="running", limit=500))
    for rec in raw:
        st = rec.get("status") or "queued"
        if st == "queued":
            queued += 1
        elif st == "running":
            running += 1
        try:
            items.append(
                QueueItem(
                    queue_id=rec.get("queue_id") or "",
                    status=st,
                    source=rec.get("source") or "jira",
                    issue_key=rec.get("issue_key") or "",
                    summary=rec.get("summary") or "",
                    message=rec.get("message") or "",
                    repository_url=rec.get("repository_url") or "",
                    source_branch=rec.get("source_branch") or "",
                    work_branch=rec.get("work_branch") or "",
                    target_branch=rec.get("target_branch") or "",
                    lock_key=rec.get("lock_key") or "",
                    job_id=rec.get("job_id"),
                    merge_request_url=rec.get("merge_request_url") or "",
                    gitlab_note_id=rec.get("gitlab_note_id") or "",
                    error_message=rec.get("error_message"),
                    created_at=rec.get("created_at"),
                    started_at=rec.get("started_at"),
                    finished_at=rec.get("finished_at"),
                )
            )
        except Exception:
            continue
    items.sort(
        key=lambda i: (
            {"queued": 0, "running": 1}.get(i.status, 9),
            i.created_at or "",
        )
    )
    return QueueResponse(
        items=items,
        queued_count=queued,
        running_count=running,
        total=len(items),
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
            page=1,
            page_size=25,
            processor=processor,
            state_manager=state_manager,
        ).model_dump(),
        "poll": build_poll_status(store, state_manager).model_dump(),
        "settings": build_settings_view().model_dump(),
        "queue": build_queue().model_dump(),
    }


_LIVE_JOB_STATUSES = frozenset({"running", "planning", "executing", "pending"})


def _resolve_job_dict(
    job_id: str,
    *,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
) -> Optional[Dict[str, Any]]:
    """Load a job by id from JobStore or merged list (legacy session rows)."""
    jid = (job_id or "").strip()
    if not jid:
        return None
    js = store or default_job_store
    job = js.get_job(jid)
    if job:
        return job
    merged = build_jobs(
        limit=500,
        page=1,
        page_size=500,
        processor=processor,
        store=js,
        state_manager=state_manager,
    )
    for item in merged.jobs:
        if item.job_id == jid:
            return item.model_dump()
    return None


def _safe_delete_agent_artifact(path_str: Optional[str]) -> Optional[str]:
    """Delete a session log / prompt under .jira-agent only. Returns path if deleted."""
    if not path_str:
        return None
    try:
        path = Path(str(path_str)).resolve()
    except OSError:
        return None
    if not path.is_file():
        return None

    def _under(root: Path) -> bool:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            return False

    allowed = _under(Path.cwd() / ".jira-agent")
    if not allowed:
        return None
    blocked = {"etc", "proc", "sys", "windows", "system32"}
    if blocked & {p.lower() for p in path.parts}:
        return None
    try:
        path.unlink()
        # Sibling session_id marker next to log
        sid = Path(str(path) + ".session_id")
        if sid.is_file() and (
            _under(Path.cwd() / ".jira-agent") or ".jira-agent" in sid.parts
        ):
            try:
                sid.unlink()
            except OSError:
                pass
        return str(path)
    except OSError:
        return None


def delete_job_record(
    job_id: str,
    *,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
    delete_artifacts: bool = True,
) -> Dict[str, Any]:
    """Delete a historical job record and optional linked session/prompt files.

    Refuses in-flight / live jobs. Does not change Jira issues or issue state
    beyond scrubbing this job_id from metadata.job_ids when present.
    """
    jid = (job_id or "").strip()
    js = store or default_job_store
    job = _resolve_job_dict(
        jid, processor=processor, store=js, state_manager=state_manager
    )
    if not job:
        return {"ok": False, "error": f"No job {jid}", "job_id": jid}

    status = (job.get("status") or "").lower()
    active_ids: set = set()
    if processor is not None:
        active_ids = set((getattr(processor, "_active_jobs", None) or {}).values())
        if jid in active_ids:
            return {
                "ok": False,
                "error": "Cannot delete a live job; cancel issue work first",
                "job_id": jid,
                "status": status,
            }

    if status in _LIVE_JOB_STATUSES:
        return {
            "ok": False,
            "error": (
                f"Cannot delete job in status {status}; "
                "wait until finished or cancel issue work first"
            ),
            "job_id": jid,
            "status": status,
        }

    deleted_paths: List[str] = []
    store_deleted = False
    if jid.startswith("job_"):
        store_deleted = js.delete_job(jid)
    elif jid.startswith("legacy_"):
        # No store file — artifacts-only cleanup still useful
        store_deleted = False
    else:
        # Unknown id shape: try store path once
        store_deleted = js.delete_job(jid) if jid.startswith("job_") else False

    if delete_artifacts:
        # All session/prompt artifacts for this job (initial + retries)
        artifact_candidates: List[Optional[str]] = []
        artifact_candidates.extend(_job_session_log_paths(job))
        artifact_candidates.extend(_job_prompt_paths(job))
        artifact_candidates.append(job.get("session_log_path"))
        artifact_candidates.append(job.get("prompt_path"))
        for raw_ra in job.get("retry_attempts") or []:
            if isinstance(raw_ra, dict):
                artifact_candidates.append(raw_ra.get("failed_session_log_path"))
        seen_art: set = set()
        for p in artifact_candidates:
            if not p or p in seen_art:
                continue
            seen_art.add(p)
            gone = _safe_delete_agent_artifact(p)
            if gone:
                deleted_paths.append(gone)
            # Sibling prompt next to each session log
            try:
                log = Path(str(p))
                if log.suffix == ".log":
                    sibling = log.parent / f"{log.stem}.prompt.txt"
                    gone = _safe_delete_agent_artifact(str(sibling))
                    if gone:
                        deleted_paths.append(gone)
            except Exception:
                pass
        # Durable per-job system log (daemon lines tagged with job_id)
        try:
            from src.dashboard.issue_logs import job_system_log_path

            slog = job_system_log_path(jid)
            if slog is not None and slog.is_file():
                gone = _safe_delete_agent_artifact(str(slog))
                if gone:
                    deleted_paths.append(gone)
        except Exception:
            pass

    # Scrub job_id from issue metadata history (does not change issue status)
    issue_key = (job.get("issue_key") or "").strip().upper()
    if issue_key and state_manager is not None:
        try:
            st = state_manager.get_state(issue_key)
            if st:
                meta = dict(st.metadata or {})
                job_ids = [x for x in (meta.get("job_ids") or []) if x != jid]
                patch: Dict[str, Any] = {}
                if job_ids != list(meta.get("job_ids") or []):
                    patch["job_ids"] = job_ids
                if meta.get("current_job_id") == jid:
                    patch["current_job_id"] = job_ids[-1] if job_ids else None
                if patch:
                    state_manager.update_state(issue_key, metadata=patch)
        except Exception as e:
            from src.logger import logger

            logger.debug(f"Could not scrub job_id from issue metadata: {e}")

    # Legacy-only with no store file and no artifacts deleted → still ok if we
    # intended to remove history (nothing left on disk)
    if not store_deleted and not deleted_paths and jid.startswith("legacy_"):
        return {
            "ok": True,
            "job_id": jid,
            "issue_key": issue_key,
            "store_deleted": False,
            "artifacts_deleted": [],
            "message": "Legacy job had no store file; nothing left to delete on disk",
        }

    if not store_deleted and not deleted_paths and jid.startswith("job_"):
        return {
            "ok": False,
            "error": f"Job file not found for {jid}",
            "job_id": jid,
        }

    return {
        "ok": True,
        "job_id": jid,
        "issue_key": issue_key,
        "store_deleted": store_deleted,
        "artifacts_deleted": deleted_paths,
        "message": "Job deleted",
    }


_MAX_BULK_JOB_DELETE = 100


def delete_job_records(
    job_ids: List[str],
    *,
    processor: Optional["JobProcessor"] = None,
    store: Optional[JobStore] = None,
    state_manager: Optional[JiraStateManager] = None,
    delete_artifacts: bool = True,
) -> Dict[str, Any]:
    """Delete multiple historical job records. Returns per-id results.

    Skips empty/duplicate ids. Caps at ``_MAX_BULK_JOB_DELETE``. Live / in-flight
    jobs are refused per id (same rules as ``delete_job_record``).
    """
    seen: set = set()
    ordered: List[str] = []
    for raw in job_ids or []:
        jid = (raw or "").strip()
        if not jid or jid in seen:
            continue
        seen.add(jid)
        ordered.append(jid)
        if len(ordered) >= _MAX_BULK_JOB_DELETE:
            break

    results: List[Dict[str, Any]] = []
    deleted: List[str] = []
    failed: List[Dict[str, str]] = []
    for jid in ordered:
        out = delete_job_record(
            jid,
            processor=processor,
            store=store,
            state_manager=state_manager,
            delete_artifacts=delete_artifacts,
        )
        results.append(out)
        if out.get("ok"):
            deleted.append(jid)
        else:
            failed.append(
                {
                    "job_id": jid,
                    "error": str(out.get("error") or "Delete failed"),
                }
            )

    return {
        "ok": len(failed) == 0 and len(deleted) > 0,
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "results": results,
        "message": (
            f"Deleted {len(deleted)} job(s)"
            + (f"; {len(failed)} failed" if failed else "")
        ),
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


def _artifacts_root() -> Path:
    return (Path.cwd() / ".jira-agent").resolve()


def collect_job_text_artifacts(job: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Read prompt/session files linked on this job only (not the whole issue)."""
    if hasattr(job, "model_dump"):
        job = job.model_dump()
    if not isinstance(job, dict):
        return {"prompts": [], "session_logs": []}
    root = _artifacts_root()
    prompts: List[Dict[str, Any]] = []
    logs: List[Dict[str, Any]] = []
    seen_p: set = set()
    seen_l: set = set()
    for p in _job_prompt_paths(job):
        if not p or p in seen_p:
            continue
        seen_p.add(p)
        prompts.append(_read_text_capped(Path(p), _MAX_PROMPT_CHARS, root=root))
    for p in _job_session_log_paths(job):
        if not p or p in seen_l:
            continue
        seen_l.add(p)
        logs.append(_read_text_capped(Path(p), _MAX_SESSION_CHARS, root=root))
    return {"prompts": prompts, "session_logs": logs}


def _job_started_ms(job: Dict[str, Any]) -> Optional[int]:
    raw = job.get("started_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip())
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, OSError):
        return None


def _session_time_ms(raw: Any) -> int:
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        return 0
    if n > 10_000_000_000:
        return int(n)
    return int(n * 1000)


def _job_opencode_session_ids(job: Dict[str, Any]) -> List[str]:
    """Session ids recorded on this job (history + sidecar + live OpenCode DB)."""
    ids: List[str] = []

    def _add(raw: Any) -> None:
        sid = str(raw or "").strip()
        if sid and sid.startswith("ses_") and sid not in ids:
            ids.append(sid)

    for sid in job.get("opencode_session_ids") or []:
        _add(sid)
    _add(job.get("opencode_session_id"))
    for attempt in job.get("retry_attempts") or []:
        if isinstance(attempt, dict):
            _add(attempt.get("opencode_session_id"))
    recorded = list(ids)
    for path in _job_session_log_paths(job):
        try:
            marker = Path(str(path) + ".session_id")
            if marker.is_file():
                _add(marker.read_text(encoding="utf-8").splitlines()[0])
        except OSError:
            pass
        try:
            log_path = Path(str(path))
            if log_path.is_file() and log_path.stat().st_size > 0:
                raw = log_path.read_text(encoding="utf-8", errors="replace")
                if len(raw) > 16_000:
                    raw = raw[:8_000] + "\n" + raw[-8_000:]
                for sid in extract_session_ids_from_text(raw):
                    _add(sid)
        except OSError:
            continue
    # Directory scan only when this job has no recorded ses_* (live / early
    # chat). Never pull a later job's session from a reused clone folder.
    if recorded:
        return ids
    wd = (job.get("working_directory") or "").strip()
    if wd:
        try:
            from src.opencode_sessions import find_sessions_for_directory

            started_ms = _job_started_ms(job)
            completed_ms = _job_started_ms(
                {"started_at": job.get("completed_at")}
            )
            found = find_sessions_for_directory(wd, limit=20)
            for rec in found:
                created_ms = _session_time_ms(rec.get("time_created"))
                if started_ms is None:
                    _add(rec.get("id"))
                    break
                if created_ms < started_ms - 15_000:
                    continue
                if completed_ms and created_ms > completed_ms:
                    continue
                _add(rec.get("id"))
        except Exception:
            pass
    return ids


_CHAT_WRAP_RESTART_SLACK_MS = 2_000


def _job_wrap_restart_ms(job: Dict[str, Any]) -> Optional[int]:
    """Timestamp where this job's prompt may repeat the previous [search-mode] kit."""
    started = _job_started_ms(job)
    if started is None:
        return None
    return started - _CHAT_WRAP_RESTART_SLACK_MS


def _job_prompt_for_chat(job: Dict[str, Any]) -> str:
    """Best-effort operator prompt when the session window has no user turn."""
    from src.state.job_store import extract_task_description_from_prompt

    for path in _job_prompt_paths(job):
        if not path:
            continue
        try:
            raw = Path(str(path)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = description_from_prompt_path(path)
        text = (raw or "").strip()
        if not text:
            continue
        cleaned = strip_internal_markup(text)
        if is_omo_mode_wrap_text(cleaned):
            cleaned = strip_omo_mode_wrap(cleaned)
        extracted = extract_task_description_from_prompt(cleaned)
        body = (extracted or cleaned).strip()
        if body:
            return body
    for key in ("description", "summary"):
        body = str(job.get(key) or "").strip()
        if body:
            return body
    return ""


def _synthetic_user_message(
    job: Dict[str, Any], *, session_id: str, text: str
) -> Dict[str, Any]:
    return {
        "id": f"{job.get('job_id') or 'job'}:prompt",
        "session_id": session_id,
        "role": "user",
        "raw_role": "user",
        "finish": None,
        "summary": False,
        "agent": None,
        "created_at": job.get("started_at"),
        "parts": [{"id": "prompt", "type": "text", "text": text}],
    }


def collect_job_chat(job: Any) -> Dict[str, Any]:
    """OpenCode chat for sessions linked to this job.

    Continuing / re-queueing resumes the same ``ses_*``. The dashboard must
    show the full prompt + model history from that session, including this
    run's new operator turn (not only the cancelled job's first prompt).
    """
    if hasattr(job, "model_dump"):
        job = job.model_dump()
    if not isinstance(job, dict):
        return {
            "job_id": "",
            "session_ids": [],
            "sessions": [],
            "messages": [],
        }
    sids = _job_opencode_session_ids(job)
    restart_ms = _job_wrap_restart_ms(job)
    sessions: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    for sid in sids:
        chat = list_session_chat(sid, restart_wraps_at_ms=restart_ms)
        sessions.append(
            {
                "session_id": sid,
                "title": chat.get("title"),
                "directory": chat.get("directory"),
                "message_count": len(chat.get("messages") or []),
                "truncated": bool(chat.get("truncated")),
                "error": chat.get("error"),
            }
        )
        for msg in chat.get("messages") or []:
            messages.append(msg)
    if not any(m.get("role") == "user" for m in messages):
        preview = _job_prompt_for_chat(job)
        if preview:
            messages.insert(
                0,
                _synthetic_user_message(
                    job, session_id=sids[0] if sids else "", text=preview
                ),
            )
            if sessions:
                sessions[0]["message_count"] = len(messages)
    return {
        "job_id": job.get("job_id") or "",
        "session_ids": sids,
        "sessions": sessions,
        "messages": messages,
    }


def _reconstruct_prompts(state) -> Dict[str, Any]:
    """Metadata for the prompts tab (agent/workflow only).

    The UI shows captured ``*.prompt.txt`` files — the exact text sent to the
    agent. We do not rebuild a live “assembled” prompt for display.
    """
    workflow = (state.metadata or {}).get("workflow_type") or "execution"
    if workflow == WorkflowType.ORACLE_CONSULT.value or workflow == "oracle":
        agent_name = "oracle"
    else:
        agent_name = settings.default_agent

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


def _collect_git_deliveries(
    *,
    issue_key: str,
    meta: Optional[Dict[str, Any]] = None,
    jobs: Optional[List[Any]] = None,
    store: Optional[JobStore] = None,
) -> List[Dict[str, Any]]:
    """All commit/MR deliveries for an issue across runs (jobs + metadata history).

    Prefer per-job fields (one entry per processing run). Fall back to
    ``metadata.git_deliveries`` and the latest top-level MR/branch keys for
    older records that predate job-level storage.
    """
    meta = meta or {}
    items: List[Dict[str, Any]] = []
    index: Dict[tuple, Dict[str, Any]] = {}

    def _identities(d: Dict[str, Any]) -> List[tuple]:
        """Stable keys so the same MR is not listed three times.

        One push is stored on the job, in ``metadata.git_deliveries``, and again
        as top-level ``merge_request_url`` / ``feature_branch``. Those rows
        differ by job_id / created_at / status and used to bypass exact-key
        dedupe.
        """
        ids: List[tuple] = []
        mr = str(d.get("merge_request_url") or "").strip().rstrip("/")
        sha = str(d.get("commit_sha") or "").strip().lower()
        jid = str(d.get("job_id") or "").strip()
        if mr:
            ids.append(("mr", mr))
        if sha:
            ids.append(("sha", sha))
        if jid:
            ids.append(("job", jid))
        if not ids:
            branch = str(d.get("feature_branch") or "").strip()
            if branch:
                ids.append(("branch", branch))
        return ids

    def _merge_into(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
        for key in (
            "job_id",
            "feature_branch",
            "merge_request_url",
            "commit_sha",
            "commit_subject",
            "commit_url",
            "created_at",
            "status",
        ):
            if not dst.get(key) and src.get(key):
                dst[key] = src[key]

    def _add(raw: Dict[str, Any]) -> None:
        if not any(
            [
                raw.get("merge_request_url"),
                raw.get("commit_sha"),
                raw.get("commit_url"),
                raw.get("feature_branch"),
            ]
        ):
            return
        row = {
            "job_id": raw.get("job_id") or None,
            "feature_branch": raw.get("feature_branch") or None,
            "merge_request_url": raw.get("merge_request_url") or None,
            "commit_sha": raw.get("commit_sha") or None,
            "commit_subject": raw.get("commit_subject") or None,
            "commit_url": raw.get("commit_url") or None,
            "created_at": raw.get("created_at") or None,
            "status": raw.get("status") or None,
        }
        ids = _identities(row)
        if not ids:
            return
        existing: Optional[Dict[str, Any]] = None
        for ident in ids:
            hit = index.get(ident)
            if hit is not None:
                existing = hit
                break
        if existing is not None:
            _merge_into(existing, row)
            target = existing
        else:
            items.append(row)
            target = row
        for ident in _identities(target):
            index[ident] = target

    # 1) Jobs (source of truth per run)
    job_rows: List[Any] = list(jobs or [])
    if not job_rows:
        try:
            js = store or default_job_store
            job_rows = js.list_jobs(issue_key=issue_key, limit=100, offset=0)
        except Exception:
            job_rows = []
    for j in job_rows:
        if hasattr(j, "model_dump"):
            j = j.model_dump()
        if not isinstance(j, dict):
            continue
        _add(
            {
                "job_id": j.get("job_id"),
                "feature_branch": j.get("feature_branch"),
                "merge_request_url": j.get("merge_request_url"),
                "commit_sha": j.get("commit_sha"),
                "commit_subject": j.get("commit_subject"),
                "commit_url": j.get("commit_url"),
                "created_at": j.get("completed_at") or j.get("updated_at") or j.get("started_at"),
                "status": j.get("status"),
            }
        )

    # 2) Explicit history on issue metadata
    hist = meta.get("git_deliveries")
    if isinstance(hist, list):
        for entry in hist:
            if isinstance(entry, dict):
                _add(entry)

    # 3) Latest top-level keys (legacy single-MR storage)
    if meta.get("merge_request_url") or meta.get("last_commit_sha") or meta.get("feature_branch"):
        _add(
            {
                "job_id": meta.get("current_job_id"),
                "feature_branch": meta.get("feature_branch"),
                "merge_request_url": meta.get("merge_request_url"),
                "commit_sha": meta.get("last_commit_sha"),
                "commit_subject": meta.get("last_commit_subject"),
                "commit_url": meta.get("last_commit_url"),
                "created_at": None,
                "status": None,
            }
        )

    # Newest first for UI
    items.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return items


def _build_task_detail_without_state(
    issue_key: str,
    *,
    processor: Optional["JobProcessor"] = None,
) -> Dict[str, Any]:
    """Detail for board issues that have never been processed (no local state).

    Used when opening from Poll monitor — still show Jira/poll summary so
    operators can inspect the ticket without a 404.
    """
    key = (issue_key or "").strip().upper()
    # Never open a live Jira client from a no-state stub (arbitrary key SSRF/read)
    jira_live: Dict[str, Any] = {}
    if processor is not None:
        jira_live = _fetch_live_jira_fields(key, processor=processor)
    poll_row: Dict[str, Any] = {}
    try:
        from src.dashboard.snapshot import poll_snapshot_store as snap_store

        snap = snap_store.snapshot()
        for row in snap.get("issues") or []:
            if (row.get("key") or "").strip().upper() == key:
                poll_row = row
                break
    except Exception:
        pass

    summary = (
        (jira_live.get("summary") or "").strip()
        or (poll_row.get("summary") or "").strip()
        or ""
    )
    description = (jira_live.get("description") or "").strip()
    jira_status = (
        (jira_live.get("jira_status") or "").strip()
        or (poll_row.get("jira_status") or "").strip()
        or None
    )
    local_status = poll_row.get("local_status") or "pending"

    return {
        "issue_key": key,
        "summary": summary,
        "description": description,
        "jira_status": jira_status,
        "jira_live": bool(jira_live),
        "status": str(local_status),
        "progress_percentage": 0,
        "live": False,
        "can_cancel": False,
        "can_start": False,
        "workflow_type": None,
        "plan_path": None,
        "current_task_id": None,
        "current_opencode_session_id": None,
        "task_ids": [],
        "job_ids": [],
        "current_job_id": None,
        "opencode_session_ids": [],
        "opencode_sessions": [],
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "feature_branch": None,
        "merge_request_url": None,
        "git_deliveries": _collect_git_deliveries(issue_key=key, meta={}),
        "retry_history": [],
        "prompts": {
            "workflow_type": None,
            "agent": None,
            "captured_prompt_files": [],
        },
        "session_logs": [],
        "system_logs": issue_log_ring.for_issue(key, limit=500),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


def build_task_detail(
    issue_key: str,
    *,
    state_manager: Optional[JiraStateManager] = None,
    processor: Optional["JobProcessor"] = None,
    include_artifacts: bool = True,
    include_live_jira: bool = True,
    jobs: Optional[List[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Full task detail for dashboard (prompts, sessions, logs, cancel eligibility).

    Returns a read-only stub when the issue is on the board but has no local
    agent state yet (so Poll monitor can open any eligible key).

    ``include_artifacts`` / ``include_live_jira`` default True for callers that
    need the full dump. HTTP handlers pass False so overview paints quickly.
    Pass ``jobs`` to avoid a second JobStore scan for git deliveries.
    """
    sm = state_manager or JiraStateManager()
    key = (issue_key or "").strip().upper()
    state = sm.get_state(key) if key else None
    if not state:
        if not key:
            return None
        # Live Jira GET only for keys on the last poll snapshot (board read).
        # Arbitrary keys stay stub-only so this is not an open Jira proxy.
        on_board = False
        try:
            from src.dashboard.snapshot import poll_snapshot_store as snap_store

            snap = snap_store.snapshot()
            on_board = any(
                str(row.get("key") or "").upper() == key
                for row in (snap.get("issues") or [])
                if isinstance(row, dict)
            )
        except Exception:
            on_board = False
        return _build_task_detail_without_state(
            key, processor=processor if (on_board and include_live_jira) else None
        )

    live = False
    if processor is not None:
        live = processor._is_live_processing(key)

    # Live fields from Jira (not frozen local state / job snapshot)
    jira_live: Dict[str, Any] = {}
    if include_live_jira:
        jira_live = _fetch_live_jira_fields(key, processor=processor)
    if jira_live:
        live_summary = jira_live.get("summary") or state.issue_summary or ""
        live_description = jira_live.get("description", "")
    else:
        live_summary = state.issue_summary or ""
        live_description = state.description or ""

    artifacts: Dict[str, Any] = {"session_logs": [], "prompt_files": []}
    if include_artifacts:
        artifacts = _collect_session_artifacts(key)
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
    # Plans never auto-start; dashboard does not offer a Start button.
    # Operator: new Mode: build issue, or ai-start-work / ai-execute label.
    can_start = False

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
    db_sessions: List[Any] = []
    if include_artifacts:
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
        db_sessions = find_sessions_for_issue(key, limit=20)
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
        "git_deliveries": _collect_git_deliveries(
            issue_key=key,
            meta=meta,
            jobs=jobs,
        ),
        "retry_history": retry_history,
        "prompts": prompts,
        "session_logs": artifacts["session_logs"],
        "system_logs": issue_log_ring.for_issue(key, limit=500),
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }
