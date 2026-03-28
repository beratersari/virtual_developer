"""State manager for persisting JIRA agent state."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.state.models import JiraAgentState, TaskStatus


class JiraStateManager:
    """Manages JIRA agent state persistence to disk."""
    
    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = state_dir or settings.state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_state_file(self, issue_key: str) -> Path:
        """Get path to state file for an issue."""
        # Sanitize issue key for filename (replace special chars)
        safe_key = issue_key.replace("-", "_").replace("/", "_")
        return self.state_dir / f"{safe_key}.json"
    
    def get_state(self, issue_key: str) -> Optional[JiraAgentState]:
        """Load state for an issue from disk."""
        state_file = self._get_state_file(issue_key)
        if not state_file.exists():
            return None
        
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return JiraAgentState.from_dict(data)
        except Exception as e:
            print(f"[StateManager] Error loading state for {issue_key}: {e}")
            return None
    
    def set_state(self, state: JiraAgentState) -> None:
        """Save state to disk."""
        state_file = self._get_state_file(state.issue_key)
        
        try:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[StateManager] Error saving state for {state.issue_key}: {e}")
    
    def create_state(
        self,
        issue_key: str,
        issue_summary: str,
        description: str = "",
        triggered_by: Optional[str] = None,
        jira_assignee: Optional[str] = None,
    ) -> JiraAgentState:
        """Create a new state for an issue."""
        state = JiraAgentState(
            issue_key=issue_key,
            issue_summary=issue_summary,
            description=description,
            status=TaskStatus.PENDING,
            triggered_by=triggered_by,
            jira_assignee=jira_assignee,
            metadata={},
        )
        self.set_state(state)
        return state
    
    def update_state(
        self,
        issue_key: str,
        **kwargs: Any,
    ) -> Optional[JiraAgentState]:
        """Update specific fields of an existing state."""
        state = self.get_state(issue_key)
        if not state:
            print(f"[StateManager] No state found for {issue_key}")
            return None
        
        # Update fields
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
            else:
                print(f"[StateManager] Unknown field: {key}")
        
        self.set_state(state)
        return state
    
    def get_active_issues(self) -> List[JiraAgentState]:
        """Get all issues that are not in a terminal state."""
        terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
        active = []
        
        if not self.state_dir.exists():
            return active
        
        for state_file in self.state_dir.glob("*.json"):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                state = JiraAgentState.from_dict(data)
                if state.status not in terminal_states:
                    active.append(state)
            except Exception as e:
                print(f"[StateManager] Error loading {state_file}: {e}")
        
        return active
    
    def delete_state(self, issue_key: str) -> bool:
        """Delete state file for an issue."""
        state_file = self._get_state_file(issue_key)
        if state_file.exists():
            try:
                state_file.unlink()
                return True
            except Exception as e:
                print(f"[StateManager] Error deleting state for {issue_key}: {e}")
                return False
        return False
