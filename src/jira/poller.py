"""Polling-based JIRA issue discovery."""

import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from src.config import settings
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
    ):
        self.client = client or JiraClient()
        self.interval = interval_seconds or settings.poll_interval_seconds
        self.board_id = board_id or settings.jira_board_id
        self.state_manager = JiraStateManager()
        
        logger.info(f"Initializing JiraPoller - interval: {self.interval}s, board_id: {self.board_id or 'not configured'}")
        
        if not self.board_id:
            logger.warning("JIRA_BOARD_ID not configured, board polling disabled")
        
        self._last_check: Optional[datetime] = None
        self._seen_issues: Set[str] = set()
        # Last observed Jira status name (lowercased) per issue — used to detect
        # real transitions into "To Do" rather than re-queueing every poll.
        self._last_jira_status: Dict[str, str] = {}
        self._running = False
        self._handler: Optional[Callable[[dict], None]] = None

    def _is_assigned_to_jira_ai_bot(self, issue_key: str) -> bool:
        #logger.debug(f"Checking if {issue_key} is assigned to JIRA AI Bot")
        try:
            issue = self.client.get_issue(issue_key)
            if not issue:
                logger.debug(f"{issue_key} not found, skipping assignee check")
                return False
            assignee = issue.get("fields", {}).get("assignee")
            if not assignee:
                #logger.debug(f"{issue_key} has no assignee")
                return False
            assignee_name = assignee.get("displayName", "").lower()
            is_assigned = "jira ai bot" in assignee_name or "jira-ai-bot" in assignee_name or "jiraai" in assignee_name
            #logger.debug(f"{issue_key} assignee: {assignee_name}, is_assigned_to_bot: {is_assigned}")
            return is_assigned
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
            return []

        issue_fields = [
            "key",
            "summary",
            "description",
            "labels",
            "assignee",
            "status",
            "issuetype",
        ]

        # Prefer active sprint (Scrum); fall back to board issues (Kanban / simple boards)
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
            return []
        
        logger.debug(f"Found {len(issues)} issues from {source}")
        
        trigger_labels = set(settings.trigger_labels_list)
        logger.debug(f"Trigger labels: {trigger_labels}")
        
        new_issues = []
        todo_issues = []
        checked_count = 0
        assigned_to_bot_count = 0
        
        for issue in issues:
            issue_key = issue["key"]
            fields = issue.get("fields", {})
            status_name = (fields.get("status") or {}).get("name", "")
            status = status_name.lower()
            labels = set(fields.get("labels") or [])

            # Track Jira status for all issues so we can detect real To Do re-entry
            self._last_jira_status[issue_key] = status
            
            has_label = bool(trigger_labels & labels)
            is_assigned_to_bot = self._is_assigned_to_jira_ai_bot(issue_key)
            if is_assigned_to_bot:
                assigned_to_bot_count += 1
            is_todo = self._is_todo_status(fields)
            seen = issue_key in self._seen_issues
            should_process = has_label or is_assigned_to_bot
            
            checked_count += 1
            
            if should_process and is_todo:
                if not seen:
                    # Mark after deciding "new" so first event is create, not update
                    new_issues.append(issue)
                    logger.info(f"New issue to process: {issue_key}")
                todo_issues.append(issue)
        
        reprocess_issues = self.check_status_changes(todo_issues)

        # Deduplicate: prefer create over update when both would fire
        new_keys = {i["key"] for i in new_issues}
        reprocess_issues = [i for i in reprocess_issues if i["key"] not in new_keys]

        if checked_count > 0:
            logger.info(
                f"Checked {checked_count} issues from {source}, "
                f"{assigned_to_bot_count} assigned to bot, "
                f"{len(new_issues)} new to process, "
                f"{len(reprocess_issues)} to reprocess"
            )
        
        self._last_check = datetime.now()
        return new_issues + reprocess_issues
    
    def check_status_changes(self, todo_issues: List[dict]) -> List[dict]:
        """Re-queue only when an issue *transitions back* into To Do after leaving it.

        Never re-queue in-flight work (PLANNING/EXECUTING) just because
        Jira still says To Do — that caused infinite re-execution loops.
        Terminal states (COMPLETED/ERROR/CANCELLED) are reprocessed only on a real
        status transition into To Do (user reopened the ticket).
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

            # Only reprocess terminal work when we observed a real transition into To Do
            # (previous Jira status was set and was not To Do).
            previous = self._last_jira_status.get(issue_key)
            # previous was already overwritten to "to do" in poll_board — use transition
            # marker stored before overwrite via a dedicated key.
            # We store pre-update in check via metadata on a side map.
            # Use _previous_before_poll if available.
            prev_before = getattr(self, "_status_before_poll", {}).get(issue_key)
            # Treat all To Do-like names the same (open, backlog, Turkish, …)
            if prev_before is None or self._is_todo_status_name(prev_before):
                # Still To Do-like since last poll (or first sighting) — do not loop
                continue

            logger.info(
                f"[Status Change] {issue_key}: Jira {prev_before} -> to do "
                f"(local {state.status.value}); reprocessing"
            )
            reprocess_issues.append(issue)

        return reprocess_issues
    
    def process_issue(self, issue: dict, is_update: bool = False) -> None:
        issue_key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary", "No summary")

        logger.info(f"Processing {issue_key}: {summary}")
        
        if self._handler:
            event = {
                "webhookEvent": "jira:issue_updated" if is_update else "jira:issue_created",
                "issue": issue,
                "timestamp": int(time.time() * 1000),
            }
            self._handler(event)
        
        if self.client.transition_to_in_progress(issue_key):
            logger.info(f"{issue_key} transitioned to In Progress")
    
    def start(self, handler: Callable[[dict], None]):
        self._running = True
        self._handler = handler

        logger.info(f"Starting JIRA board poller (interval: {self.interval}s)")
        
        while self._running:
            try:
                # Snapshot prior statuses so check_status_changes can detect transitions
                self._status_before_poll = dict(self._last_jira_status)
                issues = self.poll_board()
                if issues:
                    logger.info(f"=== Found {len(issues)} issue(s) to process ===")
                    for issue in issues:
                        issue_key = issue["key"]
                        # is_update only if we already processed this key in a prior cycle
                        is_update = issue_key in self._seen_issues
                        self.process_issue(issue, is_update)
                        self._seen_issues.add(issue_key)
                    logger.info("========================")

            except Exception as e:
                logger.error(f"Error during poll: {e}")
            
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)
    
    def stop(self):
        """Stop polling loop."""
        self._running = False
        logger.info("JIRA poller stopped")
