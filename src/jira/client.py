"""JIRA API client wrapper."""

from typing import Any, Dict, List, Optional, Union

import httpx

from src.config import settings


class JiraClient:
    """Client for JIRA REST API."""
    
    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        api_token: Optional[str] = None,
    ):
        self.host = (host or settings.jira_host).rstrip("/")
        self.username = username or settings.jira_username
        self.api_token = api_token or settings.jira_api_token
        
        self.client = httpx.Client(
            base_url=f"{self.host}/rest/api/2",
            auth=(self.username, self.api_token),
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
    
    def get_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Get issue details by key."""
        try:
            response = self.client.get(f"/issue/{issue_key}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            print(f"Error fetching issue {issue_key}: {e}")
            return None
    
    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search issues using JQL."""
        params = {
            "jql": jql,
            "maxResults": max_results,
        }
        if fields:
            params["fields"] = ",".join(fields)
        
        try:
            response = self.client.get("/search", params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("issues", [])
        except httpx.HTTPError as e:
            print(f"Error searching issues: {e}")
            return []
    
    def add_comment(self, issue_key: str, body: str) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue."""
        try:
            response = self.client.post(
                f"/issue/{issue_key}/comment",
                json={"body": body},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            print(f"Error adding comment to {issue_key}: {e}")
            return None
    
    def update_issue(
        self,
        issue_key: str,
        fields: Optional[Dict[str, Any]] = None,
        labels: Optional[List[str]] = None,
    ) -> bool:
        """Update issue fields."""
        payload: Dict[str, Any] = {}
        
        if fields:
            payload["fields"] = fields
        if labels is not None:
            payload["fields"] = payload.get("fields", {})
            payload["fields"]["labels"] = labels
        
        if not payload:
            return True
        
        try:
            response = self.client.put(f"/issue/{issue_key}", json=payload)
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            print(f"Error updating issue {issue_key}: {e}")
            return False
    
    def transition_issue(
        self,
        issue_key: str,
        transition_name: str,
    ) -> bool:
        """Transition issue to a new status."""
        try:
            # First get available transitions
            response = self.client.get(f"/issue/{issue_key}/transitions")
            response.raise_for_status()
            transitions = response.json().get("transitions", [])
            
            # Find transition by name
            transition_id = None
            for t in transitions:
                if t["name"].lower() == transition_name.lower():
                    transition_id = t["id"]
                    break
            
            if not transition_id:
                print(f"Transition '{transition_name}' not found for {issue_key}")
                return False
            
            # Perform transition
            response = self.client.post(
                f"/issue/{issue_key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            print(f"Error transitioning issue {issue_key}: {e}")
            return False
    
    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get all comments for an issue."""
        try:
            response = self.client.get(f"/issue/{issue_key}/comment")
            response.raise_for_status()
            return response.json().get("comments", [])
        except httpx.HTTPError as e:
            print(f"Error fetching comments for {issue_key}: {e}")
            return []
    
    def assign_issue(self, issue_key: str, account_id: str) -> bool:
        """Assign issue to a user."""
        return self.update_issue(
            issue_key,
            fields={"assignee": {"accountId": account_id}},
        )
    
    def add_attachment(
        self,
        issue_key: str,
        file_path: str,
        filename: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Attach a file to an issue."""
        from pathlib import Path
        
        path = Path(file_path)
        if not path.exists():
            print(f"File not found: {file_path}")
            return None
        
        try:
            with open(path, "rb") as f:
                files = {"file": (filename or path.name, f)}
                response = self.client.post(
                    f"/issue/{issue_key}/attachments",
                    files=files,
                    headers={"X-Atlassian-Token": "no-check"},
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            print(f"Error attaching file to {issue_key}: {e}")
            return None
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


def create_jira_client(simulated: bool = False) -> Union[JiraClient, "SimulatedJiraClient"]:
    """Factory function to create either real or simulated JIRA client.
    
    Args:
        simulated: If True, returns SimulatedJiraClient for local testing
        
    Returns:
        Either JiraClient (real) or SimulatedJiraClient (simulated)
    """
    if simulated or not settings.is_configured():
        from src.jira.simulated_client import SimulatedJiraClient
        return SimulatedJiraClient()
    return JiraClient()
