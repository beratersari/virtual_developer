"""Issue keys for Azure DevOps PR intake and stable AZ- fallback keys."""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence, Tuple

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
    """Work-item key: ``WIT-{PROJECT}-{id}`` (e.g. ``WIT-DEMO-42``).

    Project slug + id is unique enough for one daemon. Bare numeric
    keys (``42``) are still recognized as leftover state. Must not use
    the ``AZ-`` PR prefix.
    """
    from src.gitlab.keys import project_path_slug

    slug = project_path_slug(project or "project")
    try:
        iid = int(work_item_id)
    except (TypeError, ValueError):
        iid = 0
    if iid <= 0:
        return f"WIT-{slug}-0"
    return f"WIT-{slug}-{iid}"


def is_azure_work_item_key(issue_key: str) -> bool:
    """True for ``WIT-…-{id}`` or a leftover bare work-item id."""
    text = (issue_key or "").strip()
    if re.fullmatch(r"[1-9]\d*", text):
        return True
    return bool(
        re.match(r"^WIT-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+$", text.upper())
    )


_WIT_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(WIT-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d+)\b",
    re.IGNORECASE,
)
# Azure Repos / Boards: "#42" or "Fixes #42" (not /#/ in a URL).
_HASH_WORK_ITEM = re.compile(r"(?<![A-Za-z0-9/#])#([1-9]\d*)\b")


def work_item_key_from_text(text: str) -> Optional[str]:
    """First ``WIT-{PROJECT}-{id}`` in *text*, or None."""
    match = _WIT_IN_TEXT.search(text or "")
    if not match:
        return None
    return match.group(1).upper()


def work_item_id_from_hash_mention(text: str) -> Optional[int]:
    """First Azure-style ``#42`` / ``Fixes #42`` in *text*, or None."""
    match = _HASH_WORK_ITEM.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def prompt_ticket_label(issue_key: str) -> str:
    """Value for the agent ``Ticket`` / ``{ISSUE_KEY}`` field.

    Azure work items stay ``WIT-{PROJECT}-{id}`` on disk and the
    dashboard. The model only sees the numeric TFS id (``42``).
    """
    raw = (issue_key or "").strip()
    if not is_azure_work_item_key(raw):
        return raw
    _slug, wid = parse_azure_work_item_key(raw)
    return str(wid) if wid > 0 else raw


def parse_azure_work_item_key(issue_key: str) -> Tuple[str, int]:
    """Return ``(project_slug, id)`` or ``("", 0)``."""
    text = (issue_key or "").strip()
    if re.fullmatch(r"[1-9]\d*", text):
        return "", int(text)
    match = re.match(r"^WIT-([A-Z0-9]+(?:-[A-Z0-9]+)*)-(\d+)$", text.upper())
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
    repository_url: str = "",
    source_branch: str = "",
    target_branch: str = "",
    collection_url: str = "",
    state_manager: Any = None,
) -> str:
    """Bind a PR comment to a ticket, then fall back to ``AZ-…``.

    Order:
    1. Jira key in the title (``feat(KAN-12):``) when ``JIRA_PROJECTS`` matches
    2. ``WIT-{PROJECT}-{id}`` in the title
    3. Azure ``#42`` in the title (scoped to this PR's collection)
    4. Closes/Fixes Jira key in the description
    5. ``WIT-…`` in the description
    6. ``#42`` / ``Fixes #42`` in the description (same collection scope)
    7. Local work item with the same repo + source + target
    8. Stable ``AZ-{project}-{pr}`` key
    """
    from src.azure.log import azure_info
    from src.azure.workitems import find_work_item_key_by_id

    found = jira_key_from_mr_title(pr_title, project_keys)
    if found:
        azure_info(f"issue-key from PR title {found} pr={project_path}!{pr_id}")
        return found
    found = work_item_key_from_text(pr_title)
    if found:
        azure_info(f"issue-key from PR title {found} pr={project_path}!{pr_id}")
        return found
    hid = work_item_id_from_hash_mention(pr_title)
    if hid:
        found = find_work_item_key_by_id(
            hid, collection_url=collection_url, state_manager=state_manager
        )
        if found:
            azure_info(
                f"issue-key from PR #id {found} pr={project_path}!{pr_id}"
            )
            return found
    if (pr_description or "").strip():
        found = jira_key_from_closes_line(pr_description, project_keys)
        if found:
            azure_info(
                f"issue-key from PR closes {found} pr={project_path}!{pr_id}"
            )
            return found
        found = work_item_key_from_text(pr_description)
        if found:
            azure_info(
                f"issue-key from PR description {found} pr={project_path}!{pr_id}"
            )
            return found
        hid = work_item_id_from_hash_mention(pr_description)
        if hid:
            found = find_work_item_key_by_id(
                hid, collection_url=collection_url, state_manager=state_manager
            )
            if found:
                azure_info(
                    f"issue-key from PR #id {found} pr={project_path}!{pr_id}"
                )
                return found
    if repository_url and source_branch and target_branch:
        from src.azure.workitems import find_work_item_key_by_git

        found = find_work_item_key_by_git(
            repository_url,
            source_branch,
            target_branch,
            state_manager=state_manager,
        )
        if found:
            azure_info(
                f"issue-key from work-item git match {found} "
                f"pr={project_path}!{pr_id}"
            )
            return found
    key = azure_issue_key(project_path or "project", pr_id)
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
    "prompt_ticket_label",
    "resolve_pr_issue_key",
    "work_item_id_from_hash_mention",
    "work_item_key_from_text",
]
