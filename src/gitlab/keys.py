"""Stable local issue keys for GitLab merge requests."""

from __future__ import annotations

import re


def gitlab_issue_key(project_path: str, mr_iid: int) -> str:
    """Build a filesystem-safe key: ``GL-{PROJECT-PATH}-{iid}``.

    Example: ``group/sub/repo`` + 12 → ``GL-GROUP-SUB-REPO-12``.
    Used as JobStore / state ``issue_key`` so dashboard jobs match Jira jobs.
    """
    raw = (project_path or "project").strip().strip("/")
    parts = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").upper()
    if not parts:
        parts = "PROJECT"
    parts = parts[:48].rstrip("-") or "PROJECT"
    try:
        iid = int(mr_iid)
    except (TypeError, ValueError):
        iid = 0
    return f"GL-{parts}-{iid}"


def is_gitlab_issue_key(issue_key: str) -> bool:
    return (issue_key or "").strip().upper().startswith("GL-")
