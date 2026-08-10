"""Persistent scheduled jobs (fire agent work at a future time)."""

from __future__ import annotations

import json
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
    return Path.cwd() / ".jira-agent" / "schedules"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_local_aware(dt: datetime) -> datetime:
    """Treat naive datetimes as local wall clock; leave aware stamps intact."""
    if dt.tzinfo is not None:
        return dt
    tz = datetime.now().astimezone().tzinfo
    return dt.replace(tzinfo=tz)


class ScheduleStore:
    """File-backed store of scheduled agent jobs (one JSON per schedule)."""

    def __init__(self, schedules_dir: Optional[Path] = None) -> None:
        self.schedules_dir = schedules_dir or _default_schedules_dir()
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, schedule_id: str) -> Path:
        safe = schedule_id.replace("/", "_").replace("\\", "_")
        return self.schedules_dir / f"{safe}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        sid = rec["schedule_id"]
        path = self._path(sid)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        tmp.replace(path)

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
    ) -> Dict[str, Any]:
        """Persist a schedule after the Jira issue is known (created or existing)."""
        schedule_id = f"sched_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        src = (source or "new").strip().lower()
        if src not in ("new", "existing"):
            src = "new"
        rec: Dict[str, Any] = {
            "schedule_id": schedule_id,
            "title": title or "",
            "description": description or "",
            "repository_url": repository_url or "",
            "source_branch": source_branch or "",
            "target_branch": target_branch or "",
            "mode": mode or "",
            "issue_type": (issue_type or "Task").strip() or "Task",
            "scheduled_at": scheduled_at,
            "status": "scheduled",
            "issue_key": (issue_key or "").strip().upper(),
            "issue_description": issue_description or "",
            "project_key": project_key or "",
            "label": SCHEDULE_LABEL,
            # new = we created the Jira issue; existing = schedule an issue that already exists
            "source": src,
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
        path = self._path((schedule_id or "").strip())
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
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
            if (
                new_status in ("dispatched", "error")
                and (rec.get("status") or "") != "dispatching"
                and expected_status is None
            ):
                return rec
            for key, value in fields.items():
                if key == "schedule_id":
                    continue
                rec[key] = value
            rec["updated_at"] = _now_iso()
            self._write(rec)
            return rec

    def claim_due(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim a scheduled row for dispatch (scheduled → dispatching)."""
        with self._lock:
            rec = self.get(schedule_id)
            if not rec or (rec.get("status") or "") != "scheduled":
                return None
            rec["status"] = "dispatching"
            rec["updated_at"] = _now_iso()
            self._write(rec)
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
            self._write(rec)
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
        with self._lock:
            for path in list(self.schedules_dir.glob("sched_*.json")):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
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

    def list_schedules(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        with self._lock:
            for path in sorted(
                self.schedules_dir.glob("sched_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if status and (rec.get("status") or "") != status:
                    continue
                items.append(rec)
                if len(items) >= limit:
                    break
        # Sort by scheduled_at then created_at (newest first for list UI)
        items.sort(
            key=lambda r: r.get("scheduled_at") or r.get("created_at") or "",
            reverse=True,
        )
        return items

    def list_due(self, *, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Return schedules with status=scheduled and scheduled_at <= now."""
        when = now or datetime.now()
        due: List[Dict[str, Any]] = []
        for rec in self.list_schedules(status="scheduled", limit=500):
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
