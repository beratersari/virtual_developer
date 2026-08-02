"""Create scheduled jobs (Jira issue + local record) and fire them when due.

Jira reliability rules
----------------------
* **Hard fail:** creating the Jira issue. If create fails, no schedule record.
* **Soft fail:** transition to In Progress, later comments, re-fetch at dispatch.
  Local state / UI still updates; work still runs.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.config import settings
from src.issue_git_spec import _normalize_branch, _normalize_repo_url
from src.logger import logger
from src.state.schedule_store import SCHEDULE_LABEL, ScheduleStore, schedule_store

if TYPE_CHECKING:
    from src.processor import JobProcessor

# Mirrors issue_git_spec mode aliases (plan | build)
_MODE_ALIASES = {
    "plan": "plan",
    "planning": "plan",
    "prometheus": "plan",
    "build": "build",
    "execute": "build",
    "execution": "build",
    "atlas": "build",
    "implement": "build",
}


def parse_schedule_at(raw: str) -> datetime:
    """Parse ISO-8601 datetime (local or with Z / offset). Raises ValueError."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("scheduled_at is required")
    # Allow space instead of T
    text = text.replace(" ", "T", 1) if " " in text and "T" not in text else text
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def _canonical_mode(mode: str) -> str:
    key = (mode or "").strip().lower()
    if key not in _MODE_ALIASES:
        raise ValueError("mode must be 'plan' or 'build'")
    return _MODE_ALIASES[key]


def build_issue_description(
    *,
    description: str,
    repository_url: str,
    source_branch: str,
    target_branch: str,
    mode: str,
) -> str:
    """Build Jira description with mandatory {params} block for the agent."""
    body = (description or "").strip()
    params = (
        "{params}\n"
        f"Repository: {repository_url}\n"
        f"Source branch: {source_branch}\n"
        f"Target branch: {target_branch}\n"
        f"Mode: {mode}\n"
        "{params}"
    )
    if body:
        return f"{body}\n\n{params}"
    return params


def list_project_issue_types(
    *,
    project_key: Optional[str] = None,
    jira_client: Any = None,
) -> Dict[str, Any]:
    """List creatable Jira issue types for a project (Cloud + on-prem).

    Returns ``{"ok": True, "project_key": ..., "issue_types": [...]}``.
    Each type: ``id``, ``name``, ``subtask``.
    """
    project = (project_key or "").strip() or (
        settings.jira_projects_list[0] if settings.jira_projects_list else ""
    )
    if not project:
        return {"ok": False, "error": "JIRA project key is not configured", "issue_types": []}

    client = jira_client
    close_client = False
    if client is None:
        from src.jira.client import create_jira_client

        client = create_jira_client()
        close_client = True
    try:
        if not hasattr(client, "get_project_issue_types"):
            return {
                "ok": False,
                "error": "Jira client cannot list issue types",
                "project_key": project,
                "issue_types": [],
            }
        raw = client.get_project_issue_types(project) or []
        items: List[Dict[str, Any]] = []
        for it in raw:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            items.append(
                {
                    "id": str(it.get("id") or ""),
                    "name": name,
                    "subtask": bool(it.get("subtask")),
                }
            )
        # Non-subtasks first, then alpha by name
        items.sort(key=lambda x: (x["subtask"], x["name"].lower()))
        return {
            "ok": True,
            "project_key": project,
            "issue_types": items,
        }
    except Exception as e:
        logger.warning(f"list_project_issue_types failed: {e}")
        return {
            "ok": False,
            "error": str(e),
            "project_key": project,
            "issue_types": [],
        }
    finally:
        if close_client and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass


def create_scheduled_job(
    *,
    title: str,
    description: str = "",
    repository_url: str,
    source_branch: str,
    target_branch: str,
    mode: str,
    scheduled_at: str,
    project_key: Optional[str] = None,
    issue_type: str = "Task",
    jira_client: Any = None,
    store: Optional[ScheduleStore] = None,
) -> Dict[str, Any]:
    """Create Jira issue + local schedule. Hard-fails only on issue creation.

    ``issue_type`` is the Jira issue type name preferred for create (e.g.
    ``Task``, ``Story``, ``ExtBug``, or locale names like ``Görev``). The
    client resolves it against the project's creatable types (id preferred).

    Returns ``{"ok": True, "schedule": {...}}`` or ``{"ok": False, "error": "..."}``.
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}

    try:
        mode_c = _canonical_mode(mode)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    itype = (issue_type or "Task").strip() or "Task"

    repo = _normalize_repo_url(repository_url)
    if not repo:
        return {"ok": False, "error": "repository_url is required"}
    src = _normalize_branch(source_branch)
    tgt = _normalize_branch(target_branch)
    if not src:
        return {"ok": False, "error": "source_branch is required"}
    if not tgt:
        return {"ok": False, "error": "target_branch is required"}

    try:
        at_dt = parse_schedule_at(scheduled_at)
    except ValueError as e:
        return {"ok": False, "error": f"invalid scheduled_at: {e}"}

    # Store as ISO without forcing timezone if naive
    scheduled_iso = at_dt.isoformat(timespec="seconds")

    project = (project_key or "").strip() or (
        settings.jira_projects_list[0] if settings.jira_projects_list else ""
    )
    if not project:
        return {"ok": False, "error": "JIRA project key is not configured"}

    issue_description = build_issue_description(
        description=description,
        repository_url=repo,
        source_branch=src,
        target_branch=tgt,
        mode=mode_c,
    )

    client = jira_client
    close_client = False
    if client is None:
        from src.jira.client import create_jira_client

        client = create_jira_client()
        close_client = True

    try:
        created = client.create_issue(
            project=project,
            summary=title,
            description=issue_description,
            issue_type=itype,
            labels=[SCHEDULE_LABEL],
        )
        if not created or not created.get("key"):
            detail = getattr(client, "last_error", None) or (
                "server unavailable or create rejected"
            )
            return {
                "ok": False,
                "error": (
                    f"Failed to create Jira issue: {detail}. "
                    "Schedule was not saved."
                ),
            }
        issue_key = str(created["key"]).strip().upper()

        # Soft: In Progress — do not fail the schedule if Jira is flaky
        try:
            if hasattr(client, "transition_to_in_progress"):
                ok = client.transition_to_in_progress(issue_key)
                if ok:
                    logger.info(f"{issue_key} moved to In Progress (scheduled job)")
                else:
                    logger.warning(
                        f"{issue_key}: could not transition to In Progress "
                        f"(schedule still saved)"
                    )
        except Exception as e:
            logger.warning(
                f"{issue_key}: In Progress transition soft-failed: {e} "
                f"(schedule still saved)"
            )

        ss = store or schedule_store
        rec = ss.create(
            title=title,
            description=(description or "").strip(),
            repository_url=repo,
            source_branch=src,
            target_branch=tgt,
            mode=mode_c,
            scheduled_at=scheduled_iso,
            issue_key=issue_key,
            issue_description=issue_description,
            project_key=project,
            issue_type=itype,
        )
        return {"ok": True, "schedule": rec, "issue_key": issue_key}
    finally:
        if close_client and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass


def list_scheduled_jobs(
    *,
    status: Optional[str] = None,
    limit: int = 200,
    store: Optional[ScheduleStore] = None,
) -> List[Dict[str, Any]]:
    ss = store or schedule_store
    return ss.list_schedules(status=status, limit=limit)


def cancel_scheduled_job(
    schedule_id: str,
    *,
    store: Optional[ScheduleStore] = None,
) -> Dict[str, Any]:
    """Cancel a pending schedule. Does not delete the Jira issue."""
    ss = store or schedule_store
    rec = ss.get(schedule_id)
    if not rec:
        return {"ok": False, "error": f"No schedule {schedule_id}"}
    st = (rec.get("status") or "").lower()
    if st in ("dispatched", "dispatching"):
        return {
            "ok": False,
            "error": f"Cannot cancel schedule in status {st}",
            "schedule": rec,
        }
    if st == "cancelled":
        return {"ok": True, "schedule": rec, "message": "Already cancelled"}
    updated = ss.update(schedule_id, status="cancelled", error_message=None)
    return {"ok": True, "schedule": updated, "message": "Schedule cancelled"}


def _issue_payload_for_dispatch(
    rec: Dict[str, Any],
    *,
    jira_client: Any = None,
) -> Dict[str, Any]:
    """Build a poller-shaped issue dict. Prefer live Jira; fall back to local copy."""
    issue_key = (rec.get("issue_key") or "").upper()
    summary = rec.get("title") or ""
    description = rec.get("issue_description") or ""
    labels = [SCHEDULE_LABEL]

    client = jira_client
    if client is not None and issue_key:
        try:
            live = client.get_issue(issue_key)
            if live:
                fields = live.get("fields") or {}
                if fields.get("summary"):
                    summary = fields["summary"]
                desc = fields.get("description")
                if isinstance(desc, str) and desc.strip():
                    description = desc
                elif desc is not None and not isinstance(desc, str):
                    description = str(desc)
                lab = fields.get("labels") or []
                if isinstance(lab, list) and lab:
                    labels = [str(x) for x in lab]
                return {
                    "key": issue_key,
                    "id": live.get("id"),
                    "fields": {
                        "summary": summary,
                        "description": description,
                        "labels": labels,
                        "status": fields.get("status") or {"name": "In Progress"},
                        "assignee": fields.get("assignee"),
                    },
                }
        except Exception as e:
            logger.warning(
                f"{issue_key}: Jira fetch soft-failed at dispatch: {e}; "
                f"using local schedule snapshot"
            )

    return {
        "key": issue_key,
        "fields": {
            "summary": summary,
            "description": description,
            "labels": labels,
            "status": {"name": "In Progress"},
            "assignee": None,
        },
    }


async def dispatch_due_schedules(
    *,
    processor: "JobProcessor",
    store: Optional[ScheduleStore] = None,
    jira_client: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Claim and start process_event for every due schedule.

    Returns counts: claimed, started, failed.
    """
    ss = store or schedule_store
    due = ss.list_due(now=now)
    claimed = 0
    started = 0
    failed = 0

    client = jira_client
    close_client = False
    if client is None and due:
        try:
            from src.jira.client import create_jira_client

            client = create_jira_client()
            close_client = True
        except Exception as e:
            logger.warning(f"Schedule dispatch: could not open Jira client: {e}")
            client = None

    try:
        for rec in due:
            sid = rec.get("schedule_id") or ""
            claimed_rec = ss.claim_due(sid)
            if not claimed_rec:
                continue
            claimed += 1
            issue_key = (claimed_rec.get("issue_key") or "").upper()
            try:
                issue = _issue_payload_for_dispatch(claimed_rec, jira_client=client)
                event = {
                    "webhookEvent": "jira:issue_created",
                    "issue": issue,
                    "timestamp": int(time.time() * 1000),
                    "scheduled_job": True,
                    "schedule_id": sid,
                }
                # Fire and await so we record outcome; process_event holds slots
                await processor.process_event(event)
                ss.update(
                    sid,
                    status="dispatched",
                    dispatched_at=datetime.now().isoformat(timespec="seconds"),
                    error_message=None,
                )
                started += 1
                logger.info(
                    f"Schedule {sid} dispatched for {issue_key}"
                )
            except Exception as e:
                failed += 1
                logger.exception(
                    f"Schedule {sid} dispatch failed for {issue_key}: {e}", e
                )
                # Leave local UI truthful; soft on Jira. Mark schedule error so
                # we do not spin forever. Operator can re-queue via board/CLI.
                ss.update(
                    sid,
                    status="error",
                    error_message=str(e)[:1000],
                )
    finally:
        if close_client and client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass

    return {
        "ok": True,
        "due": len(due),
        "claimed": claimed,
        "started": started,
        "failed": failed,
    }
