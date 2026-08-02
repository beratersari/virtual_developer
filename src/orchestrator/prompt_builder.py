"""Build prompts for different agent types from the unified prompt kit."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.config import settings
from src.issue_git_spec import strip_params_block
from src.orchestrator.prompt_kit import get_section, substitute_issue_key


class PromptBuilder:
    """Builds prompts from ``agent/AGENT_PROMPT.md`` sections + Jira body.

    Paths:

    * **planning** — ``§role.planning`` + summary/description (no git policy)
    * **direct** — ``§role.direct`` + ``§policy.commit`` + summary/description
    * **execution** — ``§role.execution`` + ``§policy.commit`` + plan path
    * **oracle** — ``§role.oracle`` + question (no git policy)

    Jira ``{params}`` git blocks are stripped from prompt text (still used by
    GitManager for clone/push).
    """

    @staticmethod
    def _kit_path():
        return settings.prompt_kit_file

    @staticmethod
    def role_section(role: str) -> str:
        """Load a role section: planning | execution | direct | oracle."""
        section_id = role if role.startswith("role.") else f"role.{role}"
        return get_section(section_id, kit_path=PromptBuilder._kit_path())

    @staticmethod
    def commit_message_block(
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Git policy from ``§policy.commit``.

        Commit subjects always use the Jira ``issue_key``. ``work_branch`` is
        the prepared MR source (may differ from the issue key).
        """
        body = get_section(
            "policy.commit",
            kit_path=PromptBuilder._kit_path(),
            issue_key=issue_key,
            work_branch=work_branch,
        )
        return f"## Git policy\n\n{body}"

    @staticmethod
    def _join_blocks(*parts: str) -> str:
        return "\n\n".join(p.strip() for p in parts if p and p.strip()) + "\n"

    @staticmethod
    def _jira_body(
        issue_key: str,
        summary: str = "",
        description: str = "",
        *,
        extra_heading: str = "### Description",
    ) -> str:
        summary = strip_params_block(summary or "")
        description = strip_params_block(description or "")
        parts = [f"## Jira issue: {issue_key}"]
        if summary:
            parts.append(f"**Summary:** {summary}")
        if description:
            parts.append(f"{extra_heading}\n{description}")
        elif not summary:
            parts.append("(no summary or description provided)")
        return "\n\n".join(parts)

    @staticmethod
    def build_prometheus_prompt(
        issue_key: str,
        summary: str,
        description: str,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
        """Planning (Prometheus): kit §role.planning + Jira body. No git policy."""
        jira = PromptBuilder._jira_body(issue_key, summary, description)
        if acceptance_criteria and str(acceptance_criteria).strip():
            jira += f"\n\n### Acceptance criteria\n{acceptance_criteria.strip()}"

        plan_rel = f".sisyphus/plans/{issue_key}.md"
        plan_instr = (
            f"## Required plan file (mandatory)\n\n"
            f"This is an **unattended** Jira agent run — **do not wait** for human "
            f"approval, \"okay\", or chat confirmation.\n\n"
            f"Before you finish, write the **full** plan (markdown with task checkboxes) to:\n\n"
            f"`{plan_rel}`\n\n"
            f"Also acceptable: `.omo/plans/{issue_key}.md` "
            f"(drafts under `.omo/drafts/` alone are **not** enough).\n\n"
            f"Exit with success only after that file exists and is non-empty."
        )

        return PromptBuilder._join_blocks(
            "# Task planning request",
            f"## Role\n\n{PromptBuilder.role_section('planning')}",
            plan_instr,
            jira,
        )

    @staticmethod
    def build_atlas_prompt(
        issue_key: str,
        plan_path: str,
        previous_learnings: Optional[List[str]] = None,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Execution (Atlas): kit §role.execution + git policy + plan path."""
        body = (
            f"## Jira issue: {issue_key}\n\n"
            f"Execute the plan at:\n`{plan_path or '(no plan path)'}`\n"
        )
        if work_branch:
            body += (
                f"\n### Prepared git work branch\n"
                f"`{work_branch}` (already checked out — stay on it; "
                f"commit subjects use `[{issue_key}]`, not the branch name)\n"
            )
        if previous_learnings:
            body += "\n### Previous learnings\n"
            for learning in previous_learnings:
                body += f"- {learning}\n"

        return PromptBuilder._join_blocks(
            "# Task execution request",
            f"## Role\n\n{PromptBuilder.role_section('execution')}",
            PromptBuilder.commit_message_block(
                issue_key, work_branch=work_branch
            ),
            body,
        )

    @staticmethod
    def build_sisyphus_prompt(
        issue_key: str,
        task_description: str,
        context: Optional[Dict[str, Any]] = None,
        *,
        summary: str = "",
        work_branch: Optional[str] = None,
    ) -> str:
        """Direct execution (Sisyphus): kit §role.direct + git policy + Jira body.

        ``task_description`` is the Jira description (or free-form request text).
        Optional ``summary`` is the issue summary.
        """
        jira = PromptBuilder._jira_body(
            issue_key,
            summary,
            task_description,
            extra_heading="### Task",
        )
        if work_branch:
            jira += (
                f"\n\n### Prepared git work branch\n"
                f"`{work_branch}` (already checked out — stay on it; "
                f"commit subjects use `[{issue_key}]`, not the branch name)"
            )
        if context:
            ctx_bits: List[str] = []
            if context.get("files"):
                ctx_bits.append(
                    "**Relevant files:**\n"
                    + "\n".join(f"- {f}" for f in context["files"])
                )
            if context.get("patterns"):
                ctx_bits.append(
                    "**Code patterns:**\n"
                    + "\n".join(f"- {p}" for p in context["patterns"])
                )
            extra = {
                k: v for k, v in context.items() if k not in ("files", "patterns")
            }
            if extra:
                ctx_bits.append(
                    "**Other context:**\n"
                    + "\n".join(f"- {k}: {v}" for k, v in extra.items())
                )
            if ctx_bits:
                jira += "\n\n### Context\n" + "\n\n".join(ctx_bits)

        return PromptBuilder._join_blocks(
            "# Direct task execution",
            f"## Role\n\n{PromptBuilder.role_section('direct')}",
            PromptBuilder.commit_message_block(
                issue_key, work_branch=work_branch
            ),
            jira,
        )

    @staticmethod
    def build_oracle_consult_prompt(
        question: str,
        context_files: Optional[List[str]] = None,
        *,
        issue_key: str = "",
        summary: str = "",
    ) -> str:
        """Oracle: kit §role.oracle + question. No git policy."""
        parts: List[str] = []
        if issue_key:
            parts.append(PromptBuilder._jira_body(issue_key, summary, question, extra_heading="### Question"))
        else:
            parts.append(f"## Question\n{(question or '').strip() or '(empty)'}")
        if context_files:
            parts.append(
                "## Context files\n" + "\n".join(f"- {f}" for f in context_files)
            )

        return PromptBuilder._join_blocks(
            "# Architecture consultation",
            f"## Role\n\n{PromptBuilder.role_section('oracle')}",
            *parts,
        )


__all__ = ["PromptBuilder", "substitute_issue_key"]
