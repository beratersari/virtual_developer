"""Polling-based JIRA issue discovery."""

import hashlib
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from src.config import settings
from src.dashboard.snapshot import poll_snapshot_store
from src.jira.client import JiraClient
from src.logger import logger
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus


class JiraPoller:
    """Polling-based JIRA issue discovery using board/sprint."""

    def __init__(
        self,
        client: Optional[JiraClient] = None,
        interval_seconds: Optional[int] = None,
        board_id: Optional[str] = None,
        state_manager: Optional[JiraStateManager] = None,
    ):
        self.client = client or JiraClient()
        self.interval = interval_seconds or settings.poll_interval_seconds
        self.board_id = board_id or settings.jira_board_id
        # Prefer shared manager from daemon/processor (same process lock + dir)
        self.state_manager = state_manager or JiraStateManager()

        logger.info(
            f"Initializing JiraPoller - interval: {self.interval}s, "
            f"board_id: {self.board_id or 'not configured'}"
        )

        if not self.board_id:
            logger.warning("JIRA_BOARD_ID not configured, board polling disabled")

        self._last_check: Optional[datetime] = None
        self._seen_issues: Set[str] = set()
        # Last observed Jira status name (lowercased) per issue — used to detect
        # real transitions into "To Do" rather than re-queueing every poll.
        self._last_jira_status: Dict[str, str] = {}
        self._running = False
        self._handler: Optional[Callable[[dict], None]] = None

    @staticmethod
    def _assignee_looks_like_bot(assignee: Optional[dict]) -> bool:
        """True when assignee name matches any configured bot name fragment.

        Fragments come from ``TRIGGER_ASSIGNEE_NAMES`` (see settings
        ``trigger_assignee_names_list``). Match is case-insensitive substring
        against displayName, name, and key.
        """
        if not assignee:
            return False
        candidates = [
            (assignee.get("displayName") or "").lower(),
            (assignee.get("name") or "").lower(),
            (assignee.get("key") or "").lower(),
        ]
        needles = settings.trigger_assignee_names_list
        if not needles:
            return False
        for name in candidates:
            if not name:
                continue
            for needle in needles:
                if needle and needle in name:
                    return True
        return False

    def _is_assigned_to_jira_ai_bot(self, issue_key: str, fields: Optional[dict] = None) -> bool:
        # Prefer assignee already present on the board payload (avoids N+1 GET)
        if fields is not None:
            assignee = fields.get("assignee")
            if assignee is not None or "assignee" in fields:
                return self._assignee_looks_like_bot(assignee)
        try:
            issue = self.client.get_issue(issue_key)
            if not issue:
                logger.debug(f"{issue_key} not found, skipping assignee check")
                return False
            assignee = issue.get("fields", {}).get("assignee")
            return self._assignee_looks_like_bot(assignee)
        except Exception as e:
            logger.warning(f"Error checking assignee for {issue_key}: {e}")
            return False

    @staticmethod
    def _is_todo_status_name(name: str) -> bool:
        """True when a lowercased Jira status *name* is To Do / backlog-like."""
        n = (name or "").strip().lower()
        todo_names = {
            "to do",
            "todo",
            "open",
            "backlog",
            "new",
            "yapılacaklar",  # Turkish
            "yapilacaklar",
        }
        return n in todo_names

    @staticmethod
    def _is_todo_status(fields: dict) -> bool:
        """True for To Do / backlog-like columns (locale-safe via statusCategory)."""
        status = fields.get("status") or {}
        name = (status.get("name") or "").strip().lower()
        category_key = ((status.get("statusCategory") or {}).get("key") or "").lower()

        # Jira statusCategory key "new" = blue To Do column in every language
        if category_key == "new":
            return True

        return JiraPoller._is_todo_status_name(name)

    def poll_board(self) -> List[dict]:
        if not self.board_id:
            logger.debug("No board_id configured, skipping poll")
            poll_snapshot_store.end_poll(
                source="unconfigured",
                issues=[],
                interval_seconds=self.interval,
                error="JIRA_BOARD_ID not configured",
            )
            return []

        # Lightweight fields for board scan (no description — large ADF payloads)
        issue_fields = [
            "key",
            "summary",
            "labels",
            "assignee",
            "status",
            "issuetype",
            "parent",
        ]

        logger.debug(f"Polling board {self.board_id}")
        sprint = self.client.get_active_sprint(self.board_id)
        if sprint:
            sprint_id = sprint["id"]
            sprint_name = sprint.get("name", "unknown")
            logger.info(f"Found active sprint: {sprint_name} (id: {sprint_id})")
            issues = self.client.get_sprint_issues(
                sprint_id,
                fields=issue_fields,
                max_results=100,
            )
            source = f"sprint {sprint_name}"
        else:
            logger.info(
                f"No active sprint on board {self.board_id}; "
                f"loading issues from board (Kanban/simple)"
            )
            issues = self.client.get_board_issues(
                self.board_id,
                fields=issue_fields,
                max_results=100,
            )
            source = f"board {self.board_id}"

        if not issues:
            logger.debug(f"No issues found from {source}")
            poll_snapshot_store.end_poll(
                source=source,
                issues=[],
                interval_seconds=self.interval,
            )
            self._last_check = datetime.now()
            return []

        logger.debug(f"Found {len(issues)} issues from {source}")

        trigger_labels = set(settings.trigger_labels_list)
        logger.debug(f"Trigger labels: {trigger_labels}")

        new_issues = []
        todo_issues = []
        plan_start_issues = []  # plan_ready + ai-start-work label (poller-only start)
        checked_count = 0
        assigned_to_bot_count = 0
        snapshot_rows: List[Dict[str, Any]] = []
        _START_LABELS = frozenset({"ai-start-work", "ai-execute"})

        for issue in issues:
            issue_key = issue["key"]
            fields = issue.get("fields", {})
            status_name = (fields.get("status") or {}).get("name", "")
            status = status_name.lower()
            labels = list(fields.get("labels") or [])
            label_set = set(labels)
            assignee_data = fields.get("assignee")
            assignee_display = None
            if assignee_data:
                assignee_display = (
                    assignee_data.get("displayName")
                    or assignee_data.get("name")
                    or None
                )

            # Track Jira status for all issues so we can detect real To Do re-entry
            self._last_jira_status[issue_key] = status

            matched_labels = sorted(trigger_labels & label_set)
            has_label = bool(matched_labels)
            is_assigned_to_bot = self._is_assigned_to_jira_ai_bot(issue_key, fields)
            if is_assigned_to_bot:
                assigned_to_bot_count += 1
            is_todo = self._is_todo_status(fields)
            seen = issue_key in self._seen_issues
            should_process = has_label or (
                is_assigned_to_bot and bool(settings.trigger_on_assignment)
            )
            # will_process decided after reprocess pass; provisional for new
            provisional_new = should_process and is_todo and not seen

            local = self.state_manager.get_state(issue_key)
            snapshot_rows.append(
                {
                    "key": issue_key,
                    "summary": fields.get("summary") or "",
                    "jira_status": status_name,
                    "labels": labels,
                    "assignee": assignee_display,
                    "matched_label": has_label,
                    "matched_assignee": is_assigned_to_bot,
                    "matched_labels": matched_labels,
                    "is_todo": is_todo,
                    "will_process": provisional_new,  # updated after reprocess
                    "local_status": local.status.value if local else None,
                }
            )

            checked_count += 1

            if should_process and is_todo:
                if not seen:
                    # Cold start: do NOT re-fire terminal/in-flight/plan_ready work
                    # as "new" — only true first sightings (no local state) or
                    # PENDING-like states. Terminal rework requires a real To Do
                    # transition via check_status_changes.
                    local_st = local.status if local else None
                    skip_as_new = local_st in {
                        TaskStatus.COMPLETED,
                        TaskStatus.ERROR,
                        TaskStatus.CANCELLED,
                        TaskStatus.PLANNING,
                        TaskStatus.EXECUTING,
                        TaskStatus.PLAN_READY,
                    }
                    if skip_as_new:
                        self._seen_issues.add(issue_key)
                        logger.debug(
                            f"Skip cold-start requeue for {issue_key} "
                            f"(local status={local_st.value if local_st else None})"
                        )
                    else:
                        new_issues.append(issue)
                        logger.info(f"New issue to process: {issue_key}")
                todo_issues.append(issue)

                # plan_ready is not terminal — start via label without comment poll
                if (
                    local
                    and local.status == TaskStatus.PLAN_READY
                    and (_START_LABELS & {str(x).strip().lower() for x in labels})
                ):
                    plan_start_issues.append(issue)
                    logger.info(
                        f"Plan-ready start signal for {issue_key} "
                        f"(label ai-start-work / ai-execute)"
                    )

        reprocess_issues = self.check_status_changes(todo_issues)

        # Deduplicate: prefer create over update when both would fire
        new_keys = {i["key"] for i in new_issues}
        reprocess_issues = [i for i in reprocess_issues if i["key"] not in new_keys]
        plan_start_issues = [
            i
            for i in plan_start_issues
            if i["key"] not in new_keys
            and i["key"] not in {x["key"] for x in reprocess_issues}
        ]
        reprocess_keys = {i["key"] for i in reprocess_issues} | {
            i["key"] for i in plan_start_issues
        }
        will_keys = new_keys | reprocess_keys

        for row in snapshot_rows:
            row["will_process"] = row["key"] in will_keys

        if checked_count > 0:
            logger.info(
                f"Checked {checked_count} issues from {source}, "
                f"{assigned_to_bot_count} assigned to bot, "
                f"{len(new_issues)} new to process, "
                f"{len(reprocess_issues)} to reprocess, "
                f"{len(plan_start_issues)} plan_ready starts"
            )

        poll_snapshot_store.end_poll(
            source=source,
            issues=snapshot_rows,
            interval_seconds=self.interval,
        )
        self._last_check = datetime.now()
        # plan_start goes as is_update so processor uses issue_updated path
        return new_issues + reprocess_issues + plan_start_issues

    @staticmethod
    def issue_text_fingerprint(issue: dict, *, light: Optional[bool] = None) -> str:
        """Stable hash for reprocess-on-edit detection.

        Full fingerprint: ``summary + "\\n" + description``.
        Light fingerprint (board scan omits description): ``summary + "\\n"``.

        When ``light`` is None, light mode is chosen automatically if the
        ``description`` key is absent from fields (poll_board payload shape).
        """
        fields = issue.get("fields") or {}
        summary = fields.get("summary") or ""
        if light is None:
            light = "description" not in fields
        if light:
            raw = f"{summary}\n".encode("utf-8", errors="replace")
        else:
            desc = fields.get("description") or ""
            if not isinstance(desc, str):
                desc = str(desc)
            raw = f"{summary}\n{desc}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()[:20]

    @staticmethod
    def text_fingerprints_from_state(
        summary: Optional[str], description: Optional[str]
    ) -> Dict[str, str]:
        """Full + light fingerprints for fail-path metadata (poller-compatible)."""
        s = summary or ""
        d = description or ""
        if not isinstance(d, str):
            d = str(d)
        full = hashlib.sha256(f"{s}\n{d}".encode("utf-8", errors="replace")).hexdigest()[
            :20
        ]
        light = hashlib.sha256(f"{s}\n".encode("utf-8", errors="replace")).hexdigest()[
            :20
        ]
        return {
            "last_intake_fingerprint": full,
            "last_intake_fingerprint_light": light,
        }

    def check_status_changes(self, todo_issues: List[dict]) -> List[dict]:
        """Re-queue terminal issues that should run again.

        Never re-queue in-flight work (PLANNING/EXECUTING) just because
        Jira still says To Do — that caused infinite re-execution loops.

        Reprocess when:
        * user moves ticket back to To Do (leave → return), or
        * ERROR/CANCELLED + requeue_eligible + still To Do + **description/summary
          changed** (user fixed Mode/{params} without leaving the column).
        """
        reprocess_issues = []
        terminal = {
            TaskStatus.COMPLETED,
            TaskStatus.ERROR,
            TaskStatus.CANCELLED,
        }
        in_flight = {
            TaskStatus.PLANNING,
            TaskStatus.EXECUTING,
        }

        for issue in todo_issues:
            issue_key = issue["key"]
            state = self.state_manager.get_state(issue_key)
            if not state:
                continue

            # Never interrupt active agent work via poll reprocess
            if state.status in in_flight:
                logger.debug(
                    f"Skipping reprocess for in-flight {issue_key} ({state.status.value})"
                )
                continue

            if state.status not in terminal:
                continue

            prev_before = getattr(self, "_status_before_poll", {}).get(issue_key)
            fields = issue.get("fields") or {}
            curr_name = ((fields.get("status") or {}).get("name") or "").strip().lower()
            meta = state.metadata or {}
            requeue_eligible = bool(meta.get("requeue_eligible"))
            # Local-only markers — never treat as "left To Do" or auto-requeue
            # while Jira stayed on To Do after cancel/fail.
            synthetic = frozenset({"__cancelled__", "__terminal_local__"})

            # Real board leave non-To-Do → return to To Do.
            # Require an actual status *name* change: category-"new" columns
            # (e.g. "Selected for Development") are To Do-like for eligibility
            # but are not in the hard-coded English name set. Without prev!=curr
            # they re-fired every poll for terminal work.
            entered_todo_from_elsewhere = (
                prev_before is not None
                and prev_before not in synthetic
                and prev_before != curr_name
                and not self._is_todo_status_name(prev_before)
                and self._is_todo_status(fields)
            )
            # Cancel/error: only when Jira status *string* changed into To Do
            # (user actually moved the ticket). Synthetic markers alone do NOT count.
            status_changed_into_todo = (
                requeue_eligible
                and prev_before is not None
                and prev_before not in synthetic
                and prev_before != curr_name
                and self._is_todo_status(fields)
            )
            # After process_issue moved tracker to "in progress", returning to To Do
            # is a real reopen even if requeue_eligible was cleared mid-flight.
            force_after_in_progress = (
                prev_before == "in progress"
                and self._is_todo_status(fields)
            )
            # User fixed description (Mode/{params}) while staying on To Do after ERROR.
            # CANCELLED while still To Do must NOT auto-retry (operator cancelled).
            # Board scan omits description — use light fingerprint there so a full
            # stored hash never false-matches "text changed" every poll.
            text_changed_retry = False
            if (
                requeue_eligible
                and state.status == TaskStatus.ERROR
                and self._is_todo_status(fields)
            ):
                text_changed_retry = self._error_text_changed_for_reprocess(
                    issue, meta
                )

            if not (
                entered_todo_from_elsewhere
                or status_changed_into_todo
                or force_after_in_progress
                or text_changed_retry
            ):
                # Still To Do-like since last poll (or first sighting) — do not loop
                continue

            reason = (
                "issue text changed"
                if text_changed_retry
                and not (
                    entered_todo_from_elsewhere
                    or status_changed_into_todo
                    or force_after_in_progress
                )
                else f"jira {prev_before} -> to do"
            )
            logger.info(
                f"status change {issue_key}: {reason} "
                f"(local {state.status.value}); reprocessing"
            )
            reprocess_issues.append(issue)

        return reprocess_issues

    def _error_text_changed_for_reprocess(
        self, issue: dict, meta: dict
    ) -> bool:
        """True when ERROR requeue should fire due to summary/description edit.

        Light board payloads (no ``description`` key) compare only the summary
        fingerprint so missing description never looks like a user edit.
        When description is present (enriched), use the full fingerprint.
        """
        fields = issue.get("fields") or {}
        board_is_light = "description" not in fields

        if board_is_light:
            light_fp = self.issue_text_fingerprint(issue, light=True)
            last_light = meta.get("last_intake_fingerprint_light")
            if last_light is not None:
                return last_light != light_fp
            # Legacy rows with only full fingerprint: do not false-positive.
            # Missing fingerprint entirely: allow one requeue attempt.
            if meta.get("last_intake_fingerprint") is None:
                return True
            return False

        fp = self.issue_text_fingerprint(issue, light=False)
        last_fp = meta.get("last_intake_fingerprint")
        if last_fp is None:
            return True
        return last_fp != fp

    def _enrich_issue_for_work(self, issue: dict) -> dict:
        """Fetch full issue (incl. description) only for keys we will process."""
        issue_key = issue.get("key") or ""
        fields = issue.get("fields") or {}
        if fields.get("description") not in (None, ""):
            return issue
        if not hasattr(self.client, "get_issue"):
            return issue
        try:
            full = self.client.get_issue(
                issue_key,
                fields=["summary", "description", "labels", "assignee", "status", "issuetype"],
            )
            if full and full.get("fields"):
                # Merge full fields onto the light poll payload
                merged = dict(issue)
                merged_fields = dict(fields)
                merged_fields.update(full.get("fields") or {})
                merged["fields"] = merged_fields
                return merged
        except Exception as e:
            logger.warning(f"Could not enrich {issue_key} from Jira: {e}")
        return issue

    def process_issue(self, issue: dict, is_update: bool = False) -> None:
        issue = self._enrich_issue_for_work(issue)
        issue_key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "No summary")

        logger.info(f"Processing {issue_key}: {summary}")

        # Move to In Progress first (same as before): board shows work was accepted.
        # Config errors still post a Jira comment + local ERROR for the ops UI.
        if self.client.transition_to_in_progress(issue_key):
            logger.info(f"{issue_key} transitioned to In Progress")
            # Critical: poll_board already stored "to do" before this transition.
            # Without updating, cancel → move back to To Do never looks like a
            # status *change* (prev is still "to do") and reprocess is skipped.
            self._last_jira_status[issue_key] = "in progress"

        if self._handler:
            event = {
                "webhookEvent": "jira:issue_updated" if is_update else "jira:issue_created",
                "issue": issue,
                "timestamp": int(time.time() * 1000),
            }
            self._handler(event)

    def start(self, handler: Callable[[dict], None]):
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._running = True
        self._handler = handler

        logger.info(f"Starting JIRA board poller (interval: {self.interval}s)")

        while self._running:
            try:
                # Snapshot prior statuses so check_status_changes can detect transitions
                self._status_before_poll = dict(self._last_jira_status)
                # Keep interval in sync with runtime settings / dashboard
                self.interval = int(
                    getattr(settings, "poll_interval_seconds", None) or self.interval or 30
                )
                if settings.jira_board_id:
                    self.board_id = settings.jira_board_id

                poll_snapshot_store.begin_poll(
                    board_id=self.board_id,
                    interval_seconds=self.interval,
                )
                issues = self.poll_board()
                if issues:
                    logger.info(f"Poll cycle: {len(issues)} issue(s) to process")
                    workers = max(
                        1,
                        min(
                            32,
                            int(getattr(settings, "poll_dispatch_workers", None) or 8),
                        ),
                    )
                    # Parallel dispatch: enrich + fire handler + transition
                    # (agent work itself is capped by max_concurrent_jobs)
                    def _one(issue: dict) -> str:
                        key = issue["key"]
                        is_update = key in self._seen_issues
                        self.process_issue(issue, is_update)
                        return key

                    if len(issues) == 1 or workers == 1:
                        for issue in issues:
                            key = _one(issue)
                            self._seen_issues.add(key)
                    else:
                        with ThreadPoolExecutor(max_workers=workers) as pool:
                            futures = [pool.submit(_one, issue) for issue in issues]
                            for fut in as_completed(futures):
                                try:
                                    key = fut.result()
                                    self._seen_issues.add(key)
                                except Exception as e:
                                    logger.error(f"Dispatch failed for an issue: {e}")

            except Exception as e:
                logger.error(f"Error during poll: {e}")
                poll_snapshot_store.end_poll(
                    source=self.board_id or "error",
                    issues=[],
                    interval_seconds=self.interval,
                    error=str(e),
                )

            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)

    def stop(self):
        """Stop polling loop."""
        self._running = False
        poll_snapshot_store.set_idle()
        logger.info("JIRA poller stopped")
