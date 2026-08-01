"""State manager for persisting JIRA agent state."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.logger import logger
from src.state.models import JiraAgentState, TaskStatus


def _log_state_transition(issue_key: str, old_status: TaskStatus, new_status: TaskStatus) -> None:
    """Log a status change using the standard application logger format."""
    logger.info(
        f"state {issue_key}: {old_status.value} -> {new_status.value}"
    )


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
            logger.error(f"Error loading state for {issue_key}: {e}")
            return None
    
    def set_state(self, state: JiraAgentState) -> None:
        """Save state to disk. Logs state transitions to terminal."""
        state_file = self._get_state_file(state.issue_key)

        # Detect state transitions by comparing with current persisted state
        try:
            if state_file.exists():
                with open(state_file, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                old_status = TaskStatus(old_data.get("status", "pending"))
                if old_status != state.status:
                    _log_state_transition(state.issue_key, old_status, state.status)
        except Exception:
            pass  # If we can't read old state, skip logging
        
        try:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error saving state for {state.issue_key}: {e}")
    
    def create_state(
        self,
        issue_key: str,
        issue_summary: str,
        description: str = "",
        triggered_by: Optional[str] = None,
        jira_assignee: Optional[str] = None,
    ) -> JiraAgentState:
        """Create a new state for an issue."""
        logger.info(f"state {issue_key}: created -> {TaskStatus.PENDING.value}")

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
            logger.warning(f"No state found for {issue_key}")
            return None
        
        # Update fields (merge metadata instead of replacing the whole dict)
        for key, value in kwargs.items():
            if not hasattr(state, key):
                logger.warning(f"Unknown field: {key}")
                continue
            if key == "metadata" and isinstance(value, dict):
                state.metadata = {**(state.metadata or {}), **value}
            else:
                setattr(state, key, value)
        
        self.set_state(state)
        return state
    
    def get_all_states(self) -> List[JiraAgentState]:
        """Load every persisted issue state (all statuses)."""
        states: List[JiraAgentState] = []
        if not self.state_dir.exists():
            return states
        for state_file in self.state_dir.glob("*.json"):
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                states.append(JiraAgentState.from_dict(data))
            except Exception as e:
                logger.error(f"Error loading {state_file}: {e}")
        return states

    def get_active_issues(self) -> List[JiraAgentState]:
        """Get all issues that are not in a terminal state."""
        terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
        return [s for s in self.get_all_states() if s.status not in terminal_states]
    
    def delete_state(self, issue_key: str) -> bool:
        """Delete state file for an issue."""
        state_file = self._get_state_file(issue_key)
        if state_file.exists():
            try:
                state_file.unlink()
                return True
            except Exception as e:
                logger.error(f"Error deleting state for {issue_key}: {e}")
                return False
        return False
