"""Dashboard business assembly: tasks, poll view, safe settings."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.config import settings
from src.dashboard.schemas import (
    MetaResponse,
    PolledIssueItem,
    PollStatusResponse,
    SettingsUpdate,
    SettingsView,
    TaskItem,
    TasksResponse,
)
from src.dashboard.snapshot import PollSnapshotStore, poll_snapshot_store
from src.state.manager import JiraStateManager

if TYPE_CHECKING:
    from src.processor import JobProcessor


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
        key = row.get("key") or ""
        local = sm.get_state(key) if key else None
        issues.append(
            PolledIssueItem(
                key=key,
                summary=row.get("summary") or "",
                jira_status=row.get("jira_status") or "",
                labels=list(row.get("labels") or []),
                assignee=row.get("assignee"),
                matched_label=bool(row.get("matched_label")),
                matched_assignee=bool(row.get("matched_assignee")),
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
    return SettingsView(
        jira_host=settings.jira_host or "",
        jira_board_id=settings.jira_board_id or "",
        jira_projects=settings.jira_projects or "",
        poll_interval_seconds=int(settings.poll_interval_seconds or 30),
        trigger_labels=settings.trigger_labels or "",
        trigger_on_assignment=bool(settings.trigger_on_assignment),
        auto_start_plans=bool(settings.auto_start_plans),
        max_concurrent_jobs=int(settings.max_concurrent_jobs or 1),
        default_branch=settings.default_branch or "develop",
        dashboard_host=getattr(settings, "dashboard_host", "127.0.0.1") or "127.0.0.1",
        dashboard_port=int(getattr(settings, "dashboard_port", 8080) or 8080),
        jira_token_configured=bool(settings.jira_api_token),
        gitlab_pat_configured=bool(settings.gitlab_pat),
        jira_email_configured=bool(getattr(settings, "jira_email", "") or ""),
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
    return build_settings_view()


def build_dashboard_payload(
    *,
    state_manager: Optional[JiraStateManager] = None,
    processor: Optional["JobProcessor"] = None,
    store: Optional[PollSnapshotStore] = None,
) -> Dict[str, Any]:
    return {
        "type": "dashboard",
        "meta": build_meta().model_dump(),
        "tasks": build_tasks(state_manager, processor).model_dump(),
        "poll": build_poll_status(store, state_manager).model_dump(),
        "settings": build_settings_view().model_dump(),
    }
