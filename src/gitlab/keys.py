"""Issue keys for GitLab MR intake and stable GL- fallback keys."""

from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Sequence


def project_path_slug(project_path: str, *, max_len: int = 48) -> str:
    """Filesystem-safe project slug for ``GL-`` / ``AZ-`` fallback keys.

    Short paths stay readable. Paths that would be cut at *max_len* keep an
    8-hex digest of the full slug so two long remotes cannot collide.
    """
    raw = (project_path or "project").strip().strip("/")
    parts = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").upper()
    if not parts:
        parts = "PROJECT"
    if len(parts) <= max_len:
        return parts
    digest = hashlib.sha256(parts.encode("utf-8")).hexdigest()[:8].upper()
    head_len = max(1, max_len - 1 - len(digest))
    head = parts[:head_len].rstrip("-") or "PROJECT"
    return f"{head}-{digest}"


def gitlab_issue_key(project_path: str, mr_iid: int) -> str:
    """Build a filesystem-safe fallback key: ``GL-{PROJECT-PATH}-{iid}``.

    Example: ``group/sub/repo`` + 12 → ``GL-GROUP-SUB-REPO-12``.
    Prefer :func:`jira_key_from_mr_title` when the MR title carries a real
    Jira key from ``JIRA_PROJECTS``.
    """
    parts = project_path_slug(project_path)
    try:
        iid = int(mr_iid)
    except (TypeError, ValueError):
        iid = 0
    return f"GL-{parts}-{iid}"


def is_gitlab_issue_key(issue_key: str) -> bool:
    return (issue_key or "").strip().upper().startswith("GL-")


def gitlab_note_key(
    *,
    host: str = "",
    project_path: str = "",
    project_id: object = 0,
    note_id: str = "",
) -> str:
    """Queue/dedup id. GitLab note ids restart per project, not globally."""
    nid = str(note_id or "").strip()
    if not nid:
        return ""
    proj = (project_path or "").strip().strip("/").lower()
    h = (host or "").strip().lower()
    if h and proj:
        return f"{h}/{proj}:{nid}"
    if proj:
        return f"{proj}:{nid}"
    pid = str(project_id or "").strip()
    if pid and pid != "0":
        return f"{pid}:{nid}"
    return nid


def _normalize_project_keys(project_keys: Optional[Sequence[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in project_keys or []:
        k = str(raw or "").strip().upper()
        if not k or k in seen:
            continue
        # Project keys are letters/digits only (Jira style)
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", k):
            continue
        seen.add(k)
        out.append(k)
    # INTENTIONAL: longer configured keys win, including when a shorter key
    # appears earlier in the same string. Overlapping prefixes must not parse
    # ``PROJECT-1`` as ``PROJ-1`` when both keys are configured. Distinct keys
    # in one title (``feat(KAN-12): … PLATFORM-3``) therefore bind the longer
    # key. Operators who need the leftmost ticket should put only that key in
    # the MR title (description is a fallback scan).
    out.sort(key=len, reverse=True)
    return out


def jira_key_from_text(
    text: str,
    project_keys: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Find a ``PROJECT-123`` in *text* for configured project keys.

    Matching is case-insensitive; returned key uses the configured project
    key casing (upper) + numeric id as found.

    Longer configured keys are preferred over shorter ones (intentional —
    see ``_normalize_project_keys``). This is not leftmost-in-text order.
    """
    keys = _normalize_project_keys(project_keys)
    if not keys or not (text or "").strip():
        return None
    # Word-ish boundary: not preceded by alnum (avoid XXKAN-1)
    for proj in keys:
        pat = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(proj)}-(\d+)\b",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if m:
            return f"{proj}-{m.group(1)}"
    return None


def jira_key_from_closes_line(
    text: str,
    project_keys: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Only ``Closes|Fixes|Resolves KEY-n`` in description — not free-text mentions."""
    keys = _normalize_project_keys(project_keys)
    if not keys or not (text or "").strip():
        return None
    for proj in keys:
        pat = re.compile(
            rf"(?:^|\n)\s*(?:closes|fixes|resolves)[:\s]+{re.escape(proj)}-(\d+)\b",
            re.IGNORECASE,
        )
        m = pat.search(text)
        if m:
            return f"{proj}-{m.group(1)}"
    return None


def jira_key_from_mr_title(
    title: str,
    project_keys: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """Extract a Jira issue key from an MR title using ``JIRA_PROJECTS`` keys."""
    return jira_key_from_text(title or "", project_keys)


def resolve_mr_issue_key(
    *,
    mr_title: str = "",
    mr_description: str = "",
    project_path: str = "",
    mr_iid: int = 0,
    project_keys: Optional[Sequence[str]] = None,
) -> str:
    """Prefer Jira key from MR title; description only via Closes/Fixes.

    Title is primary (e.g. ``feat(KAN-12): …`` → ``KAN-12``). A free-text
    mention in the description is ignored so ``See also PLATFORM-9`` cannot
    steal the job. Without a match, fall back to the stable ``GL-…`` key.
    """
    found = jira_key_from_mr_title(mr_title, project_keys)
    if not found and (mr_description or "").strip():
        found = jira_key_from_closes_line(mr_description, project_keys)
    if found:
        return found
    return gitlab_issue_key(project_path or "project", mr_iid)
