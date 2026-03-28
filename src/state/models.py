"""State models for JIRA agent tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class TaskStatus(Enum):
    """Status of a JIRA agent task."""
    PENDING = "pending"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class JiraAgentState:
    """Represents the state of a JIRA issue being processed by the agent."""
    
    issue_key: str
    issue_summary: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress_percentage: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # Plan and execution
    plan_path: Optional[str] = None
    
    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    execution_duration_seconds: Optional[float] = None
    
    # Agent tracking
    current_task_id: Optional[str] = None
    current_session_id: Optional[str] = None
    
    # Error handling
    error_message: Optional[str] = None
    retry_count: int = 0
    
    # Cost tracking
    token_usage_input: int = 0
    token_usage_output: int = 0
    estimated_cost: float = 0.0
    
    # JIRA info
    jira_assignee: Optional[str] = None
    
    # Trigger info
    triggered_by: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert state to dictionary for JSON serialization."""
        return {
            "issue_key": self.issue_key,
            "issue_summary": self.issue_summary,
            "description": self.description,
            "status": self.status.value,
            "progress_percentage": self.progress_percentage,
            "metadata": self.metadata,
            "plan_path": self.plan_path,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "execution_duration_seconds": self.execution_duration_seconds,
            "current_task_id": self.current_task_id,
            "current_session_id": self.current_session_id,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "token_usage_input": self.token_usage_input,
            "token_usage_output": self.token_usage_output,
            "estimated_cost": self.estimated_cost,
            "jira_assignee": self.jira_assignee,
            "triggered_by": self.triggered_by,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JiraAgentState":
        """Create state from dictionary (JSON deserialization)."""
        # Parse datetime strings
        started_at = None
        if data.get("started_at"):
            started_at = datetime.fromisoformat(data["started_at"])
        
        completed_at = None
        if data.get("completed_at"):
            completed_at = datetime.fromisoformat(data["completed_at"])
        
        # Parse status
        status = TaskStatus(data.get("status", "pending"))
        
        return cls(
            issue_key=data["issue_key"],
            issue_summary=data.get("issue_summary", ""),
            description=data.get("description", ""),
            status=status,
            progress_percentage=data.get("progress_percentage", 0),
            metadata=data.get("metadata", {}),
            plan_path=data.get("plan_path"),
            started_at=started_at,
            completed_at=completed_at,
            execution_duration_seconds=data.get("execution_duration_seconds"),
            current_task_id=data.get("current_task_id"),
            current_session_id=data.get("current_session_id"),
            error_message=data.get("error_message"),
            retry_count=data.get("retry_count", 0),
            token_usage_input=data.get("token_usage_input", 0),
            token_usage_output=data.get("token_usage_output", 0),
            estimated_cost=data.get("estimated_cost", 0.0),
            jira_assignee=data.get("jira_assignee"),
            triggered_by=data.get("triggered_by"),
        )
