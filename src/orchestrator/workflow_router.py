"""Route issues to appropriate workflows."""

from enum import Enum
from typing import Optional, Tuple

from src.config import settings
from src.issue_git_spec import parse_issue_mode


class WorkflowType(Enum):
    """Types of workflows available."""

    PLANNING = "planning"  # Mode: plan — derman-plan (no GitLab push)
    EXECUTION = "execution"  # Mode: build — derman-build → push + MR
    TESTING = "testing"  # Mode: test — derman-test → unit tests, push + MR


class WorkflowRouter:
    """Routes JIRA issues to appropriate workflows via ``Mode:`` in ``{params}``."""

    @classmethod
    def route_issue(
        cls,
        issue_key: str,
        summary: str,
        description: str,
    ) -> WorkflowType:
        """Determine workflow type for an issue (board poller intake only).

        Primary signal: ``Mode: plan|build|test`` inside the Jira ``{params}`` block.
        Direct execution is removed — use ``Mode: build`` for implementation.
        """
        del issue_key  # reserved for future per-key rules
        mode = parse_issue_mode(summary, description)
        if mode == "plan":
            return WorkflowType.PLANNING
        if mode == "build":
            return WorkflowType.EXECUTION
        if mode == "test":
            return WorkflowType.TESTING

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
        ``plan_ready``. Same-ticket implement uses label ``plan_execute``;
        a new ``Mode: build`` issue is still a direct build.
        """
        return workflow_type in (WorkflowType.EXECUTION, WorkflowType.TESTING)

    @classmethod
    def get_agent_for_workflow(cls, workflow_type: WorkflowType) -> str:
        """OpenCode agent for this workflow."""
        if workflow_type == WorkflowType.PLANNING:
            plan = getattr(settings, "default_plan_agent", None)
            if isinstance(plan, str) and plan.strip():
                return plan.strip()
        if workflow_type == WorkflowType.TESTING:
            test = getattr(settings, "default_test_agent", None)
            if isinstance(test, str) and test.strip():
                return test.strip()
            return "derman-test"
        agent = getattr(settings, "default_agent", None)
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
        return "derman-build"

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
