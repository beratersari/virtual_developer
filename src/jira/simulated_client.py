"""Simulated JIRA client for testing without real JIRA."""

import httpx
from typing import Optional, Dict, Any, List
from src.config import settings


class SimulatedJiraClient:
    """Client for the simulated JIRA server."""
    
    def __init__(self, base_url: str = "http://localhost:7001"):
        self.base_url = base_url
        self.client = httpx.Client(base_url=base_url, timeout=30.0)
    
    def create_issue(
        self,
        summary: str,
        description: str,
        issue_type: str = "Task",
        priority: str = "Medium",
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new issue in the simulated JIRA."""
        response = self.client.post(
            "/api/issues",
            json={
                "summary": summary,
                "description": description,
                "issue_type": issue_type,
                "priority": priority,
                "assignee": assignee,
                "labels": labels or [],
            }
        )
        if response.status_code == 201:
            return response.json()["issue"]
        return None
    
    def get_issue(self, key: str) -> Optional[Dict[str, Any]]:
        """Get issue details."""
        response = self.client.get(f"/api/issues/{key}")
        if response.status_code == 200:
            return response.json()
        return None
    
    def list_issues(self) -> List[Dict[str, Any]]:
        """List all issues."""
        response = self.client.get("/api/issues")
        if response.status_code == 200:
            return response.json().get("issues", [])
        return []
    
    def add_comment(self, key: str, body: str) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue."""
        response = self.client.post(
            f"/api/issues/{key}/comments",
            json={"body": body}
        )
        if response.status_code == 201:
            return response.json()["comment"]
        return None
    
    def assign_issue(self, key: str, assignee: str) -> Optional[Dict[str, Any]]:
        """Assign an issue."""
        response = self.client.post(
            f"/api/issues/{key}/assign",
            json={"assignee": assignee}
        )
        if response.status_code == 200:
            return response.json()["issue"]
        return None
    
    def notify_bot(
        self,
        summary: str,
        description: str,
        issue_key: Optional[str] = None,
        assignee: str = "DevBot",
        labels: Optional[List[str]] = None,
        event_type: str = "jira:issue_created",
    ) -> Optional[Dict[str, Any]]:
        """Notify the bot about an issue (triggers webhook)."""
        response = self.client.post(
            "/api/notify",
            json={
                "issue_key": issue_key,
                "summary": summary,
                "description": description,
                "assignee": assignee,
                "labels": labels or ["ai-assist"],
                "event_type": event_type,
            }
        )
        if response.status_code == 200:
            return response.json()
        return None
    
    def register_webhook(self, webhook_url: str) -> bool:
        """Register webhook URL."""
        response = self.client.post(
            "/api/webhook",
            json={"webhook_url": webhook_url}
        )
        return response.status_code == 200
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
