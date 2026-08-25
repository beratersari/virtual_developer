"""Route issues to appropriate workflows."""

from enum import Enum
from typing import Optional, Tuple

from src.config import settings
from src.issue_git_spec import parse_issue_mode


class WorkflowType(Enum):
    """Types of workflows available."""

    PLANNING = "planning"  # Mode: plan — Prometheus → plan on Jira (no GitLab push)
    EXECUTION = "execution"  # Mode: build — Atlas execute → push + MR
    ORACLE_CONSULT = "oracle"  # Architecture consultation (no Mode required)


class WorkflowRouter:
    """Routes JIRA issues to appropriate workflows via ``Mode:`` in ``{params}``."""

    # Keywords indicating Oracle consultation (pure Q&A, not implementation)
    ORACLE_KEYWORDS = [
        "should we",
        "architecture",
        "design pattern",
        "best practice",
        "how to",
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
        "mode: plan",
        "mode: build",
    ]

    @classmethod
    def route_issue(
        cls,
        issue_key: str,
        summary: str,
        description: str,
    ) -> WorkflowType:
        """Determine workflow type for an issue (board poller intake only).

        Primary signal: ``Mode: plan|build`` inside the Jira ``{params}`` block.
        Direct execution is removed — use ``Mode: build`` for implementation.
        """
        del issue_key  # reserved for future per-key rules
        mode = parse_issue_mode(summary, description)
        if mode == "plan":
            return WorkflowType.PLANNING
        if mode == "build":
            return WorkflowType.EXECUTION

        combined_text = f"{summary} {description}".lower()
        has_implementation = any(
            kw in combined_text for kw in cls.IMPLEMENTATION_KEYWORDS
        )
        has_oracle_phrase = any(kw in combined_text for kw in cls.ORACLE_KEYWORDS)

        # Oracle only when consultative and not asking for implementation work
        if has_oracle_phrase and not has_implementation:
            return WorkflowType.ORACLE_CONSULT

        # No {params} Mode (and no params default): prefer planning so git
        # prepare posts the format help. A {params} block without Mode is
        # already ``build`` via parse_issue_mode.
        return WorkflowType.PLANNING

    @classmethod
    def route_issue_with_reason(
        cls,
        issue_key: str,
        summary: str,
        description: str,
    ) -> Tuple[WorkflowType, Optional[str]]:
        """Route only — never validates template fields.

        Mode / Repository / Source / Target are validated together in
        ``parse_issue_git_spec`` when the git workspace is prepared (same path
        for all incomplete templates). Returns ``(workflow, None)``.
        """
        return cls.route_issue(issue_key, summary, description), None

    @classmethod
    def should_auto_start(cls, workflow_type: WorkflowType) -> bool:
        """Whether a *new* issue can start work immediately when routed.

        Fresh ``Mode: build`` issues run execution. Planning always stops at
        ``plan_ready`` and **never** auto-starts build (intentional). Resume
        from plan_ready requires an explicit start label or a new build issue.
        """
        return workflow_type == WorkflowType.EXECUTION

    @classmethod
    def get_agent_for_workflow(cls, workflow_type: WorkflowType) -> str:
        """OpenCode agent for this workflow (oracle consult is fixed)."""
        if workflow_type == WorkflowType.ORACLE_CONSULT:
            return "oracle"
        return settings.default_agent

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
