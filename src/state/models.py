"""State models for JIRA agent tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TaskStatus(Enum):
    """Status of a JIRA agent task."""
    PENDING = "pending"
    PLANNING = "planning"
    PLAN_READY = "plan_ready"
    EXECUTING = "executing"
    CODE_REVIEW = "code_review"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class RetryAttempt:
    """Represents a single retry attempt."""
    attempt_number: int
    timestamp: datetime
    reason: str
    delay_seconds: float
    session_log_path: Optional[str] = None
    error_message: Optional[str] = None
    return_code: Optional[int] = None
    opencode_session_id: Optional[str] = None  # opencode session ID from CLI output

    def to_dict(self) -> Dict[str, Any]:
        """Convert retry attempt to dictionary."""
        return {
            "attempt_number": self.attempt_number,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "reason": self.reason,
            "delay_seconds": self.delay_seconds,
            "session_log_path": self.session_log_path,
            "error_message": self.error_message,
            "return_code": self.return_code,
            "opencode_session_id": self.opencode_session_id,  # opencode session ID
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetryAttempt":
        """Create retry attempt from dictionary."""
        timestamp = None
        if data.get("timestamp"):
            timestamp = datetime.fromisoformat(data["timestamp"])

        return cls(
            attempt_number=data.get("attempt_number", 0),
            timestamp=timestamp,
            reason=data.get("reason", ""),
            delay_seconds=data.get("delay_seconds", 0.0),
            session_log_path=data.get("session_log_path"),
            error_message=data.get("error_message"),
            return_code=data.get("return_code"),
            opencode_session_id=data.get("opencode_session_id"),  # opencode session ID
        )


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
    current_opencode_session_id: Optional[str] = None  # opencode session ID from CLI output
    
    # Error handling
    error_message: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 0
    last_retry_at: Optional[datetime] = None

    # Timeout tracking
    timed_out: bool = False
    timeout_seconds: Optional[int] = None

    # Retry history - array of all retry attempts (each has its own reason)
    retry_history: List[RetryAttempt] = field(default_factory=list)

    # Cost tracking
    token_usage_input: int = 0
    token_usage_output: int = 0
    estimated_cost: float = 0.0

    # JIRA info
    jira_assignee: Optional[str] = None

    # Code review
    code_review_result: Optional[str] = None
    code_review_model: Optional[str] = None

    # Trigger info
    triggered_by: Optional[str] = None

    def add_retry_attempt(self, attempt: RetryAttempt) -> None:
        """Add a retry attempt to the history."""
        self.retry_history.append(attempt)
        self.retry_count = len(self.retry_history)
        self.last_retry_at = attempt.timestamp
    
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
            "current_opencode_session_id": self.current_opencode_session_id,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "last_retry_at": self.last_retry_at.isoformat() if self.last_retry_at else None,
            "timed_out": self.timed_out,
            "timeout_seconds": self.timeout_seconds,
            "retry_history": [attempt.to_dict() for attempt in self.retry_history],
            "token_usage_input": self.token_usage_input,
            "token_usage_output": self.token_usage_output,
            "estimated_cost": self.estimated_cost,
            "jira_assignee": self.jira_assignee,
            "code_review_result": self.code_review_result,
            "code_review_model": self.code_review_model,
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

        last_retry_at = None
        if data.get("last_retry_at"):
            last_retry_at = datetime.fromisoformat(data["last_retry_at"])

        # Parse retry history
        retry_history = []
        if data.get("retry_history"):
            retry_history = [
                RetryAttempt.from_dict(attempt_data)
                for attempt_data in data["retry_history"]
            ]

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
            current_opencode_session_id=data.get("current_opencode_session_id"),
            error_message=data.get("error_message"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 0),
            last_retry_at=last_retry_at,
            timed_out=data.get("timed_out", False),
            timeout_seconds=data.get("timeout_seconds"),
            retry_history=retry_history,
            token_usage_input=data.get("token_usage_input", 0),
            token_usage_output=data.get("token_usage_output", 0),
            estimated_cost=data.get("estimated_cost", 0.0),
            jira_assignee=data.get("jira_assignee"),
            code_review_result=data.get("code_review_result"),
            code_review_model=data.get("code_review_model"),
            triggered_by=data.get("triggered_by"),
        )
