"""Issue keys for GitLab MR intake and stable GL- fallback keys."""

from __future__ import annotations

import hashlib
import re
from typing import Any, List, Optional, Sequence
from urllib.parse import urlparse


def project_path_slug(project_path: str, *, max_len: int = 48) -> str:
    """Filesystem-safe project slug for ``GL-`` / ``AZ-`` fallback keys.

    Short paths stay readable. ``/`` still becomes ``-`` so ``acme/demo``
    stays ``ACME-DEMO``. ``-``, ``_``, and ``.`` are marked first so they
    do not collapse into that same hyphen. A trailing ``.git`` stays the
    historical ``-GIT`` suffix. Paths that would be cut at *max_len* keep
    an 8-hex digest of the original path.
    """
    raw = (project_path or "project").strip().strip("/")
    body = raw
    git_suffix = ""
    if body.lower().endswith(".git"):
        body = body[:-4].strip().strip("/")
        git_suffix = "-GIT"
    marked = body.replace("-", "-H-").replace("_", "-U-").replace(".", "-D-")
    parts = re.sub(r"[^A-Za-z0-9]+", "-", marked).strip("-").upper()
    if git_suffix:
        parts = f"{parts}{git_suffix}" if parts else "GIT"
    if not parts:
        parts = "PROJECT"
    if len(parts) <= max_len:
        return parts
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8].upper()
    head_len = max(1, max_len - 1 - len(digest))
    head = parts[:head_len].rstrip("-") or "PROJECT"
    return f"{head}-{digest}"


def _host_from_repository_url(repository_url: str) -> str:
    raw = (repository_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@"):
        rest = raw[4:]
        return rest.split(":", 1)[0].strip().lower()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        return (urlparse(raw).hostname or "").strip().lower()
    except Exception:
        return ""


def _host_slug(host: str) -> str:
    """Hostname slug. Dots become hyphens; a hyphen in the hostname stays marked."""
    raw = (host or "").strip().lower()
    if not raw:
        return ""
    marked = raw.replace("-", "-H-")
    parts = re.sub(r"[^a-z0-9]+", "-", marked).strip("-").upper()
    return "" if parts == "PROJECT" else parts


def gitlab_issue_key(project_path: str, mr_iid: int, host: str = "") -> str:
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
    host_slug = _host_slug(host)
    if host_slug:
        return f"GL-{host_slug}-{parts}-{iid}"
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
    repository_url: str = "",
    source_branch: str = "",
    target_branch: str = "",
    collection_url: str = "",
    state_manager: Any = None,
) -> str:
    """Bind an MR comment to a ticket, then fall back to ``GL-…``.

    Same order as Azure PRs, with Jira always first:
    1. Jira key in the title when ``JIRA_PROJECTS`` matches
    2. ``WIT-{PROJECT}-{id}`` in the title
    3. Azure ``#42`` in the title (scoped by collection when known)
    4. Closes/Fixes Jira key in the description
    5. ``WIT-…`` in the description
    6. ``#42`` / ``Fixes #42`` in the description
    7. Local work item with the same repo + source + target
    8. Stable ``GL-{project}-{iid}`` key
    """
    from src.azure.keys import work_item_id_from_hash_mention, work_item_key_from_text
    from src.azure.workitems import find_work_item_key_by_id

    found = jira_key_from_mr_title(mr_title, project_keys)
    if found:
        return found
    found = work_item_key_from_text(mr_title)
    if found:
        return found
    hid = work_item_id_from_hash_mention(mr_title)
    if hid:
        found = find_work_item_key_by_id(
            hid, collection_url=collection_url, state_manager=state_manager
        )
        if found:
            return found
    if (mr_description or "").strip():
        found = jira_key_from_closes_line(mr_description, project_keys)
        if found:
            return found
        found = work_item_key_from_text(mr_description)
        if found:
            return found
        hid = work_item_id_from_hash_mention(mr_description)
        if hid:
            found = find_work_item_key_by_id(
                hid, collection_url=collection_url, state_manager=state_manager
            )
            if found:
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
            return found
    return gitlab_issue_key(
        project_path or "project",
        mr_iid,
        host=_host_from_repository_url(repository_url),
    )
