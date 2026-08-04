"""Placeholder substitution for plan/build prompt files."""

from __future__ import annotations

from typing import Optional


def substitute_issue_key(text: str, issue_key: str) -> str:
    """Replace ``{ISSUE_KEY}`` placeholders (literal brace form only)."""
    if not text:
        return ""
    key = (issue_key or "ISSUE").strip() or "ISSUE"
    return text.replace("{ISSUE_KEY}", key)


def substitute_placeholders(
    text: str,
    *,
    issue_key: Optional[str] = None,
    work_branch: Optional[str] = None,
    plan_path: Optional[str] = None,
) -> str:
    """Replace ``{ISSUE_KEY}``, ``{WORK_BRANCH}``, ``{PLAN_PATH}``."""
    if not text:
        return ""
    out = text
    if issue_key is not None:
        out = substitute_issue_key(out, issue_key)
    if work_branch is not None:
        # Coerce non-str (e.g. mock objects in tests) so replace never TypeErrors
        branch = str(work_branch or "").strip() or "HEAD"
        out = out.replace("{WORK_BRANCH}", branch)
    if "{WORK_BRANCH}" in out:
        key = (issue_key or "ISSUE").strip() or "ISSUE"
        out = out.replace("{WORK_BRANCH}", f"feature/{key}")
    if plan_path is not None:
        out = out.replace("{PLAN_PATH}", (plan_path or "").strip() or "(none)")
    if "{PLAN_PATH}" in out:
        key = (issue_key or "ISSUE").strip() or "ISSUE"
        out = out.replace("{PLAN_PATH}", f".sisyphus/plans/{key}.md")
    return out


# Back-compat no-ops for tests that still import cache helpers
def clear_prompt_kit_cache() -> None:
    """No-op (section kit removed; use PromptBuilder.clear_prompt_file_cache)."""
    try:
        from src.orchestrator.prompt_builder import PromptBuilder

        PromptBuilder.clear_prompt_file_cache()
    except Exception:
        pass
