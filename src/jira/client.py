"""JIRA API client wrapper."""

from typing import Any, Dict, List, Optional, Union

import httpx

from src.config import settings
from src.logger import logger


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
        
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        auth = None
        headers = {"Content-Type": "application/json"}
        
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        elif self.username and self.api_token:
            auth = (self.username, self.api_token)
        
        self.client = httpx.Client(
            base_url=f"{self.host}/rest/api/2",
            auth=auth,
            headers=headers,
            timeout=30.0,
            verify=False
        )
    
    def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new issue in JIRA."""
        payload = {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": {"name": issue_type},
        }
        
        if assignee:
            payload["assignee"] = {"name": assignee}
        
        if labels:
            payload["labels"] = labels
        
        try:
            response = self.client.post("/issue", json=payload)
            logger.info(f"Create issue status: {response.status_code}")
            if response.status_code != 201:
                logger.warning(f"Create issue response: {response.text[:500]}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error creating issue: {e}")
            return None
    
    def get_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Get issue details by key."""
        try:
            response = self.client.get(f"/issue/{issue_key}")
            if response.status_code != 200:
                logger.warning(f"Get issue {issue_key}: {response.status_code}")
                logger.debug(f"Get issue response: {response.text}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching issue {issue_key}: {e}")
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
            logger.error(f"Error searching issues: {e}")
            return []
    
    def get_board_issues(
        self,
        board_id: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> List[Dict[str, Any]]:
        """Get all issues from a Jira board."""
        params = {
            "maxResults": max_results,
            "startAt": start_at,
        }
        if fields:
            params["fields"] = ",".join(fields)
        
        try:
            # Agile API is at /rest/agile/1.0, not /rest/api/2
            url = f"{self.host}/rest/agile/1.0/board/{board_id}/issue"
            response = self.client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get("issues", [])
        except httpx.HTTPError as e:
            logger.error(f"Error getting board issues: {e}")
            return []
    
    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        """Get the active sprint for a board."""
        try:
            # Agile API is at /rest/agile/1.0, not /rest/api/2
            # Use absolute URL to avoid base_url conflict
            url = f"{self.host}/rest/agile/1.0/board/{board_id}/sprint"
            response = self.client.get(url, params={"state": "active"})
            response.raise_for_status()
            data = response.json()
            values = data.get("values", [])
            if values:
                return values[0]
            return None
        except httpx.HTTPError as e:
            logger.error(f"Error getting active sprint: {e}")
            return None
    
    def get_sprint_issues(
        self,
        sprint_id: int,
        fields: Optional[List[str]] = None,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get all issues in a sprint with pagination."""
        all_issues = []
        start_at = 0
        
        while True:
            params = {
                "startAt": start_at,
                "maxResults": max_results,
            }
            if fields:
                params["fields"] = ",".join(fields)
            
            try:
                # Agile API is at /rest/agile/1.0, not /rest/api/2
                url = f"{self.host}/rest/agile/1.0/sprint/{sprint_id}/issue"
                response = self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                issues = data.get("issues", [])
                all_issues.extend(issues)
                
                # Check if there are more issues
                total = data.get("total", 0)
                if start_at + max_results >= total:
                    break
                start_at += max_results
                
            except httpx.HTTPError as e:
                logger.error(f"Error getting sprint issues: {e}")
                break
        
        return all_issues
    
    def get_transitions(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get available transitions for an issue."""
        try:
            response = self.client.get(f"/issue/{issue_key}/transitions")
            response.raise_for_status()
            data = response.json()
            return data.get("transitions", [])
        except httpx.HTTPError as e:
            logger.error(f"Error getting transitions for {issue_key}: {e}")
            return []
    
    def do_transition(self, issue_key: str, transition_id: str) -> bool:
        """Transition an issue to a new status."""
        try:
            response = self.client.post(
                f"/issue/{issue_key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error transitioning {issue_key}: {e}")
            return False
    
    def transition_to_in_progress(self, issue_key: str) -> bool:
        """Transition an issue to 'In Progress' status."""
        transitions = self.get_transitions(issue_key)
        for t in transitions:
            if "in progress" in t["name"].lower():
                return self.do_transition(issue_key, t["id"])
        logger.warning(f"No 'In Progress' transition found for {issue_key}")
        return False
    
    def add_comment(self, issue_key: str, body: str) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue."""
        try:
            logger.info(f"Adding comment to {issue_key}")
            response = self.client.post(
                f"/issue/{issue_key}/comment",
                json={"body": body},
            )
            logger.debug(f"Comment status: {response.status_code}")
            if response.status_code == 400:
                alt_body = {"body": {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]}}
                logger.warning(f"Trying alternate comment format for {issue_key}")
                response = self.client.post(
                    f"/issue/{issue_key}/comment",
                    json=alt_body,
                )
                logger.debug(f"Alternate comment status: {response.status_code}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error adding comment to {issue_key}: {e}")
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
            logger.error(f"Error updating issue {issue_key}: {e}")
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
                logger.warning(f"Transition '{transition_name}' not found for {issue_key}")
                return False
            
            # Perform transition
            response = self.client.post(
                f"/issue/{issue_key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error transitioning issue {issue_key}: {e}")
            return False
    
    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get all comments for an issue."""
        try:
            response = self.client.get(f"/issue/{issue_key}/comment")
            response.raise_for_status()
            return response.json().get("comments", [])
        except httpx.HTTPError as e:
            logger.error(f"Error fetching comments for {issue_key}: {e}")
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
            logger.error(f"File not found: {file_path}")
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
            logger.error(f"Error attaching file to {issue_key}: {e}")
            return None
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


def create_jira_client(simulated: bool = False):
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
