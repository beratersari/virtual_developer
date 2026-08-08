"""Simulated JIRA client for testing without real JIRA."""

from typing import Any, Dict, List, Optional

import httpx


class SimulatedJiraClient:
    """Client for the simulated JIRA server (in-memory REST only)."""

    def __init__(self, base_url: str = "http://localhost:7001"):
        self.base_url = base_url
        self.last_error: Optional[str] = None
        self.client = httpx.Client(base_url=base_url, timeout=30.0, verify=False)

    def create_issue(
        self,
        summary: str = "",
        description: str = "",
        issue_type: str = "Task",
        priority: str = "Medium",
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
        key: Optional[str] = None,
        project: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Create a new issue in the simulated JIRA.

        Accepts the same keyword shape as ``JiraClient.create_issue``
        (``project=``, ``summary=``, …) plus the legacy positional form.
        """
        _ = project  # simulated server is single-project
        payload = {
            "summary": summary or kwargs.get("summary") or "",
            "description": description or kwargs.get("description") or "",
            "issue_type": issue_type,
            "priority": priority,
            "assignee": assignee,
            "labels": labels or [],
        }
        if key:
            payload["key"] = key

        response = self.client.post(
            "/api/issues",
            json=payload,
        )
        if response.status_code == 201:
            return response.json()["issue"]
        self.last_error = response.text[:300] if response.text else str(response.status_code)
        return None

    def get_issue(self, issue_key: str, fields: Optional[List[str]] = None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """Get issue details (``fields`` ignored — full stub payload)."""
        _ = fields
        response = self.client.get(f"/api/issues/{issue_key}")
        if response.status_code == 200:
            return response.json()
        return None

    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        _ = board_id
        return None

    def get_board_issues(
        self,
        board_id: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> List[Dict[str, Any]]:
        _ = board_id, fields, max_results, start_at
        return self.list_issues()

    def get_sprint_issues(
        self,
        sprint_id: int,
        fields: Optional[List[str]] = None,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        _ = sprint_id, fields, max_results
        return []

    def transition_to_in_progress(self, issue_key: str) -> bool:
        _ = issue_key
        return True

    def add_labels(self, issue_key: str, labels: List[str]) -> bool:
        return self.update_issue(issue_key, labels=labels)

    def append_to_description(self, issue_key: str, suffix: str) -> bool:
        issue = self.get_issue(issue_key)
        old = ""
        if issue:
            fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else issue
            raw = (fields or {}).get("description") if isinstance(fields, dict) else ""
            old = raw if isinstance(raw, str) else ""
        new_desc = f"{old}\n\n{suffix}" if old else suffix
        return self.update_issue(issue_key, fields={"description": new_desc})

    def update_issue(self, issue_key: str, fields=None, labels=None) -> bool:
        payload: Dict[str, Any] = {}
        if fields:
            payload.update(fields if isinstance(fields, dict) else {})
        if labels is not None:
            payload["labels"] = labels
        if not payload:
            return True
        try:
            response = self.client.put(f"/api/issues/{issue_key}", json=payload)
            return response.status_code < 400
        except Exception:
            return False

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
            json={"body": body},
        )
        if response.status_code == 201:
            return response.json()["comment"]
        return None

    def assign_issue(self, key: str, assignee: str) -> Optional[Dict[str, Any]]:
        """Assign an issue."""
        response = self.client.post(
            f"/api/issues/{key}/assign",
            json={"assignee": assignee},
        )
        if response.status_code == 200:
            return response.json()["issue"]
        return None

    def close(self):
        """Close the HTTP client."""
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
