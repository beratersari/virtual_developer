"""Build short per-job user prompts: job facts + Jira title/description.

Stable unattended rules live on the OpenCoderman ``derman-plan`` /
``derman-build`` agents. These files only pass issue key, branch, plan
path, and Jira text.
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
    """Short user stubs for **derman-plan** and **derman-build**.

    Each run is:

    1. Job facts from ``agent/PLAN_PROMPT.md`` or ``agent/BUILD_PROMPT.md``
       (placeholders ``{ISSUE_KEY}``, ``{WORK_BRANCH}``, ``{PLAN_PATH}``)
    2. Jira title (summary)
    3. Jira description
    """

    @staticmethod
    def _agent_dir() -> Path:
        """Directory containing PLAN_PROMPT.md / BUILD_PROMPT.md."""
        candidates: list[Path] = []
        custom = getattr(settings, "agent_prompts_dir", None)
        if custom:
            p = Path(custom)
            candidates.append(p if p.is_absolute() else Path.cwd() / p)
        candidates.append(Path.cwd() / "agent")
        try:
            from src.install_paths import bundled_agent_dir, install_root

            candidates.append(install_root() / "agent")
            candidates.append(bundled_agent_dir())
        except Exception:
            pass
        seen: set[str] = set()
        for path in candidates:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if path.is_dir():
                return path
        return candidates[0] if candidates else Path.cwd() / "agent"

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
        title = (summary or "").strip() or "Oracle consultation"
        q = desc or "(no question provided)"
        return (
            f"## Oracle consultation: {key}\n\n"
            f"Answer the operator's question. Do **not** write a plan file, "
            f"do not modify product code, and do not invent a Mode/params template.\n\n"
            f"## Jira title\n\n{title}\n\n"
            f"## Question\n\n{q}\n"
        )

    @staticmethod
    def commit_message_block(
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Git policy text from build prompt (for tests / commit policy helpers)."""
        body = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=work_branch or f"feature/{issue_key}",
        )
        marker = "## Git policy"
        if marker in body:
            return marker + body.split(marker, 1)[1]
        return (
            f"## Git policy\n\n"
            f"Match this repo's AGENTS.md and git log. "
            f"If no pattern exists, commit as `[{issue_key}] <type>: <short description>`."
        )

    @staticmethod
    def build_gitlab_comment_prompt(
        *,
        issue_key: str,
        mr_title: str,
        mr_url: str,
        source_branch: str,
        target_branch: str,
        author: str,
        comment: str,
        work_branch: Optional[str] = None,
        plan_path: Optional[str] = None,
    ) -> str:
        """Build-mode prompt for a GitLab MR @mention.

        Same ``BUILD_PROMPT.md`` as Jira execution: the agent may edit, build,
        test, and commit. The orchestrator pushes onto the **existing** MR
        source branch and posts the reply as a note.
        """
        from src.issue_git_spec import strip_params_block

        comment_body = strip_params_block(comment or "").strip()
        title = strip_params_block(mr_title or "").strip()
        who = (author or "").strip() or "someone"
        branch = (work_branch or source_branch or "").strip()
        plan = (plan_path or "").strip() or f".sisyphus/plans/{issue_key}.md"
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=branch or source_branch,
            plan_path=plan,
        )
        parts = [
            system,
            f"## GitLab merge request: {issue_key}",
            (
                "This run is a **build** follow-up on an existing GitLab merge "
                "request (not a new Jira ticket). The repository is already "
                f"checked out on `{source_branch}` (MR into `{target_branch}`). "
                "Resume any existing OpenCode session for this repo + branch + "
                "target. Treat the MR comment below as the request."
            ),
            f"## MR title\n\n{title or '(no title)'}",
        ]
        if mr_url:
            parts.append(f"## MR URL\n\n{mr_url}")
        parts.append(
            f"## Branches\n\n* Source (checked out): `{source_branch}`\n"
            f"* Target: `{target_branch}`\n"
            f"* Work branch: `{branch or source_branch}`"
        )
        parts.append(f"## Comment from {who}\n\n{comment_body or '(empty comment)'}")
        parts.append(
            "## GitLab delivery\n\n"
            "Implement the comment when it asks for code changes, or when a "
            "code change is the correct answer. Stay on the prepared work "
            "branch. Commit if you change files. Do **not** push and do **not** "
            "open a new merge request — the orchestrator will push onto this "
            "existing MR. Write a clear final answer for the reviewer; it will "
            "be posted back on the MR as a note."
        )
        return PromptBuilder._join_blocks(*parts)


__all__ = ["PromptBuilder"]