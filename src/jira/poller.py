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
    
    def poll_board(self) -> List[dict]:
        if not self.board_id:
            logger.debug("No board_id configured, skipping poll")
            return []

        logger.debug(f"Polling board {self.board_id} for active sprint")
        sprint = self.client.get_active_sprint(self.board_id)
        if not sprint:
            logger.debug("No active sprint found")
            return []

        sprint_id = sprint["id"]
        sprint_name = sprint.get('name', 'unknown')
        logger.info(f"Found active sprint: {sprint_name} (id: {sprint_id})")
        
        issues = self.client.get_sprint_issues(
            sprint_id,
            fields=["key", "summary", "description", "labels", "assignee", "status", "issuetype"],
            max_results=100,
        )
        
        if not issues:
            logger.debug("No issues found in sprint")
            return []
        
        logger.debug(f"Found {len(issues)} issues in sprint")
        
        trigger_labels = set(settings.trigger_labels_list)
        logger.debug(f"Trigger labels: {trigger_labels}")
        
        new_issues = []
        todo_issues = []
        checked_count = 0
        assigned_to_bot_count = 0
        
        for issue in issues:
            issue_key = issue["key"]
            fields = issue.get("fields", {})
            status = fields.get("status", {}).get("name", "").lower()
            labels = set(fields.get("labels", []))
            
            has_label = bool(trigger_labels & labels)
            is_assigned_to_bot = self._is_assigned_to_jira_ai_bot(issue_key)
            if is_assigned_to_bot:
                assigned_to_bot_count += 1
            is_todo = status == "to do"
            seen = issue_key in self._seen_issues
            should_process = has_label or is_assigned_to_bot
            
            checked_count += 1
            
            # logger.debug(f"Checking {issue_key}: status={status}, labels={labels}, "
            #            f"has_trigger_label={has_label}, is_assigned_to_bot={is_assigned_to_bot}, "
            #            f"is_todo={is_todo}, seen={seen}")
            
            if should_process and is_todo:
                if not seen:
                    self._seen_issues.add(issue_key)
                    new_issues.append(issue)
                    logger.info(f"New issue to process: {issue_key}")
                todo_issues.append(issue)
        
        reprocess_issues = self.check_status_changes(todo_issues)

        if checked_count > 0:
            logger.info(f"Checked {checked_count} issues in sprint, "
                       f"{assigned_to_bot_count} assigned to bot, "
                       f"{len(new_issues)} new to process, "
                       f"{len(reprocess_issues)} to reprocess")
        
        self._last_check = datetime.now()
        return new_issues + reprocess_issues
    
    def check_status_changes(self, todo_issues: List[dict]) -> List[dict]:
        reprocess_issues = []
        for issue in todo_issues:
            issue_key = issue["key"]
            state = self.state_manager.get_state(issue_key)
            if not state:
                continue
            
            if state.status in {TaskStatus.EXECUTING, TaskStatus.PLANNING, TaskStatus.CODE_REVIEW, TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}:
                logger.info(f"Status Change] {issue_key}: {state.status.value} -> TO DO (reprocessing)")
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
                issues = self.poll_board()
                if issues:
                    logger.info(f"=== Found {len(issues)} issue(s) to process ===")
                    for issue in issues:
                        is_update = issue["key"] in self._seen_issues
                        self.process_issue(issue, is_update)
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
