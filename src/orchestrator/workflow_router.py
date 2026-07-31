"""Route issues to appropriate workflows."""

from enum import Enum
from typing import Any, Dict, Optional

from src.config import settings


class WorkflowType(Enum):
    """Types of workflows available."""
    PLANNING = "planning"           # Prometheus → Atlas → Done
    DIRECT_EXECUTION = "direct"     # Sisyphus direct
    COMMENT_RESPONSE = "comment"    # Respond to @mention
    ORACLE_CONSULT = "oracle"       # Architecture question


class WorkflowRouter:
    """Routes JIRA issues to appropriate workflows."""
    
    # Keywords that indicate planning is needed
    PLANNING_KEYWORDS = [
        "epic", "feature", "implement", "create", "build",
        "design", "architecture", "refactor", "migrate",
    ]
    
    # Keywords for direct execution
    DIRECT_KEYWORDS = [
        "fix", "bug", "typo", "update", "change",
        "add", "remove", "delete", "rename",
    ]
    
    # Keywords indicating Oracle consultation
    ORACLE_KEYWORDS = [
        "should we", "architecture", "design pattern",
        "best practice", "how to", "approach",
    ]
    
    @classmethod
    def route_issue(
        cls,
        issue_key: str,
        summary: str,
        description: str,
        comment: Optional[str] = None,
    ) -> WorkflowType:
        """Determine workflow type for an issue."""
        
        # If it's a comment with @mention, handle as comment response
        if comment:
            return WorkflowType.COMMENT_RESPONSE
        
        combined_text = f"{summary} {description}".lower()
        
        # Check for Oracle consultation indicators
        if any(kw in combined_text for kw in cls.ORACLE_KEYWORDS):
            return WorkflowType.ORACLE_CONSULT
        
        # Check complexity to decide planning vs direct
        complexity_score = cls._calculate_complexity(summary, description)
        
        if complexity_score >= 3:
            return WorkflowType.PLANNING
        else:
            return WorkflowType.DIRECT_EXECUTION
    
    @classmethod
    def _calculate_complexity(cls, summary: str, description: str) -> int:
        """Calculate complexity score (0-5)."""
        score = 0
        text = f"{summary} {description}".lower()
        
        # Score based on planning keywords
        for kw in cls.PLANNING_KEYWORDS:
            if kw in text:
                score += 1
        
        # Score based on description length
        if len(description) > 500:
            score += 1
        if len(description) > 1000:
            score += 1
        
        # Score based on file references
        if any(ext in text for ext in [".ts", ".js", ".py", ".java", ".go"]):
            score += 1
        
        return min(score, 5)
    
    @classmethod
    def should_auto_start(cls, workflow_type: WorkflowType) -> bool:
        """Check if workflow should auto-start without human confirmation."""
        if workflow_type == WorkflowType.DIRECT_EXECUTION:
            return True
        if workflow_type == WorkflowType.COMMENT_RESPONSE:
            return True
        # Planning and Oracle typically need human confirmation
        return settings.auto_start_plans
    
    @classmethod
    def get_agent_for_workflow(cls, workflow_type: WorkflowType) -> str:
        """Get default agent for workflow type."""
        mapping = {
            WorkflowType.PLANNING: settings.planning_agent,
            WorkflowType.DIRECT_EXECUTION: settings.default_agent,
            WorkflowType.COMMENT_RESPONSE: settings.default_agent,
            WorkflowType.ORACLE_CONSULT: "oracle",
        }
        return mapping.get(workflow_type, settings.default_agent)
    
    @classmethod
    def extract_mention_command(cls, comment_text: str) -> Optional[str]:
        """Extract command from @bot mention."""
        text_lower = comment_text.lower()
        
        for mention in settings.trigger_mentions_list:
            mention_lower = mention.lower()
            if mention_lower in text_lower:
                # Get text after mention
                idx = text_lower.index(mention_lower)
                after_mention = comment_text[idx + len(mention):].strip()
                return after_mention
        
        return None
