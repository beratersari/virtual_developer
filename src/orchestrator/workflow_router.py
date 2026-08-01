"""Route issues to appropriate workflows."""

from enum import Enum
from typing import Optional

from src.config import settings


class WorkflowType(Enum):
    """Types of workflows available."""

    PLANNING = "planning"  # Prometheus → Atlas → Done
    DIRECT_EXECUTION = "direct"  # Sisyphus direct
    ORACLE_CONSULT = "oracle"  # Architecture consultation


class WorkflowRouter:
    """Routes JIRA issues to appropriate workflows."""

    # Keywords that indicate planning is needed
    PLANNING_KEYWORDS = [
        "epic",
        "feature",
        "implement",
        "create",
        "build",
        "design",
        "architecture",
        "refactor",
        "migrate",
    ]

    # Keywords for direct execution
    DIRECT_KEYWORDS = [
        "fix",
        "bug",
        "typo",
        "update",
        "change",
        "add",
        "remove",
        "delete",
        "rename",
    ]

    # Keywords indicating Oracle consultation (pure Q&A, not implementation)
    ORACLE_KEYWORDS = [
        "should we",
        "architecture",
        "design pattern",
        "best practice",
        "how to",
        "approach",
    ]

    # Words that signal real implementation work (must not route to oracle-only)
    IMPLEMENTATION_KEYWORDS = [
        "implement",
        "create",
        "build",
        "fix",
        "bug",
        "add",
        "remove",
        "delete",
        "rename",
        "refactor",
        "migrate",
        "update",
        "change",
        "feature",
        "epic",
    ]

    @classmethod
    def route_issue(
        cls,
        issue_key: str,
        summary: str,
        description: str,
    ) -> WorkflowType:
        """Determine workflow type for an issue (board poller intake only)."""
        del issue_key  # reserved for future per-key rules
        combined_text = f"{summary} {description}".lower()
        has_implementation = any(
            kw in combined_text for kw in cls.IMPLEMENTATION_KEYWORDS
        )
        has_oracle_phrase = any(kw in combined_text for kw in cls.ORACLE_KEYWORDS)

        # Oracle only when consultative and not asking for code/implementation work
        if has_oracle_phrase and not has_implementation:
            return WorkflowType.ORACLE_CONSULT

        complexity_score = cls._calculate_complexity(summary, description)
        if complexity_score >= 3:
            return WorkflowType.PLANNING
        return WorkflowType.DIRECT_EXECUTION

    @classmethod
    def _calculate_complexity(cls, summary: str, description: str) -> int:
        """Calculate complexity score (0-5)."""
        score = 0
        text = f"{summary} {description}".lower()

        for kw in cls.PLANNING_KEYWORDS:
            if kw in text:
                score += 1

        if len(description) > 500:
            score += 1
        if len(description) > 1000:
            score += 1

        if any(ext in text for ext in [".ts", ".js", ".py", ".java", ".go"]):
            score += 1

        return min(score, 5)

    @classmethod
    def should_auto_start(cls, workflow_type: WorkflowType) -> bool:
        """Check if workflow should auto-start without human confirmation."""
        if workflow_type == WorkflowType.DIRECT_EXECUTION:
            return True
        # Planning and Oracle typically need human confirmation (or auto_start_plans)
        return settings.auto_start_plans

    @classmethod
    def get_agent_for_workflow(cls, workflow_type: WorkflowType) -> str:
        """Get default agent for workflow type."""
        mapping = {
            WorkflowType.PLANNING: settings.planning_agent,
            WorkflowType.DIRECT_EXECUTION: settings.default_agent,
            WorkflowType.ORACLE_CONSULT: "oracle",
        }
        return mapping.get(workflow_type, settings.default_agent)

    @classmethod
    def extract_mention_command(cls, comment_text: str) -> Optional[str]:
        """Extract text after a trigger @mention (for optional /start-work style cmds)."""
        text_lower = comment_text.lower()

        for mention in settings.trigger_mentions_list:
            mention_lower = mention.lower()
            if mention_lower in text_lower:
                idx = text_lower.index(mention_lower)
                after_mention = comment_text[idx + len(mention) :].strip()
                return after_mention

        return None
