"""Persistent scheduled jobs (fire agent work at a future time)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from src.logger import logger

SCHEDULE_LABEL = "SCHEDULED_AI_JOB"

# scheduled → waiting for fire time
# dispatching → claimed by daemon tick
# dispatched → process_event started
# cancelled → user cancelled before fire
# error → create/dispatch hard failure after record exists
_TERMINAL = frozenset({"dispatched", "cancelled", "error"})


def _default_schedules_dir() -> Path:
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("schedules")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_local_aware(dt: datetime) -> datetime:
    """Treat naive datetimes as local wall clock; leave aware stamps intact."""
    if dt.tzinfo is not None:
        return dt
    tz = datetime.now().astimezone().tzinfo
    return dt.replace(tzinfo=tz)


class ScheduleStore:
    """Scheduled agent jobs stored in yaver.sqlite.

    ``get`` reads the document column. Leftover ``sched_*.json`` is imported
    once by ``ensure_index``, then deleted. A failed write does not fall
    back to a JSON file.
    """

    def __init__(self, schedules_dir: Optional[Path] = None) -> None:
        self.schedules_dir = schedules_dir or _default_schedules_dir()
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index = None
        self._index_ready = False
        try:
            from src.state.schedule_index import ScheduleIndex, default_index_path

            self._index = ScheduleIndex(default_index_path(self.schedules_dir))
        except Exception as e:
            logger.warning(f"Schedule database unavailable: {e}")
            self._index = None

    def ensure_index(self) -> int:
        """Import leftover schedule JSON once. Later calls do not scan.

        A failed import leaves the flag unset so the next call can retry.
        """
        if self._index is None:
            return 0
        with self._lock:
            if self._index_ready:
                return 0
            try:
                n = self._index.reconcile(self.schedules_dir)
            except Exception as e:
                logger.warning(f"Schedule import failed: {e}")
                return 0
            self._index_ready = True
            if n:
                logger.info(f"Imported {n} schedule(s) into yaver.sqlite")
            return n

    def _path(self, schedule_id: str) -> Path:
        safe = schedule_id.replace("/", "_").replace("\\", "_")
        return self.schedules_dir / f"{safe}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        if self._index is None:
            raise RuntimeError(
                f"schedule database unavailable for {rec.get('schedule_id')}"
            )
        self._index.upsert(rec)

    def create(
        self,
        *,
        title: str,
        description: str,
        repository_url: str,
        source_branch: str,
        target_branch: str,
        mode: str,
        scheduled_at: str,
        issue_key: str,
        issue_description: str,
        project_key: str = "",
        issue_type: str = "Task",
        source: str = "new",
        model: str = "",
        backend: str = "",
        mr_iid: int = 0,
        gitlab_host: str = "",
        gitlab_project: str = "",
        merge_request_url: str = "",
        pr_id: int = 0,
        azure_host: str = "",
        azure_collection_url: str = "",
        azure_project: str = "",
        azure_repository: str = "",
        azure_repository_id: str = "",
        repository_urls: Optional[list] = None,
        repository_refs: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Persist a schedule after the Jira issue is known (created or existing)."""
        schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        src = (source or "new").strip().lower()
        if src not in ("new", "existing", "gitlab_mr", "azure_pr"):
            src = "new"
        rec: Dict[str, Any] = {
            "schedule_id": schedule_id,
            "title": title or "",
            "description": description or "",
            "repository_url": repository_url or "",
            "source_branch": source_branch or "",
            "target_branch": target_branch or "",
            "mode": mode or "",
            "model": (model or "").strip(),
            "backend": (backend or "").strip(),
            "issue_type": (issue_type or "Task").strip() or "Task",
            "scheduled_at": scheduled_at,
            "status": "scheduled",
            "issue_key": (issue_key or "").strip().upper(),
            "issue_description": issue_description or "",
            "project_key": project_key or "",
            "label": SCHEDULE_LABEL,
            # new = we created the Jira issue; existing = existing Jira;
            # gitlab_mr = follow-up prompt on an existing merge request
            # azure_pr = follow-up prompt on an existing Azure DevOps PR
            "source": src,
            "mr_iid": int(mr_iid or 0),
            "gitlab_host": (gitlab_host or "").strip(),
            "gitlab_project": (gitlab_project or "").strip(),
            "merge_request_url": (merge_request_url or "").strip(),
            "pr_id": int(pr_id or 0),
            "azure_host": (azure_host or "").strip(),
            "azure_collection_url": (azure_collection_url or "").strip(),
            "azure_project": (azure_project or "").strip(),
            "azure_repository": (azure_repository or "").strip(),
            "azure_repository_id": (azure_repository_id or "").strip(),
            "repository_urls": [
                str(u).strip()
                for u in (repository_urls or [])
                if str(u).strip()
            ][:12],
            "repository_refs": [
                row
                for row in (repository_refs or [])
                if isinstance(row, dict) and str(row.get("url") or "").strip()
            ][:12],
            "created_at": now,
            "updated_at": now,
            "dispatched_at": None,
            "error_message": None,
        }
        with self._lock:
            self._write(rec)
        logger.info(
            f"Schedule created: {schedule_id} issue={rec['issue_key']} "
            f"at={scheduled_at}"
        )
        return rec

    def get(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        sid = (schedule_id or "").strip()
        if not sid or self._index is None:
            return None
        self.ensure_index()
        try:
            return self._index.get(sid)
        except Exception as e:
            logger.error(f"Error loading schedule {schedule_id}: {e}")
            return None

    def update(
        self,
        schedule_id: str,
        *,
        expected_status: Optional[str] = None,
        **fields: Any,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self.get(schedule_id)
            if not rec:
                return None
            if expected_status is not None and (rec.get("status") or "") != expected_status:
                return rec
            new_status = fields.get("status")
            held = (rec.get("status") or "")
            if (
                new_status in ("dispatched", "error")
                and held != "dispatching"
                and expected_status is None
            ):
                return rec
            # Cancel is only for scheduled/error. A claim can land between
            # the unlocked read and this write; do not cover dispatching.
            if (
                new_status == "cancelled"
                and expected_status is None
                and held not in ("scheduled", "error", "cancelled")
            ):
                return rec
            for key, value in fields.items():
                if key == "schedule_id":
                    continue
                rec[key] = value
            rec["updated_at"] = _now_iso()
            try:
                self._write(rec)
            except Exception as e:
                logger.error(f"Error saving schedule {schedule_id}: {e}")
                return None
            return rec

    def claim_due(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a scheduled row for dispatch (scheduled → dispatching)."""
        with self._lock:
            rec = self.get(schedule_id)
            if not rec or (rec.get("status") or "") != "scheduled":
                return None
            rec["status"] = "dispatching"
            rec["updated_at"] = _now_iso()
            try:
                self._write(rec)
            except Exception as e:
                logger.error(f"Error saving schedule {schedule_id}: {e}")
                return None
            return rec

    def claim_for_dispatch(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Claim a scheduled or error row now (ignores ``scheduled_at``)."""
        with self._lock:
            rec = self.get(schedule_id)
            if not rec:
                return None
            if (rec.get("status") or "") not in ("scheduled", "error"):
                return None
            rec["status"] = "dispatching"
            rec["error_message"] = None
            rec["updated_at"] = _now_iso()
            try:
                self._write(rec)
            except Exception as e:
                logger.error(f"Error saving schedule {schedule_id}: {e}")
                return None
            return rec

    def recover_stuck_dispatching(
        self,
        *,
        max_age_seconds: float = 0.0,
        now: Optional[datetime] = None,
        exclude_ids: Optional[Iterable[str]] = None,
    ) -> int:
        """Reset ``dispatching`` rows back to ``scheduled`` after a crash.

        Claim moves ``scheduled → dispatching`` before ``process_event`` finishes
        and marks ``dispatched``. If the daemon dies mid-flight, the row stays
        ``dispatching`` forever (never due, cancel was refused).

        ``max_age_seconds=0`` recovers **all** dispatching rows (startup path).
        Positive age only recovers rows whose ``updated_at`` is older than the
        cutoff (periodic safety net after a lost worker).

        ``exclude_ids`` are live in-process dispatches — never re-open those
        while ``process_event`` is still running (agent jobs can exceed 30 min).

        Returns the number of rows re-opened.
        """
        when = now or datetime.now()
        skip: Set[str] = {
            str(x).strip() for x in (exclude_ids or []) if str(x).strip()
        }
        recovered = 0
        self.ensure_index()
        if self._index is None:
            return 0
        try:
            rows = self._index.records_with_status("dispatching")
        except Exception as e:
            logger.warning(f"Schedule dispatching lookup failed: {e}")
            return 0
        with self._lock:
            for rec in rows:
                if (rec.get("status") or "").lower() != "dispatching":
                    continue
                sid = rec.get("schedule_id") or ""
                if sid in skip:
                    continue
                if max_age_seconds and max_age_seconds > 0:
                    raw = (rec.get("updated_at") or rec.get("created_at") or "").strip()
                    if raw:
                        try:
                            ts = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                            updated = datetime.fromisoformat(ts)
                            if updated.tzinfo is not None and when.tzinfo is None:
                                when_cmp = when.replace(tzinfo=updated.tzinfo)
                            else:
                                when_cmp = when
                            age = (when_cmp - updated).total_seconds()
                            if age < max_age_seconds:
                                continue
                        except ValueError:
                            # Unparseable timestamp — recover rather than stuck forever
                            pass
                rec["status"] = "scheduled"
                rec["error_message"] = None
                rec["updated_at"] = _now_iso()
                try:
                    self._write(rec)
                    recovered += 1
                    logger.info(
                        f"Recovered stuck schedule {rec.get('schedule_id')} "
                        f"(dispatching → scheduled)"
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not recover schedule {rec.get('schedule_id')}: {e}"
                    )
        return recovered

    def list_for_issue(self, issue_key: str, *, limit: int = 20) -> List[Dict[str, Any]]:
        """Schedules whose issue key matches, newest first."""
        key = (issue_key or "").strip().upper()
        if not key or self._index is None:
            return []
        self.ensure_index()
        try:
            return self._index.list_for_issue(key, limit=limit)
        except Exception as e:
            logger.warning(f"Schedule issue list failed: {e}")
            return []

    def list_schedules(
        self,
        *,
        status: Optional[str] = None,
        limit: Optional[int] = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        self.ensure_index()
        if self._index is None:
            return []
        try:
            return self._index.list_records(status=status, limit=limit, offset=offset)
        except Exception as e:
            logger.warning(f"Schedule list failed: {e}")
            return []

    def count_schedules(self, *, status: Optional[str] = None) -> int:
        """How many schedules match *status* (all rows when unset)."""
        self.ensure_index()
        if self._index is None:
            return 0
        try:
            return self._index.count(status=status)
        except Exception as e:
            logger.warning(f"Schedule count failed: {e}")
            return 0

    def has_open_for_issue(
        self,
        issue_key: str,
        *,
        statuses: Iterable[str] = ("scheduled", "dispatching"),
    ) -> bool:
        """True when this issue has a non-terminal schedule (any age / file order)."""
        key = (issue_key or "").strip().upper()
        if not key:
            return False
        want = {str(s or "").strip().lower() for s in statuses if str(s or "").strip()}
        if not want:
            return False
        self.ensure_index()
        if self._index is None:
            return False
        try:
            return self._index.has_issue_status(key, want)
        except Exception as e:
            logger.warning(f"Schedule issue lookup failed: {e}")
            return False

    def list_due(self, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Return schedules with status=scheduled and scheduled_at <= now."""
        when = now or datetime.now()
        due: List[Dict[str, Any]] = []
        for rec in self.list_schedules(status="scheduled", limit=None):
            raw = (rec.get("scheduled_at") or "").strip()
            if not raw:
                continue
            try:
                # Support trailing Z
                ts = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
                at = datetime.fromisoformat(ts)
                # Naive = local wall clock (CLI + dashboard datetime-local).
                # Aware/Z = real UTC instant — never label naive now as UTC.
                if _as_local_aware(at) <= _as_local_aware(when):
                    due.append(rec)
            except ValueError:
                logger.warning(
                    f"Invalid scheduled_at on {rec.get('schedule_id')}: {raw!r}"
                )
        due.sort(key=lambda r: r.get("scheduled_at") or "")
        return due


# Process-wide default store (same pattern as JobStore)
schedule_store = ScheduleStore()
