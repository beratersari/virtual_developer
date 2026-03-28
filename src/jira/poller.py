"""Polling-based JIRA issue discovery."""

import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Set

from src.config import settings
from src.jira.client import JiraClient


class JiraPoller:
    """Poll JIRA for new issues matching criteria."""
    
    def __init__(
        self,
        client: Optional[JiraClient] = None,
        interval_seconds: Optional[int] = None,
    ):
        self.client = client or JiraClient()
        self.interval = interval_seconds or settings.poll_interval_seconds
        self._last_check: Optional[datetime] = None
        self._seen_issues: Set[str] = set()
        self._running = False
        self._handler: Optional[Callable[[dict], None]] = None
    
    def build_jql(self) -> str:
        """Build JQL query for issues to process."""
        projects = ", ".join(f'"{p}"' for p in settings.jira_projects_list)
        labels = ", ".join(f'"{l}"' for l in settings.trigger_labels_list)
        
        conditions = [f"project in ({projects})"]
        
        # Add label condition
        if settings.trigger_labels_list:
            conditions.append(f"labels in ({labels})")
        
        # Add assignee condition
        if settings.trigger_on_assignment:
            conditions.append("assignee is not EMPTY")
        
        # Only issues created/updated since last check
        if self._last_check:
            jira_time = self._last_check.strftime("%Y-%m-%d %H:%M")
            conditions.append(f'created >= "{jira_time}" OR updated >= "{jira_time}"')
        
        return " AND ".join(conditions)
    
    def poll(self) -> List[dict]:
        """Poll for new issues."""
        jql = self.build_jql()
        print(f"Polling with JQL: {jql}")
        
        issues = self.client.search_issues(
            jql=jql,
            fields=["key", "summary", "description", "labels", "assignee", "status", "created", "updated"],
        )
        
        # Filter out already seen issues
        new_issues = []
        for issue in issues:
            issue_key = issue["key"]
            if issue_key not in self._seen_issues:
                self._seen_issues.add(issue_key)
                new_issues.append(issue)
        
        self._last_check = datetime.now()
        return new_issues
    
    def start(self, handler: Callable[[dict], None]):
        """Start polling loop."""
        self._running = True
        self._handler = handler
        
        print(f"Starting JIRA poller (interval: {self.interval}s)")
        
        while self._running:
            try:
                issues = self.poll()
                for issue in issues:
                    # Convert to webhook-like event format
                    event = {
                        "webhookEvent": "jira:issue_created",
                        "issue": issue,
                        "timestamp": int(time.time() * 1000),
                    }
                    handler(event)
                
            except Exception as e:
                print(f"Error during poll: {e}")
            
            # Sleep with interrupt check
            for _ in range(self.interval):
                if not self._running:
                    break
                time.sleep(1)
    
    def stop(self):
        """Stop polling loop."""
        self._running = False
        print("JIRA poller stopped")
