"""Build short per-job user prompts: job facts + Jira title/description.

Stable unattended rules live on the OpenCoderman ``derman-plan`` /
``derman-build`` / ``derman-test`` agents. These files only pass issue
key, branch, plan path, and Jira text.
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
        """Directory containing PLAN_PROMPT.md / BUILD_PROMPT.md / TEST_PROMPT.md."""
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
    def test_prompt_path() -> Path:
        custom = getattr(settings, "test_prompt_file", None)
        if custom:
            p = Path(custom)
            return p if p.is_absolute() else Path.cwd() / p
        return PromptBuilder._agent_dir() / "TEST_PROMPT.md"

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
        plan_path: Optional[str] = None,
    ) -> str:
        """Plan mode: ``PLAN_PROMPT.md`` + Jira title + description."""
        from src.paths import plans_dir

        plan_abs = (plan_path or "").strip() or str(
            plans_dir() / f"{issue_key}.md"
        )
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.plan_prompt_path(),
            issue_key=issue_key,
            plan_path=plan_abs,
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
        """Build mode: implement the plan when it exists, else Jira text.

        ``Mode: build`` is not ``plan_execute``. When a durable plan file
        is present (this ticket or a sibling plan for the same repo /
        branches), that file is the spec. Jira is context only.
        """
        from src.paths import plans_dir

        plan = (plan_path or "").strip() or str(plans_dir() / f"{issue_key}.md")
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=work_branch,
            plan_path=plan,
        )
        jira = PromptBuilder._jira_title_and_description(
            issue_key, summary, description
        )
        plan_exists = False
        try:
            plan_exists = bool(plan) and Path(plan).is_file()
        except OSError:
            plan_exists = False
        if plan_exists:
            lead = PromptBuilder.build_plan_execute_prompt(
                plan, issue_key=issue_key
            )
            context = (
                "## Jira context (do not replace the plan)\n\n"
                "Implement the plan above. Title and description are "
                "background only unless the plan is missing a detail.\n\n"
                + jira
            )
            return PromptBuilder._join_blocks(lead, system, context)
        return PromptBuilder._join_blocks(system, jira)

    @staticmethod
    def build_test_prompt(
        issue_key: str,
        summary: str,
        description: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Test mode: unit tests only. Read this clone's AGENTS.md first."""
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.test_prompt_path(),
            issue_key=issue_key,
            work_branch=work_branch,
        )
        jira = PromptBuilder._jira_title_and_description(
            issue_key, summary, description
        )
        return PromptBuilder._join_blocks(system, jira)

    @staticmethod
    def build_plan_execute_prompt(
        plan_path: str,
        *,
        issue_key: str = "",
    ) -> str:
        """Same-ticket plan→build: continue the *build* session.

        The plan may live under the host data ``plans/`` dir (absolute
        path). Naming only that path made the model treat the data dir
        as the project. Name the plan as ``{ISSUE_KEY}.md`` and say the
        clone cwd is the only workdir.
        """
        path = (plan_path or "").strip() or "plan.md"
        key = (issue_key or "").strip()
        name = f"{key}.md" if key else Path(path).name
        return (
            f"implement the plan {name}\n\n"
            f"Read the plan at this absolute path (Yaver data dir — "
            f"not the product repository):\n"
            f"{path}\n\n"
            f"Do all implementation in the current working directory "
            f"(the git clone already checked out). Do not treat the "
            f"plan file's parent directory as the project. Do not copy "
            f"or commit the plan file.\n"
        )

    @staticmethod
    def build_plan_refactor_prompt(
        issue_key: str,
        comment: str,
        *,
        plan_path: Optional[str] = None,
    ) -> str:
        """Revise the existing plan from a Jira comment (same plan session)."""
        from src.paths import plans_dir

        plan = (plan_path or "").strip() or str(plans_dir() / f"{issue_key}.md")
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.plan_prompt_path(),
            issue_key=issue_key,
            plan_path=plan,
        )
        body = (comment or "").strip() or "(empty comment)"
        extra = (
            f"## Plan refactor\n\n"
            f"Revise the existing plan at `{plan}`. Overwrite that file. "
            f"Do not implement product code.\n\n"
            f"## Operator comment\n\n{body}"
        )
        return PromptBuilder._join_blocks(system, extra)

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
    def parent_comment_from_webhook_raw(raw: Optional[dict]) -> str:
        """Parent / replied-to comment body from a GitLab or Azure webhook payload."""
        if not isinstance(raw, dict):
            return ""
        resource = raw.get("resource")
        comment = resource.get("comment") if isinstance(resource, dict) else None
        if not isinstance(comment, dict):
            comment = raw.get("comment") if isinstance(raw.get("comment"), dict) else {}
        parent = (
            comment.get("parentComment")
            or comment.get("parent_comment")
            or raw.get("parentComment")
        )
        if isinstance(parent, dict):
            text = (
                parent.get("content")
                or parent.get("comments")
                or parent.get("body")
                or parent.get("note")
                or ""
            )
            if str(text).strip():
                return str(text).strip()
        if isinstance(parent, str) and parent.strip():
            return parent.strip()
        return ""

    @staticmethod
    def _thread_request_sections(
        *,
        author: str,
        prompt: str,
        replied_message: str = "",
        forge: str,
        source_branch: str,
        target_branch: str,
    ) -> list[str]:
        """Labeled thread-follow-up blocks (Creasy-style replied + prompt)."""
        from src.issue_git_spec import strip_params_block

        who = (author or "").strip() or "someone"
        request = strip_params_block(prompt or "").strip() or "(empty prompt)"
        replied = strip_params_block(replied_message or "").strip()
        if not replied:
            replied = request
        where = (
            "GitLab merge request"
            if forge == "gitlab"
            else "Azure DevOps pull request"
        )
        return [
            (
                f"This run is a **thread follow-up** on an existing {where} "
                "(not a new Jira ticket). The repository is already checked out "
                f"on `{source_branch}` (into `{target_branch}`). Resume any "
                "existing OpenCode session for this repo + branch + target.\n\n"
                "Use **Replied message** as context only. Do what **Prompt** says. "
                "Do not quote or restate the replied message in the posted answer. "
                "Do not @mention or ping anyone."
            ),
            f"## Replied message\n\nFrom {who}:\n\n{replied}",
            f"## Prompt\n\n{request}",
        ]

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
        replied_message: str = "",
        raw: Optional[dict] = None,
    ) -> str:
        """Build-mode prompt for a GitLab MR thread comment.

        Kit: ``agent/BUILD_PROMPT.md`` (derman-build). Then structured
        **Replied message** + **Prompt** sections so the model does not
        mix thread context with the operator request.
        """
        from src.issue_git_spec import strip_params_block
        from src.paths import plans_dir

        title = strip_params_block(mr_title or "").strip()
        branch = (work_branch or source_branch or "").strip()
        plan = (plan_path or "").strip() or str(plans_dir() / f"{issue_key}.md")
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=branch or source_branch,
            plan_path=plan,
        )
        replied = (
            PromptBuilder.parent_comment_from_webhook_raw(raw)
            or (replied_message or "").strip()
        )
        parts = [
            system,
            f"## GitLab merge request: {issue_key}",
            *PromptBuilder._thread_request_sections(
                author=author,
                prompt=comment,
                replied_message=replied,
                forge="gitlab",
                source_branch=source_branch,
                target_branch=target_branch,
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
        parts.append(
            "## GitLab delivery\n\n"
            "Implement the **Prompt** when it asks for code changes, or when a "
            "code change is the correct answer. Stay on the prepared work "
            "branch. Commit if you change files. Do **not** push and do **not** "
            "open a new merge request — the orchestrator will push onto this "
            "existing MR. Write a clear final answer for the reviewer; it will "
            "be posted back on the MR as a note."
        )
        return PromptBuilder._join_blocks(*parts)

    @staticmethod
    def build_azure_comment_prompt(
        *,
        issue_key: str,
        pr_title: str,
        pr_url: str,
        source_branch: str,
        target_branch: str,
        author: str,
        comment: str,
        work_branch: Optional[str] = None,
        plan_path: Optional[str] = None,
        replied_message: str = "",
        raw: Optional[dict] = None,
    ) -> str:
        """Build-mode prompt for an Azure DevOps PR thread comment.

        Same kit and section layout as GitLab: ``BUILD_PROMPT.md`` plus
        **Replied message** and **Prompt**.
        """
        from src.issue_git_spec import strip_params_block
        from src.paths import plans_dir

        title = strip_params_block(pr_title or "").strip()
        branch = (work_branch or source_branch or "").strip()
        plan = (plan_path or "").strip() or str(plans_dir() / f"{issue_key}.md")
        system = PromptBuilder._load_mode_prompt(
            PromptBuilder.build_prompt_path(),
            issue_key=issue_key,
            work_branch=branch or source_branch,
            plan_path=plan,
        )
        replied = (
            PromptBuilder.parent_comment_from_webhook_raw(raw)
            or (replied_message or "").strip()
        )
        parts = [
            system,
            f"## Azure DevOps pull request: {issue_key}",
            *PromptBuilder._thread_request_sections(
                author=author,
                prompt=comment,
                replied_message=replied,
                forge="azure",
                source_branch=source_branch,
                target_branch=target_branch,
            ),
            f"## PR title\n\n{title or '(no title)'}",
        ]
        if pr_url:
            parts.append(f"## PR URL\n\n{pr_url}")
        parts.append(
            f"## Branches\n\n* Source (checked out): `{source_branch}`\n"
            f"* Target: `{target_branch}`\n"
            f"* Work branch: `{branch or source_branch}`"
        )
        parts.append(
            "## Azure DevOps delivery\n\n"
            "Implement the **Prompt** when it asks for code changes, or when a "
            "code change is the correct answer. Stay on the prepared work "
            "branch. Commit if you change files. Do **not** push and do **not** "
            "open a new pull request — the orchestrator will push onto this "
            "existing PR. Write a clear final answer for the reviewer; it will "
            "be posted back on the PR as a comment."
        )
        return PromptBuilder._join_blocks(*parts)


__all__ = ["PromptBuilder"]