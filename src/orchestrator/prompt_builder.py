"""Build prompts for plan vs build workflows from the unified prompt kit."""

from __future__ import annotations

from typing import Optional

from src.config import settings
from src.issue_git_spec import strip_params_block
from src.orchestrator.prompt_kit import get_section, substitute_issue_key


class PromptBuilder:
    """Exactly two prompt paths: **plan** and **build**.

    Both concatenate:

    1. **System** — unattended policy + role (+ commit policy / plan path for build)
    2. **Jira title** (summary)
    3. **Jira description**

    Jira ``{params}`` git blocks are stripped from prompt text (still used by
    GitManager for clone/push). Agent choice (prometheus/atlas/…) is separate
    from which of these two prompt shapes is used.
    """

    @staticmethod
    def _kit_path():
        return settings.prompt_kit_file

    @staticmethod
    def role_section(role: str) -> str:
        """Load a role section: planning | execution."""
        section_id = role if role.startswith("role.") else f"role.{role}"
        return get_section(section_id, kit_path=PromptBuilder._kit_path())

    @staticmethod
    def unattended_policy_block() -> str:
        """Daemon non-interactive policy from ``§policy.unattended``."""
        body = get_section(
            "policy.unattended",
            kit_path=PromptBuilder._kit_path(),
        )
        return f"## Unattended run (no questions)\n\n{body}"

    @staticmethod
    def commit_message_block(
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Git policy from ``§policy.commit``."""
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
    def _jira_title_and_description(
        issue_key: str,
        summary: str = "",
        description: str = "",
    ) -> str:
        """Jira title + description only (params stripped)."""
        title = strip_params_block(summary or "").strip()
        body = strip_params_block(description or "").strip()
        parts = [f"## Jira issue: {issue_key}"]
        if title:
            parts.append(f"## Jira title\n\n{title}")
        if body:
            parts.append(f"## Jira description\n\n{body}")
        if not title and not body:
            parts.append("(no summary or description provided)")
        return "\n\n".join(parts)

    @staticmethod
    def build_plan_prompt(
        issue_key: str,
        summary: str,
        description: str,
        *,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
        """Planning path: system (unattended + planning role) + title + description."""
        plan_rel = f".sisyphus/plans/{issue_key}.md"
        plan_instr = (
            f"## Required plan file (mandatory)\n\n"
            f"Before you finish, write the **full** plan (markdown with task "
            f"checkboxes / to-do items) to:\n\n`{plan_rel}`\n\n"
            f"Also acceptable: `.omo/plans/{issue_key}.md` "
            f"(drafts under `.omo/drafts/` alone are **not** enough).\n\n"
            f"The to-do list **must** end with a commit item for the build agent, e.g.\n"
            f"`[ ] Commit with the conventional format if files changed: "
            f"`[{issue_key}] <type>: <short description>``\n\n"
            f"Exit with success only after that file exists, is non-empty, and "
            f"includes the commit to-do."
        )
        system = PromptBuilder._join_blocks(
            "# Plan request",
            PromptBuilder.unattended_policy_block(),
            f"## Role\n\n{PromptBuilder.role_section('planning')}",
            plan_instr,
        ).rstrip()

        jira = PromptBuilder._jira_title_and_description(
            issue_key, summary, description
        )
        if acceptance_criteria and str(acceptance_criteria).strip():
            jira += (
                f"\n\n### Acceptance criteria\n"
                f"{str(acceptance_criteria).strip()}"
            )

        return PromptBuilder._join_blocks(system, jira)

    @staticmethod
    def build_build_prompt(
        issue_key: str,
        summary: str,
        description: str,
        *,
        plan_path: Optional[str] = None,
        work_branch: Optional[str] = None,
    ) -> str:
        """Build path: Atlas system (unattended + execution + commit) + title + description."""
        atlas_ops = (
            f"## Atlas build order (mandatory)\n\n"
            f"1. **Plan first** — read the plan file if present; otherwise re-plan briefly.\n"
            f"2. **Create to-do items** before heavy code edits. Todos **must** include:\n"
            f"   - Implementation steps\n"
            f"   - Verification when practical\n"
            f"   - **Commit with the convention below if you made changes:**\n"
            f"     `[{issue_key}] <type>: <short description>`\n"
            f"3. **Then** edit code, check off todos, and complete the commit todo last "
            f"when files changed.\n"
            f"Do not ask the user questions. Do not push or open an MR.\n"
        )
        system_parts = [
            "# Build request (Atlas)",
            PromptBuilder.unattended_policy_block(),
            f"## Role\n\n{PromptBuilder.role_section('execution')}",
            atlas_ops,
            PromptBuilder.commit_message_block(
                issue_key, work_branch=work_branch
            ),
        ]
        if plan_path and str(plan_path).strip():
            system_parts.append(
                f"## Plan file\n\n"
                f"Execute the plan at:\n`{str(plan_path).strip()}`\n"
            )
        if work_branch and str(work_branch).strip():
            wb = str(work_branch).strip()
            system_parts.append(
                f"## Prepared git work branch\n\n"
                f"`{wb}` (already checked out — stay on it; "
                f"commit subjects use `[{issue_key}]`, not the branch name)\n"
            )

        system = PromptBuilder._join_blocks(*system_parts).rstrip()
        jira = PromptBuilder._jira_title_and_description(
            issue_key, summary, description
        )
        return PromptBuilder._join_blocks(system, jira)

    # --- Compatibility aliases (map old names → the two paths only) ---

    @staticmethod
    def build_prometheus_prompt(
        issue_key: str,
        summary: str,
        description: str,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
        """Alias → :meth:`build_plan_prompt`."""
        return PromptBuilder.build_plan_prompt(
            issue_key,
            summary,
            description,
            acceptance_criteria=acceptance_criteria,
        )

    @staticmethod
    def build_atlas_prompt(
        issue_key: str,
        plan_path: str = "",
        previous_learnings: Optional[list] = None,
        *,
        summary: str = "",
        description: str = "",
        work_branch: Optional[str] = None,
    ) -> str:
        """Alias → :meth:`build_build_prompt` (includes summary + description)."""
        # previous_learnings folded into description when present (compat)
        desc = description or ""
        if previous_learnings:
            extra = "\n".join(f"- {x}" for x in previous_learnings if x)
            if extra:
                desc = (desc + "\n\n### Notes\n" + extra).strip()
        return PromptBuilder.build_build_prompt(
            issue_key,
            summary,
            desc,
            plan_path=plan_path or None,
            work_branch=work_branch,
        )

    @staticmethod
    def build_sisyphus_prompt(
        issue_key: str,
        task_description: str,
        context: Optional[dict] = None,
        *,
        summary: str = "",
        work_branch: Optional[str] = None,
    ) -> str:
        """Alias → :meth:`build_build_prompt`."""
        desc = task_description or ""
        if context:
            bits: list[str] = []
            if context.get("files"):
                bits.append(
                    "**Relevant files:**\n"
                    + "\n".join(f"- {f}" for f in context["files"])
                )
            if context.get("patterns"):
                bits.append(
                    "**Code patterns:**\n"
                    + "\n".join(f"- {p}" for p in context["patterns"])
                )
            extra = {
                k: v for k, v in context.items() if k not in ("files", "patterns")
            }
            if extra:
                bits.append(
                    "**Other context:**\n"
                    + "\n".join(f"- {k}: {v}" for k, v in extra.items())
                )
            if bits:
                desc = (desc + "\n\n### Context\n" + "\n\n".join(bits)).strip()
        return PromptBuilder.build_build_prompt(
            issue_key,
            summary,
            desc,
            work_branch=work_branch,
        )

    @staticmethod
    def build_oracle_consult_prompt(
        question: str,
        context_files: Optional[list] = None,
        *,
        issue_key: str = "",
        summary: str = "",
    ) -> str:
        """Alias → :meth:`build_plan_prompt` (consult uses plan-shaped system + Jira body)."""
        desc = (question or "").strip()
        if context_files:
            desc = (
                desc
                + "\n\n### Context files\n"
                + "\n".join(f"- {f}" for f in context_files)
            ).strip()
        key = issue_key or "CONSULT"
        return PromptBuilder.build_plan_prompt(key, summary or "", desc)


__all__ = ["PromptBuilder", "substitute_issue_key"]
