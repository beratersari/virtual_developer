"""Build prompts for plan vs build: two files + Jira title/description.

Mode is the only switch (``Mode: plan`` vs ``Mode: build``). OpenCode agent
names do not change prompt text.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from src.config import settings
from src.issue_git_spec import strip_params_block
from src.logger import logger
from src.orchestrator.prompt_kit import substitute_placeholders


class PromptBuilder:
    """Exactly two prompts: **plan** and **build**.

    Each run is:

    1. Full text from ``agent/PLAN_PROMPT.md`` or ``agent/BUILD_PROMPT.md``
       (placeholders ``{ISSUE_KEY}``, ``{WORK_BRANCH}``, ``{PLAN_PATH}``)
    2. Jira title (summary)
    3. Jira description

    Agent selection (prometheus/atlas/…) does **not** change the prompt body.
    """

    @staticmethod
    def _agent_dir() -> Path:
        """Directory containing PLAN_PROMPT.md / BUILD_PROMPT.md."""
        custom = getattr(settings, "agent_prompts_dir", None)
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else Path.cwd() / p
        return Path.cwd() / "agent"

    @staticmethod
    def plan_prompt_path() -> Path:
        custom = getattr(settings, "plan_prompt_file", None)
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else Path.cwd() / p
        return PromptBuilder._agent_dir() / "PLAN_PROMPT.md"

    @staticmethod
    def build_prompt_path() -> Path:
        custom = getattr(settings, "build_prompt_file", None)
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else Path.cwd() / p
        return PromptBuilder._agent_dir() / "BUILD_PROMPT.md"

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
    @lru_cache(maxsize=16)
    def _read_prompt_file_cached(path_str: str, mtime_ns: int) -> str:
        del mtime_ns  # cache key only
        return Path(path_str).read_text(encoding="utf-8")

    @staticmethod
    def clear_prompt_file_cache() -> None:
        """Drop file cache (tests / hot-reload after edit)."""
        PromptBuilder._read_prompt_file_cached.cache_clear()

    @staticmethod
    def _load_mode_prompt(
        path: Path,
        *,
        issue_key: str,
        work_branch: Optional[str] = None,
        plan_path: Optional[str] = None,
    ) -> str:
        """Load one mode file and substitute placeholders."""
        text = ""
        if path.is_file():
            try:
                stat = path.stat()
                text = PromptBuilder._read_prompt_file_cached(
                    str(path.resolve()), stat.st_mtime_ns
                )
            except OSError as e:
                logger.warning(f"Could not read prompt file {path}: {e}")
        if not text.strip():
            logger.warning(f"Prompt file missing or empty: {path}; using minimal stub")
            text = (
                f"# Mode prompt missing\n\n"
                f"Work on issue {{ISSUE_KEY}}. Plan path: {{PLAN_PATH}}. "
                f"Work branch: {{WORK_BRANCH}}.\n"
            )

        out = substitute_placeholders(
            text,
            issue_key=issue_key,
            work_branch=work_branch,
            plan_path=plan_path,
        )
        return out.strip()

    @staticmethod
    def build_plan_prompt(
        issue_key: str,
        summary: str,
        description: str,
        *,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
        """Plan mode: ``PLAN_PROMPT.md`` + Jira title + description."""
        plan_rel = f".sisyphus/plans/{issue_key}.md"
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.plan_prompt_path(),
            issue_key=issue_key,
            plan_path=plan_rel,
        )
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
        """Build mode: ``BUILD_PROMPT.md`` + Jira title + description."""
        plan = (plan_path or "").strip() or f".sisyphus/plans/{issue_key}.md"
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=work_branch,
            plan_path=plan,
        )
        jira = PromptBuilder._jira_title_and_description(
            issue_key, summary, description
        )
        return PromptBuilder._join_blocks(system, jira)

    # --- Compatibility aliases (same two paths; agent name does not matter) ---

    @staticmethod
    def build_prometheus_prompt(
        issue_key: str,
        summary: str,
        description: str,
        acceptance_criteria: Optional[str] = None,
    ) -> str:
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
        """Consult uses the plan-mode prompt + question as description."""
        desc = (question or "").strip()
        if context_files:
            desc = (
                desc
                + "\n\n### Context files\n"
                + "\n".join(f"- {f}" for f in context_files)
            ).strip()
        key = issue_key or "CONSULT"
        return PromptBuilder.build_plan_prompt(key, summary or "", desc)

    # Legacy helpers used by a few tests / docs — thin wrappers, not section kit.

    @staticmethod
    def unattended_policy_block() -> str:
        """Best-effort extract of unattended guidance from plan file."""
        body = PromptBuilder._load_mode_prompt(
            PromptBuilder.plan_prompt_path(),
            issue_key="ISSUE",
        )
        return body

    @staticmethod
    def commit_message_block(
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Best-effort git policy text from build prompt (for tests/compat)."""
        body = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=work_branch or f"feature/{issue_key}",
        )
        # Prefer the Git policy section if present
        marker = "## Git policy"
        if marker in body:
            return marker + body.split(marker, 1)[1]
        return f"## Git policy\n\nCommit as `[{issue_key}] <type>: <short description>`."

    @staticmethod
    def role_section(role: str) -> str:
        """Compat: planning → plan file, anything else → build file."""
        if "plan" in (role or "").lower():
            return PromptBuilder._load_mode_prompt(
                PromptBuilder.plan_prompt_path(),
                issue_key="ISSUE",
            )
        return PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key="ISSUE",
        )


__all__ = ["PromptBuilder"]
