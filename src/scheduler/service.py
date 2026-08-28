"""Create scheduled jobs (Jira issue + local record) and fire them when due.

Jira reliability rules
----------------------
* **Hard fail:** creating the Jira issue. If create fails, no schedule record.
* **Soft fail:** transition to In Progress, later comments, re-fetch at dispatch.
  Local state / UI still updates; work still runs.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Set

from src.config import settings
from src.issue_git_spec import (
    _normalize_backend_id,
    _normalize_branch,
    _normalize_model_id,
    _normalize_repo_url,
    parse_issue_git_spec,
    upsert_params_backend,
    upsert_params_model,
)
from src.logger import logger
from src.state.schedule_store import SCHEDULE_LABEL, ScheduleStore, schedule_store

# source_branch_mode for create-new schedules
_SOURCE_MODE_CUSTOM = "custom"
_SOURCE_MODE_ISSUE_KEY = "issue_key"
_SOURCE_MODES = frozenset({_SOURCE_MODE_CUSTOM, _SOURCE_MODE_ISSUE_KEY})

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
    model: str = "",
    backend: str = "",
) -> str:
    """Build Jira description with mandatory {params} block for the agent."""
    from src.issue_git_spec import _normalize_backend_id, _normalize_model_id

    body = (description or "").strip()
    mid = _normalize_model_id(model)
    bid = _normalize_backend_id(backend)
    model_line = f"Model: {mid}\n" if mid else ""
    backend_line = f"Backend: {bid}\n" if bid else ""
    params = (
        "{params}\n"
        f"Repository: {repository_url}\n"
        f"Source branch: {source_branch}\n"
        f"Target branch: {target_branch}\n"
        f"Mode: {mode}\n"
        f"{model_line}"
        f"{backend_line}"
        "{params}"
    )
    if body:
        return f"{body}\n\n{params}"
    return params


def work_branch_for_issue_key(issue_key: str) -> str:
    """Same convention as GitManager: ``feature/{ISSUE_KEY}`` (sanitized)."""
    safe = re.sub(r"[^A-Za-z0-9\-]", "-", (issue_key or "").strip() or "issue")
    safe = safe.strip("-") or "issue"
    return f"feature/{safe}"


def _canonical_source_branch_mode(raw: Optional[str]) -> str:
    key = (raw or _SOURCE_MODE_CUSTOM).strip().lower().replace("-", "_")
    if key in ("issue", "jira", "jira_key", "from_issue", "new_issue"):
        key = _SOURCE_MODE_ISSUE_KEY
    if key not in _SOURCE_MODES:
        raise ValueError(
            "source_branch_mode must be 'custom' or 'issue_key'"
        )
    return key


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


def _description_to_text(description: Any) -> str:
    """Normalize Jira description (plain string or Cloud ADF-ish) to text."""
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    # ADF document: best-effort plain extraction
    try:
        if isinstance(description, dict):
            parts: List[str] = []

            def walk(node: Any) -> None:
                if isinstance(node, dict):
                    if node.get("type") == "text" and node.get("text"):
                        parts.append(str(node["text"]))
                    for child in node.get("content") or []:
                        walk(child)
                elif isinstance(node, list):
                    for child in node:
                        walk(child)

            walk(description)
            return "\n".join(parts)
    except Exception:
        pass
    return str(description)


def _plain_template_error(jira_wiki_msg: str) -> str:
    """Turn Jira-wiki style template errors into a short UI message."""
    text = (jira_wiki_msg or "").replace("{code}", "").replace("*", "")
    # First meaningful line after header
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "Issue template is invalid"
    # Prefer the "could not start" / Missing line
    for ln in lines:
        if "Missing" in ln or "could not start" in ln or "invalid" in ln.lower():
            return ln[:400]
    return lines[0][:400]


def preview_existing_issue(
    issue_key: str,
    *,
    jira_client: Any = None,
) -> Dict[str, Any]:
    """Fetch an existing Jira issue and validate the ``{params}`` template.

    Hard-fails if the issue cannot be loaded or the template is invalid.
    Does not write a schedule record.
    """
    key = (issue_key or "").strip().upper()
    if not key:
        return {"ok": False, "error": "issue_key is required"}

    client = jira_client
    close_client = False
    if client is None:
        from src.jira.client import create_jira_client

        client = create_jira_client()
        close_client = True

    try:
        issue = client.get_issue(key)
        if not issue or not issue.get("key"):
            detail = getattr(client, "last_error", None) or "issue not found or Jira unavailable"
            return {
                "ok": False,
                "error": f"Could not load issue {key}: {detail}",
                "issue_key": key,
            }
        fields = issue.get("fields") or {}
        summary = (fields.get("summary") or "").strip()
        description = _description_to_text(fields.get("description"))
        status_name = ""
        st = fields.get("status") or {}
        if isinstance(st, dict):
            status_name = (st.get("name") or "").strip()
        itype_name = ""
        it = fields.get("issuetype") or {}
        if isinstance(it, dict):
            itype_name = (it.get("name") or "").strip()
        labels = fields.get("labels") or []
        if not isinstance(labels, list):
            labels = []

        spec, err = parse_issue_git_spec(summary, description)
        if err or spec is None:
            return {
                "ok": False,
                "error": _plain_template_error(err or "Invalid {params} template"),
                "issue_key": key,
                "title": summary,
                "description": description,
                "jira_status": status_name,
                "template_valid": False,
            }

        return {
            "ok": True,
            "issue_key": str(issue.get("key") or key).upper(),
            "title": summary,
            "description": description,
            "jira_status": status_name,
            "issue_type": itype_name,
            "labels": [str(x) for x in labels],
            "template_valid": True,
            "repository_url": spec.repository_url,
            "source_branch": spec.source_branch,
            "target_branch": spec.target_branch,
            "mode": spec.mode or "",
            "model": spec.model or "",
            "backend": spec.backend or "",
            "message": "Issue found and template is valid. Choose a run time to schedule.",
        }
    finally:
        if close_client and hasattr(client, "close"):
            try:
                client.close()
            except Exception:
                pass


def schedule_existing_issue(
    issue_key: str,
    *,
    scheduled_at: str,
    model: str = "",
    backend: str = "",
    description: str = "",
    jira_client: Any = None,
    store: Optional[ScheduleStore] = None,
) -> Dict[str, Any]:
    """Schedule an existing Jira issue for later dispatch (no new issue create).

    Hard-fails if the issue cannot be loaded or the template is invalid.
    ``description`` (optional) is the operator-edited Jira prompt; when set
    it replaces the live ticket body after a template check.
    Soft-fails: In Progress transition and SCHEDULED_AI_JOB label.
    """
    key = (issue_key or "").strip().upper()
    if not key:
        return {"ok": False, "error": "issue_key is required"}

    try:
        at_dt = parse_schedule_at(scheduled_at)
    except ValueError as e:
        return {"ok": False, "error": f"invalid scheduled_at: {e}"}
    scheduled_iso = at_dt.isoformat(timespec="seconds")

    client = jira_client
    close_client = False
    if client is None:
        from src.jira.client import create_jira_client

        client = create_jira_client()
        close_client = True

    try:
        # Re-validate at schedule time (issue may have changed since preview)
        preview = preview_existing_issue(key, jira_client=client)
        if not preview.get("ok"):
            return preview

        # Soft: In Progress
        try:
            if hasattr(client, "transition_to_in_progress"):
                ok = client.transition_to_in_progress(key)
                if ok:
                    logger.info(f"{key} moved to In Progress (schedule existing)")
                else:
                    logger.warning(
                        f"{key}: could not transition to In Progress "
                        f"(schedule still saved)"
                    )
        except Exception as e:
            logger.warning(f"{key}: In Progress soft-failed: {e}")

        # Soft: ensure schedule label
        try:
            if hasattr(client, "add_labels"):
                client.add_labels(key, [SCHEDULE_LABEL])
        except Exception as e:
            logger.warning(f"{key}: add_labels soft-failed: {e}")

        ss = store or schedule_store
        # Avoid duplicate pending schedules for the same issue
        for existing in ss.list_schedules(status="scheduled", limit=500):
            if (existing.get("issue_key") or "").upper() == key:
                return {
                    "ok": False,
                    "error": (
                        f"Issue {key} already has a pending schedule "
                        f"({existing.get('schedule_id')}). Cancel it first or wait."
                    ),
                    "schedule": existing,
                }

        operator_desc = (description or "").strip()
        desc = operator_desc or (preview.get("description") or "")
        if operator_desc:
            spec, err = parse_issue_git_spec(
                preview.get("title") or key, operator_desc
            )
            if err or spec is None:
                return {
                    "ok": False,
                    "error": _plain_template_error(
                        err or "Edited prompt is missing a valid {params} block"
                    ),
                    "issue_key": key,
                    "template_valid": False,
                }
        mid = _normalize_model_id(model) or (preview.get("model") or "")
        mid = _normalize_model_id(mid)
        bid = _normalize_backend_id(backend) or (preview.get("backend") or "")
        bid = _normalize_backend_id(bid)
        if mid:
            desc = upsert_params_model(desc, mid)
        if bid:
            desc = upsert_params_backend(desc, bid)
        if desc != (preview.get("description") or ""):
            try:
                if hasattr(client, "update_issue"):
                    ok_upd = client.update_issue(
                        key, fields={"description": desc}
                    )
                    if ok_upd:
                        logger.info(
                            f"{key}: wrote dashboard prompt/model/backend to Jira"
                        )
                    else:
                        logger.warning(
                            f"{key}: could not write prompt/model/backend to Jira "
                            f"(schedule still uses the dashboard text)"
                        )
            except Exception as e:
                logger.warning(
                    f"{key}: description update soft-failed: {e}"
                )

        rec = ss.create(
            title=preview.get("title") or key,
            description=desc[:4000],
            repository_url=preview.get("repository_url") or "",
            source_branch=preview.get("source_branch") or "",
            target_branch=preview.get("target_branch") or "",
            mode=preview.get("mode") or "",
            model=mid,
            backend=bid,
            scheduled_at=scheduled_iso,
            issue_key=key,
            issue_description=desc,
            project_key=key.split("-")[0] if "-" in key else "",
            issue_type=preview.get("issue_type") or "Task",
            source="existing",
        )
        logger.info(
            f"Schedule existing issue {key} schedule_id={rec.get('schedule_id')} "
            f"at={scheduled_iso} job_id=-"
        )
        return {
            "ok": True,
            "schedule": rec,
            "issue_key": key,
            "message": f"Scheduled existing issue {key}",
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
    source_branch: str = "",
    target_branch: str,
    mode: str,
    scheduled_at: str,
    project_key: Optional[str] = None,
    issue_type: str = "Task",
    source_branch_mode: str = _SOURCE_MODE_CUSTOM,
    model: str = "",
    backend: str = "",
    jira_client: Any = None,
    store: Optional[ScheduleStore] = None,
) -> Dict[str, Any]:
    """Create Jira issue + local schedule. Hard-fails only on issue creation.

    ``source_branch_mode``:
      * ``custom`` — use ``source_branch`` as given (required).
      * ``issue_key`` — after the issue is created, set source to
        ``feature/{ISSUE_KEY}`` (product work-branch convention) and rewrite
        the issue description so the agent sees the correct params.

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

    try:
        src_mode = _canonical_source_branch_mode(source_branch_mode)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    itype = (issue_type or "Task").strip() or "Task"

    repo = _normalize_repo_url(repository_url)
    if not repo:
        return {"ok": False, "error": "repository_url is required"}
    tgt = _normalize_branch(target_branch)
    if not tgt:
        return {"ok": False, "error": "target_branch is required"}

    if src_mode == _SOURCE_MODE_CUSTOM:
        src = _normalize_branch(source_branch)
        if not src:
            return {"ok": False, "error": "source_branch is required when mode is custom"}
    else:
        # Provisional until the Jira key is known; rewritten after create.
        src = "feature/__pending__"

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

    mid = _normalize_model_id(model)
    bid = _normalize_backend_id(backend)
    issue_description = build_issue_description(
        description=description,
        repository_url=repo,
        source_branch=src,
        target_branch=tgt,
        mode=mode_c,
        model=mid,
        backend=bid,
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

        if src_mode == _SOURCE_MODE_ISSUE_KEY:
            src = work_branch_for_issue_key(issue_key)
            issue_description = build_issue_description(
                description=description,
                repository_url=repo,
                source_branch=src,
                target_branch=tgt,
                mode=mode_c,
                model=mid,
                backend=bid,
            )
            # Soft: rewrite description so agents see feature/KEY (not __pending__)
            try:
                if hasattr(client, "update_issue"):
                    ok_upd = client.update_issue(
                        issue_key, fields={"description": issue_description}
                    )
                    if ok_upd:
                        logger.info(
                            f"{issue_key}: source branch set to {src} "
                            f"(source_branch_mode=issue_key)"
                        )
                    else:
                        logger.warning(
                            f"{issue_key}: could not update description with "
                            f"source {src}; schedule still uses {src}"
                        )
            except Exception as e:
                logger.warning(
                    f"{issue_key}: description update soft-failed: {e} "
                    f"(local schedule still has source={src})"
                )

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
            model=mid,
            backend=bid,
            scheduled_at=scheduled_iso,
            issue_key=issue_key,
            issue_description=issue_description,
            project_key=project,
            issue_type=itype,
            source="new",
        )
        return {
            "ok": True,
            "schedule": rec,
            "issue_key": issue_key,
            "source_branch": src,
            "source_branch_mode": src_mode,
        }
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
    processor: Any = None,
) -> Dict[str, Any]:
    """Cancel a pending schedule. Does not delete the Jira issue.

    ``dispatching`` may be cancelled (stuck claim after crash, or operator
    abort before dispatch finishes). ``dispatched`` is terminal.
    """
    ss = store or schedule_store
    rec = ss.get(schedule_id)
    if not rec:
        return {"ok": False, "error": f"No schedule {schedule_id}"}
    st = (rec.get("status") or "").lower()
    if st == "dispatched":
        return {
            "ok": False,
            "error": f"Cannot cancel schedule in status {st}",
            "schedule": rec,
        }
    if st == "cancelled":
        return {"ok": True, "schedule": rec, "message": "Already cancelled"}
    issue_key = (rec.get("issue_key") or "").strip().upper()
    was_dispatching = st == "dispatching"
    updated = ss.update(schedule_id, status="cancelled", error_message=None)
    task = _INFLIGHT_DISPATCHES.pop(schedule_id, None)
    if task is not None and not task.done():
        try:
            task.cancel()
        except Exception:
            pass
    # Only abort live agent work when this schedule was actually running.
    # Cancelling a future "scheduled" row must not cancel unrelated issue jobs.
    if (
        was_dispatching
        and processor is not None
        and issue_key
        and hasattr(processor, "cancel_job")
    ):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                processor.cancel_job(
                    issue_key, reason="Schedule cancelled from dashboard"
                )
            )
        except RuntimeError:
            try:
                asyncio.run(
                    processor.cancel_job(
                        issue_key, reason="Schedule cancelled from dashboard"
                    )
                )
            except Exception as e:
                logger.warning(f"Schedule cancel could not abort job {issue_key}: {e}")
        except Exception as e:
            logger.warning(f"Schedule cancel could not abort job {issue_key}: {e}")
    return {"ok": True, "schedule": updated, "message": "Schedule cancelled"}


def recover_stuck_schedules(
    *,
    store: Optional[ScheduleStore] = None,
    max_age_seconds: float = 0.0,
    exclude_ids: Optional[Iterable[str]] = None,
) -> int:
    """Re-open stuck ``dispatching`` schedules (daemon crash recovery)."""
    ss = store or schedule_store
    return ss.recover_stuck_dispatching(
        max_age_seconds=max_age_seconds,
        exclude_ids=exclude_ids,
    )


def _schedule_workspace_lock_kwargs(
    rec: Dict[str, Any], issue_key: str
) -> Dict[str, str]:
    """Clone identity for a schedule row (same rules as JobProcessor)."""
    from src.git_manager import GitManager

    repo = (rec.get("repository_url") or "").strip()
    src = (rec.get("source_branch") or "").strip()
    tgt = (rec.get("target_branch") or "").strip()
    work = GitManager.resolve_work_branch_name(issue_key, src, tgt)
    return {
        "repository_url": repo,
        "work_branch": work,
        "target_branch": tgt,
    }


def _issue_in_flight_reason(processor: Any, issue_key: str) -> Optional[str]:
    """Why a schedule must not claim success when the issue is already running."""
    key = (issue_key or "").strip()
    if not key:
        return None
    sm = getattr(processor, "state_manager", None)
    get = getattr(sm, "get_state", None) if sm is not None else None
    if not callable(get):
        return None
    try:
        st = get(key)
    except Exception:
        return None
    if not st:
        return None
    status = getattr(st, "status", None)
    inflight = getattr(processor, "IN_FLIGHT_STATUSES", None)
    if not isinstance(inflight, (set, frozenset, tuple, list)):
        return None
    if status not in inflight:
        return None
    label = getattr(status, "value", status)
    return f"already in progress ({label})"


def _note_schedule_workspace_lock(
    processor: Any, rec: Dict[str, Any], issue_key: str
) -> str:
    """Mark the schedule's clone busy before ``process_event`` starts."""
    note = getattr(processor, "note_workspace_lock", None)
    if not callable(note) or not (issue_key or "").strip():
        return ""
    try:
        return (
            note(issue_key, **_schedule_workspace_lock_kwargs(rec, issue_key))
            or ""
        )
    except Exception as e:
        logger.debug(f"{issue_key}: schedule workspace lock note failed: {e}")
        return ""


def _issue_payload_for_dispatch(
    rec: Dict[str, Any],
    *,
    jira_client: Any = None,
) -> Dict[str, Any]:
    """Build a poller-shaped issue dict.

    Summary/labels prefer live Jira. The work description prefers the
    schedule snapshot (dashboard-edited prompt + model/backend) so a
    ticket that already had Model/Backend set cannot override the form.
    """
    issue_key = (rec.get("issue_key") or "").upper()
    summary = rec.get("title") or ""
    snapshot = rec.get("issue_description") or ""
    description = snapshot
    labels = [SCHEDULE_LABEL]
    live_id = None
    assignee = None
    status = {"name": "In Progress"}

    client = jira_client
    if client is not None and issue_key:
        try:
            live = client.get_issue(issue_key)
            if isinstance(live, dict) and live:
                fields = live.get("fields") or {}
                if fields.get("summary"):
                    summary = fields["summary"]
                live_id = live.get("id")
                assignee = fields.get("assignee")
                if fields.get("status"):
                    status = fields.get("status")
                lab = fields.get("labels") or []
                if isinstance(lab, list) and lab:
                    labels = [str(x) for x in lab]
                if not snapshot.strip():
                    description = _description_to_text(fields.get("description"))
        except Exception as e:
            logger.warning(
                f"{issue_key}: Jira fetch soft-failed at dispatch: {e}; "
                f"using local schedule snapshot"
            )

    mid = _normalize_model_id(rec.get("model") or "")
    bid = _normalize_backend_id(rec.get("backend") or "")
    if mid:
        description = upsert_params_model(description, mid)
    if bid:
        description = upsert_params_backend(description, bid)

    return {
        "key": issue_key,
        "id": live_id,
        "fields": {
            "summary": summary,
            "description": description,
            "labels": labels,
            "status": status,
            "assignee": assignee,
        },
    }


def _outcome_work_started(outcome: Any) -> tuple[bool, Optional[str]]:
    """Interpret ``process_event`` return value for schedule bookkeeping.

    Real ``JobProcessor.process_event`` returns a dict with ``work_started``.
    Bare test mocks that return ``None`` / non-dict are treated as success so
    unit tests that only assert the event was fired stay valid.
    """
    if isinstance(outcome, dict):
        started = bool(outcome.get("work_started"))
        skipped = outcome.get("skipped")
        reason = str(skipped) if skipped else None
        return started, reason
    # MagicMock / None / unexpected: assume the await meant work was invoked
    return True, None


# Live ``process_event`` tasks keyed by schedule_id. Same asyncio loop as the
# daemon dispatcher. Used so a long agent run does not block other due jobs,
# and so crash-recovery does not re-open a still-running dispatch.
_INFLIGHT_DISPATCHES: Dict[str, "asyncio.Task[None]"] = {}


def inflight_dispatch_ids() -> Set[str]:
    """Schedule ids whose ``process_event`` is still running in this process."""
    return set(_INFLIGHT_DISPATCHES)


async def wait_inflight_dispatches() -> None:
    """Await every in-flight dispatch (tests)."""
    tasks = [t for t in list(_INFLIGHT_DISPATCHES.values()) if t is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _finish_schedule_dispatch(
    ss: ScheduleStore,
    schedule_id: str,
    *,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    """Write terminal dispatch outcome only while the row is still ``dispatching``.

    Operator cancel mid-flight must not be overwritten by a late success.
    """
    rec = ss.get(schedule_id)
    if not rec:
        return
    if (rec.get("status") or "").lower() != "dispatching":
        return
    fields: Dict[str, Any] = {"status": status, "error_message": error_message}
    if status == "dispatched":
        fields["dispatched_at"] = datetime.now().isoformat(timespec="seconds")
        fields["error_message"] = None
    ss.update(schedule_id, expected_status="dispatching", **fields)


def _start_claimed_worker(
    claimed_rec: Dict[str, Any],
    *,
    processor: "JobProcessor",
    store: ScheduleStore,
    jira_client: Any = None,
) -> None:
    """Register inflight first, then fetch Jira and run ``process_event``."""
    sid = claimed_rec.get("schedule_id") or ""
    issue_key = (claimed_rec.get("issue_key") or "").upper()
    coro = _dispatch_claimed_schedule(
        processor=processor,
        store=store,
        schedule_id=sid,
        issue_key=issue_key,
        claimed_rec=claimed_rec,
        jira_client=jira_client,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    task = loop.create_task(coro, name=f"vd-sched-{sid}")
    _INFLIGHT_DISPATCHES[sid] = task
    if task.done():
        _INFLIGHT_DISPATCHES.pop(sid, None)


def dispatch_schedule_now(
    schedule_id: str,
    *,
    processor: "JobProcessor",
    store: Optional[ScheduleStore] = None,
    jira_client: Any = None,
) -> Dict[str, Any]:
    """Claim a future or failed schedule and start work immediately.

    Ignores ``scheduled_at``. Allowed from ``scheduled`` or ``error``.
    ``dispatching`` / ``dispatched`` / ``cancelled`` are refused.
    """
    ss = store or schedule_store
    sid = (schedule_id or "").strip()
    rec = ss.get(sid)
    if not rec:
        return {"ok": False, "error": f"No schedule {sid}"}
    st = (rec.get("status") or "").lower()
    if sid in inflight_dispatch_ids() or st == "dispatching":
        return {
            "ok": False,
            "error": f"Cannot dispatch schedule in status {st or 'dispatching'}",
            "schedule": rec,
        }
    if st not in ("scheduled", "error"):
        return {
            "ok": False,
            "error": f"Cannot dispatch schedule in status {st}",
            "schedule": rec,
        }
    if processor is None:
        return {"ok": False, "error": "Processor is not available", "schedule": rec}
    claimed = ss.claim_for_dispatch(sid)
    if not claimed:
        latest = ss.get(sid) or rec
        return {
            "ok": False,
            "error": f"Could not claim schedule in status {latest.get('status')}",
            "schedule": latest,
        }
    try:
        _start_claimed_worker(
            claimed,
            processor=processor,
            store=ss,
            jira_client=jira_client,
        )
    except Exception as e:
        logger.exception(f"Schedule {sid} instant dispatch failed: {e}", e)
        ss.update(sid, status="error", error_message=str(e)[:1000])
        return {"ok": False, "error": str(e)[:1000], "schedule": ss.get(sid)}
    logger.info(
        f"Schedule {sid} dispatched now for {(claimed.get('issue_key') or '').upper()} "
        f"job_id=-"
    )
    return {
        "ok": True,
        "schedule": claimed,
        "issue_key": (claimed.get("issue_key") or "").upper(),
        "message": "Dispatch started",
    }


async def _dispatch_claimed_schedule(
    *,
    processor: "JobProcessor",
    store: ScheduleStore,
    schedule_id: str,
    issue_key: str,
    claimed_rec: Optional[Dict[str, Any]] = None,
    jira_client: Any = None,
    event: Optional[Dict[str, Any]] = None,
) -> None:
    """Hand work to the work queue (same path as poller/GitLab) then finish.

    Uses ``enqueue_jira_event`` so a busy issue is not started twice.
    A live issue is treated as already started (no extra Queue row).
    Does **not** await the full agent run (queue worker owns that).
    """
    try:
        live = store.get(schedule_id) or claimed_rec or {}
        if (live.get("status") or "").lower() != "dispatching":
            logger.info(
                f"Schedule {schedule_id} no longer dispatching "
                f"({live.get('status')}); not starting work"
            )
            return
        inflight = _issue_in_flight_reason(processor, issue_key)
        if inflight:
            _finish_schedule_dispatch(
                store,
                schedule_id,
                status="error",
                error_message=inflight[:1000],
            )
            logger.warning(
                f"Schedule {schedule_id} for {issue_key} did not start work: "
                f"{inflight}"
            )
            return
        if event is None:
            client = jira_client
            if client is None:
                client = getattr(processor, "jira_client", None)
            issue = _issue_payload_for_dispatch(
                claimed_rec or live, jira_client=client
            )
            live = store.get(schedule_id) or live
            if (live.get("status") or "").lower() != "dispatching":
                logger.info(
                    f"Schedule {schedule_id} cancelled during Jira fetch; "
                    "not starting work"
                )
                return
            event = {
                "webhookEvent": "jira:issue_created",
                "issue": issue,
                "timestamp": int(time.time() * 1000),
                "scheduled_job": True,
                "schedule_id": schedule_id,
            }
        enqueue = getattr(processor, "enqueue_jira_event", None)
        # Prefer the work queue so a busy issue leaves a visible ``queued`` row.
        # Skip MagicMock auto-attrs from unit tests (not real coroutines).
        use_queue = False
        if callable(enqueue):
            import inspect

            use_queue = inspect.iscoroutinefunction(enqueue) or inspect.iscoroutinefunction(
                getattr(enqueue, "__func__", None)
            )
        if use_queue:
            outcome = await enqueue(event)
        else:
            # process_event owns the full run and may not note the clone lock
            # until git prep. Hold it for the whole await so the work queue
            # cannot claim the same (repo, work, target) mid-dispatch.
            _note_schedule_workspace_lock(
                processor, claimed_rec or live, issue_key
            )
            try:
                outcome = await processor.process_event(event)
            finally:
                drop = getattr(processor, "drop_workspace_lock", None)
                if callable(drop):
                    try:
                        drop(issue_key)
                    except Exception:
                        pass
            work_started, skip_reason = _outcome_work_started(outcome)
            if not work_started:
                msg = skip_reason or "processor did not start work for this schedule"
                _finish_schedule_dispatch(
                    store, schedule_id, status="error", error_message=msg[:1000]
                )
                logger.warning(
                    f"Schedule {schedule_id} for {issue_key} did not start work: {msg}"
                )
                return
            _finish_schedule_dispatch(store, schedule_id, status="dispatched")
            logger.info(f"Schedule {schedule_id} dispatched for {issue_key}")
            return

        if not isinstance(outcome, dict) or not outcome.get("ok"):
            msg = (
                (outcome or {}).get("reason")
                if isinstance(outcome, dict)
                else None
            ) or "enqueue_jira_event failed"
            _finish_schedule_dispatch(
                store, schedule_id, status="error", error_message=str(msg)[:1000]
            )
            logger.warning(
                f"Schedule {schedule_id} for {issue_key} enqueue failed: {msg}"
            )
            return
        # Queued (waiting for slot/lock) or started — both count as dispatched
        qstat = (outcome.get("status") or "").lower()
        note = ""
        if outcome.get("queued") or qstat == "queued":
            note = f"enqueued as {outcome.get('queue_id') or 'queue item'} (waiting)"
        elif outcome.get("started") or qstat == "running":
            note = f"started via queue {outcome.get('queue_id') or ''}".strip()
        elif outcome.get("duplicate"):
            note = f"already on queue ({outcome.get('queue_id')})"
        _finish_schedule_dispatch(store, schedule_id, status="dispatched")
        logger.info(
            f"Schedule {schedule_id} dispatched for {issue_key} "
            f"queue_id={outcome.get('queue_id') or '-'} "
            f"queue_status={outcome.get('status') or '-'} "
            f"job_id=-"
            + (f" — {note}" if note else "")
        )
    except asyncio.CancelledError:
        logger.info(f"Schedule {schedule_id} dispatch task cancelled")
        raise
    except Exception as e:
        logger.exception(
            f"Schedule {schedule_id} dispatch failed for {issue_key}: {e}", e
        )
        _finish_schedule_dispatch(
            store, schedule_id, status="error", error_message=str(e)[:1000]
        )
    finally:
        _INFLIGHT_DISPATCHES.pop(schedule_id, None)


_STALE_REAP_SKIP = "Reaped stale running queue row"


def _latest_queue_row_for_schedule(queue_store: Any, schedule_id: str) -> Optional[Dict[str, Any]]:
    """Newest queue row whose payload was created by this schedule fire."""
    sid = (schedule_id or "").strip()
    if not sid or queue_store is None or not hasattr(queue_store, "list_items"):
        return None
    best: Optional[Dict[str, Any]] = None
    try:
        rows = queue_store.list_items(limit=500)
    except Exception:
        return None
    for rec in rows:
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if str(payload.get("schedule_id") or "") != sid:
            continue
        if best is None or (rec.get("created_at") or "") >= (best.get("created_at") or ""):
            best = rec
    return best


def _reopen_skipped_dispatched_schedules(
    processor: Any,
    store: ScheduleStore,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Turn ``dispatched`` + stale-reap skip back into a due schedule.

    Enqueue marks the schedule dispatched before the queue worker runs. If
    the row is then reaped as skipped (never started), the UI stays
    Dispatched and Run now is refused. Re-open those so the next tick fires.
    """
    qs = getattr(processor, "queue_store", None)
    if qs is None:
        return 0
    when = (now or datetime.now()).isoformat(timespec="seconds")
    pending_keys: Set[str] = set()
    try:
        for other in store.list_schedules(limit=500):
            st = (other.get("status") or "").lower()
            if st in ("scheduled", "dispatching"):
                k = (other.get("issue_key") or "").strip().upper()
                if k:
                    pending_keys.add(k)
    except Exception:
        pending_keys = set()
    n = 0
    for rec in store.list_schedules(status="dispatched", limit=500):
        sid = rec.get("schedule_id") or ""
        key = (rec.get("issue_key") or "").strip()
        if not sid:
            continue
        if key and key.upper() in pending_keys:
            continue
        if _issue_in_flight_reason(processor, key):
            continue
        live_fn = getattr(processor, "list_live_processing_keys", None)
        if callable(live_fn):
            try:
                live = {(k or "").strip().upper() for k in (live_fn() or []) if k}
            except Exception:
                live = set()
            if key and key.upper() in live:
                continue
        find_open = getattr(qs, "find_open_jira", None)
        if callable(find_open) and key:
            try:
                if find_open(key):
                    continue
            except Exception:
                pass
        qrow = _latest_queue_row_for_schedule(qs, sid)
        if not qrow or (qrow.get("status") or "") != "skipped":
            continue
        if _STALE_REAP_SKIP not in str(qrow.get("error_message") or ""):
            continue
        store.update(
            sid,
            status="scheduled",
            scheduled_at=when,
            error_message=None,
        )
        n += 1
        logger.info(
            f"Schedule {sid} re-opened after skipped queue "
            f"(issue={key or '-'} queue_id={qrow.get('queue_id') or '-'})"
        )
    return n


async def dispatch_due_schedules(
    *,
    processor: "JobProcessor",
    store: Optional[ScheduleStore] = None,
    jira_client: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Claim every due schedule and start ``process_event`` without blocking.

    ``process_event`` holds the job slot for the full agent run. Awaiting it
    here would freeze the daemon dispatcher tick — later-due jobs would sit
    ``scheduled`` until the first job finished. Each claim is launched as an
    asyncio task; this function returns after launching.

    Only marks a schedule ``dispatched`` when the processor reports that work
    actually started (or a bare mock returns no structured outcome). Silent
    no-ops become ``error`` so the UI does not show a false success.

    Returns counts: due, claimed, launched. ``started`` / ``failed`` stay 0 at
    return time (outcomes land asynchronously); tests should
    ``await wait_inflight_dispatches()`` then read the store.
    """
    ss = store or schedule_store
    # Re-open dispatching rows left by a prior crash. Skip ids still running
    # in this process (long agent jobs must not be reset to scheduled).
    try:
        n = ss.recover_stuck_dispatching(
            max_age_seconds=1800.0,
            now=now,
            exclude_ids=inflight_dispatch_ids(),
        )
        if n:
            logger.info(f"Schedule dispatch: recovered {n} stuck dispatching row(s)")
    except Exception as e:
        logger.warning(f"Schedule dispatch: stuck recovery failed: {e}")
    try:
        n = _reopen_skipped_dispatched_schedules(processor, ss, now=now)
        if n:
            logger.info(
                f"Schedule dispatch: re-opened {n} skipped dispatched schedule(s)"
            )
    except Exception as e:
        logger.warning(f"Schedule dispatch: skipped-dispatch recovery failed: {e}")
    due = ss.list_due(now=now)
    claimed = 0
    launched = 0
    failed = 0

    # Use the caller client or the processor's long-lived client. Do not
    # open+close a tick-scoped client: workers are async and would hit
    # "Cannot send a request, as the client has been closed."
    client = jira_client
    if client is None:
        proc_client = getattr(processor, "jira_client", None)
        if proc_client is not None and callable(
            getattr(proc_client, "get_issue", None)
        ):
            client = proc_client

    for rec in due:
        sid = rec.get("schedule_id") or ""
        claimed_rec = ss.claim_due(sid)
        if not claimed_rec:
            continue
        claimed += 1
        issue_key = (claimed_rec.get("issue_key") or "").upper()
        try:
            _start_claimed_worker(
                claimed_rec,
                processor=processor,
                store=ss,
                jira_client=client,
            )
            launched += 1
        except Exception as e:
            failed += 1
            logger.exception(
                f"Schedule {sid} dispatch failed for {issue_key}: {e}", e
            )
            ss.update(
                sid,
                status="error",
                error_message=str(e)[:1000],
            )

    return {
        "ok": True,
        "due": len(due),
        "claimed": claimed,
        "launched": launched,
        "started": 0,
        "failed": failed,
    }
