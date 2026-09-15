"""Issue keys for Azure DevOps PR intake and stable AZ- fallback keys."""

from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

from src.gitlab.keys import (
    jira_key_from_closes_line,
    jira_key_from_mr_title,
    jira_key_from_text,
    project_path_slug,
)


def azure_issue_key(project_path: str, pr_id: int) -> str:
    """Build a filesystem-safe fallback key: ``AZ-{PROJECT-PATH}-{id}``.

    Example: ``DefaultCollection/Demo/app`` + 12 → ``AZ-DEFAULTCOLLECTION-DEMO-APP-12``.
    Prefer a Jira key from the PR title when ``JIRA_PROJECTS`` matches.
    """
    parts = project_path_slug(project_path)
    try:
        iid = int(pr_id)
    except (TypeError, ValueError):
        iid = 0
    return f"AZ-{parts}-{iid}"


def is_azure_issue_key(issue_key: str) -> bool:
    """True for PR fallback keys ``AZ-…`` only (not work-item ``WIT-…``)."""
    text = (issue_key or "").strip().upper()
    return text.startswith("AZ-") and not text.startswith("AZWI-")


def azure_work_item_key(project: str, work_item_id: int) -> str:
    """Filesystem-safe work-item key: ``WIT-{PROJECT}-{id}``.

    Example: ``Demo`` + 12 → ``WIT-DEMO-12``. Must not use the ``AZ-`` PR
    prefix (``_is_azure_triggered`` would treat it as a PR comment job).
    """
    parts = project_path_slug(project or "project")
    try:
        iid = int(work_item_id)
    except (TypeError, ValueError):
        iid = 0
    return f"WIT-{parts}-{iid}"


def is_azure_work_item_key(issue_key: str) -> bool:
    """True for Azure Boards keys ``WIT-{slug}-{id}`` (never bare ``WIT-12``)."""
    text = (issue_key or "").strip().upper()
    return bool(re.match(r"^WIT-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+$", text))


def parse_azure_work_item_key(issue_key: str) -> Tuple[str, int]:
    """Return ``(project_slug, id)`` or ``("", 0)``."""
    text = (issue_key or "").strip().upper()
    match = re.match(r"^WIT-([A-Z0-9]+(?:-[A-Z0-9]+)*)-(\d+)$", text)
    if not match:
        return "", 0
    try:
        return match.group(1), int(match.group(2))
    except (TypeError, ValueError):
        return "", 0


def resolve_pr_issue_key(
    *,
    pr_title: str = "",
    pr_description: str = "",
    project_path: str = "",
    pr_id: int = 0,
    project_keys: Optional[Sequence[str]] = None,
) -> str:
    """Prefer Jira key from PR title; description only via Closes/Fixes.

    Same rule as GitLab MR intake so ``feat(KAN-12): …`` binds ``KAN-12``.
    Without a match, fall back to the stable ``AZ-…`` key.
    """
    found = jira_key_from_mr_title(pr_title, project_keys)
    if not found and (pr_description or "").strip():
        found = jira_key_from_closes_line(pr_description, project_keys)
    if found:
        from src.azure.log import azure_info

        azure_info(
            f"issue-key from PR title/closes {found} "
            f"pr={project_path}!{pr_id}"
        )
        return found
    key = azure_issue_key(project_path or "project", pr_id)
    from src.azure.log import azure_info

    azure_info(
        f"issue-key fallback {key} pr={project_path}!{pr_id} "
        f"title={ (pr_title or '')[:80]!r}"
    )
    return key


__all__ = [
    "azure_issue_key",
    "azure_work_item_key",
    "is_azure_issue_key",
    "is_azure_work_item_key",
    "jira_key_from_closes_line",
    "jira_key_from_mr_title",
    "jira_key_from_text",
    "parse_azure_work_item_key",
    "resolve_pr_issue_key",
]
