"""Issue keys for Azure DevOps PR intake and stable AZ- fallback keys."""

from __future__ import annotations

import re
from typing import Optional, Sequence

from src.gitlab.keys import (
    jira_key_from_closes_line,
    jira_key_from_mr_title,
    jira_key_from_text,
)


def azure_issue_key(project_path: str, pr_id: int) -> str:
    """Build a filesystem-safe fallback key: ``AZ-{PROJECT-PATH}-{id}``.

    Example: ``DefaultCollection/Demo/app`` + 12 → ``AZ-DEFAULTCOLLECTION-DEMO-APP-12``.
    Prefer a Jira key from the PR title when ``JIRA_PROJECTS`` matches.
    """
    raw = (project_path or "project").strip().strip("/")
    parts = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").upper()
    if not parts:
        parts = "PROJECT"
    parts = parts[:48].rstrip("-") or "PROJECT"
    try:
        iid = int(pr_id)
    except (TypeError, ValueError):
        iid = 0
    return f"AZ-{parts}-{iid}"


def is_azure_issue_key(issue_key: str) -> bool:
    return (issue_key or "").strip().upper().startswith("AZ-")


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
    "is_azure_issue_key",
    "jira_key_from_closes_line",
    "jira_key_from_mr_title",
    "jira_key_from_text",
    "resolve_pr_issue_key",
]
