"""Creasy-compatible MR/PR review *decisions* (not clone layout).

Yaver uses the same start/skip rules as MIReviewer: assign /
already-a-reviewer, ``/review`` vs ``/ask``, draft skip for auto-open
only, note edits ignored. Clone/checkout stays Yaver's existing MR/PR
workspace.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set

from src.gitlab.mentions import parse_mention_list


_ASK_WANTS_REVIEW = re.compile(
    r"(?i)\b("
    r"review again|re-?review|full review|another review|"
    r"yeniden incele|tekrar incele|tam inceleme"
    r")\b"
)
_AZURE_ADDED = re.compile(
    r"(?i)(?P<actor>.+?) added (?P<who>.+?) as an? (?:required )?reviewer"
)
_AZURE_CHANGED_LIST = re.compile(
    r"(?i)^(?P<actor>.+?) changed the reviewer list\b"
)
# Process-local. Lost on restart — first hook after boot has no previous.
_AZURE_REVIEWER_CACHE: Dict[str, frozenset] = {}


def skip_drafts() -> bool:
    try:
        from src.config import settings

        return bool(getattr(settings, "yaver_review_skip_drafts", True))
    except Exception:
        return True


def ask_wants_new_review(text: str) -> bool:
    """``/ask review again`` is a full review (Creasy ``ask_wants_new_review``)."""
    return bool(_ASK_WANTS_REVIEW.search(text or ""))


def _name_aliases(names: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for raw in names or []:
        text = str(raw or "").strip().lower().lstrip("@")
        if not text:
            continue
        out.add(text)
        if "\\" in text:
            tail = text.rsplit("\\", 1)[-1].strip()
            if tail:
                out.add(tail)
    return out


def _gitlab_trigger_names() -> List[str]:
    try:
        from src.config import settings

        names = list(getattr(settings, "gitlab_bot_mentions_list", None) or [])
        if not names:
            names = parse_mention_list(
                getattr(settings, "gitlab_trigger_user", "") or ""
            )
        return names
    except Exception:
        return []


def _azure_trigger_names() -> List[str]:
    try:
        from src.config import settings

        if hasattr(settings, "resolved_azure_trigger_user"):
            return parse_mention_list(settings.resolved_azure_trigger_user())
        return parse_mention_list(getattr(settings, "azure_trigger_user", "") or "")
    except Exception:
        return []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _gitlab_reviewer_rows(payload: Dict[str, Any], attrs: Dict[str, Any]) -> List[Any]:
    rows: List[Any] = []
    for raw in (
        payload.get("reviewers"),
        attrs.get("reviewers"),
        attrs.get("reviewer_ids"),
    ):
        if isinstance(raw, list):
            rows.extend(raw)
    return rows


def _gitlab_reviewer_matches(row: Any, names: Iterable[str]) -> bool:
    aliases = _name_aliases(names)
    if not aliases:
        return False
    if isinstance(row, dict):
        for key in ("username", "name"):
            value = str(row.get(key) or "").strip().lower().lstrip("@")
            if value and value in aliases:
                return True
        return False
    text = str(row or "").strip().lower().lstrip("@")
    return bool(text and text in aliases)


def gitlab_bot_is_reviewer(
    payload: Dict[str, Any],
    *,
    names: Optional[Iterable[str]] = None,
) -> bool:
    data = payload if isinstance(payload, dict) else {}
    attrs = _as_dict(data.get("object_attributes"))
    want = list(names) if names is not None else _gitlab_trigger_names()
    return any(
        _gitlab_reviewer_matches(row, want)
        for row in _gitlab_reviewer_rows(data, attrs)
    )


def gitlab_reviewer_just_assigned(
    payload: Dict[str, Any],
    *,
    names: Optional[Iterable[str]] = None,
) -> bool:
    """True when the bot was added or re-requested on an MR update."""
    data = payload if isinstance(payload, dict) else {}
    want = list(names) if names is not None else _gitlab_trigger_names()
    if not want:
        return False
    changes = data.get("changes") if isinstance(data.get("changes"), dict) else {}
    blob = (
        changes.get("reviewers")
        if "reviewers" in changes
        else changes.get("reviewer_ids")
    )
    previous_raw: Any = None
    current_raw: Any = None
    current_rows: List[Dict[str, Any]] = []
    if isinstance(blob, dict) and "previous" in blob and "current" in blob:
        previous_raw, current_raw = blob.get("previous"), blob.get("current")
        current_rows = [r for r in (current_raw or []) if isinstance(r, dict)]
    elif isinstance(blob, list) and len(blob) >= 2:
        previous_raw, current_raw = blob[0], blob[1]
        current_rows = [r for r in (current_raw or []) if isinstance(r, dict)]
    else:
        return False

    def _ids(raw: Any) -> Set[str]:
        found: Set[str] = set()
        if not isinstance(raw, list):
            return found
        for index, item in enumerate(raw):
            if not _gitlab_reviewer_matches(item, want):
                continue
            if isinstance(item, dict):
                found.add(str(item.get("id") or item.get("username") or index))
            else:
                found.add(str(item))
        return found

    previous = _ids(previous_raw)
    current = _ids(current_raw)
    if current - previous:
        return True
    for row in current_rows:
        if _gitlab_reviewer_matches(row, want) and row.get("re_requested") is True:
            return True
    return False


def gitlab_payload_is_draft(payload: Dict[str, Any]) -> bool:
    data = payload if isinstance(payload, dict) else {}
    attrs = _as_dict(data.get("object_attributes"))
    mr = _as_dict(data.get("merge_request"))
    for blob in (attrs, mr):
        if blob.get("draft") is True or blob.get("work_in_progress") is True:
            return True
    return False


def classify_gitlab_review_lifecycle(
    payload: Dict[str, Any],
    *,
    action: str,
) -> str:
    """``open`` / ``assign`` / ``\"\"`` (Creasy MR classify, no clone)."""
    act = (action or "").strip().lower()
    draft = gitlab_payload_is_draft(payload)
    if act in {"open", "opened"}:
        if not gitlab_bot_is_reviewer(payload):
            return ""
        if skip_drafts() and draft:
            return ""
        return "open"
    if act == "update":
        if gitlab_reviewer_just_assigned(payload):
            return "assign"
        return ""
    return ""


def azure_bot_is_reviewer(
    pr: Dict[str, Any],
    *,
    names: Optional[Iterable[str]] = None,
) -> bool:
    from src.azure.identity import reviewer_bot_aliases

    want = list(names) if names is not None else _azure_trigger_names()
    return bool(reviewer_bot_aliases(pr or {}, want))


def _azure_pr_key(pr: Dict[str, Any], collection_url: str = "") -> str:
    repo = _as_dict(pr.get("repository"))
    rid = str(repo.get("id") or repo.get("name") or "").strip().lower()
    try:
        pid = int(pr.get("pullRequestId") or pr.get("pull_request_id") or 0)
    except (TypeError, ValueError):
        pid = 0
    return f"{(collection_url or '').rstrip('/').lower()}|{rid}|{pid}"


def _azure_reviewer_ids(pr: Dict[str, Any]) -> frozenset:
    rows = pr.get("reviewers") if isinstance(pr.get("reviewers"), list) else []
    found: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        nested = row.get("user") if isinstance(row.get("user"), dict) else {}
        for raw in (
            row.get("id"),
            (nested or {}).get("id"),
            row.get("uniqueName"),
            row.get("displayName"),
            (nested or {}).get("uniqueName"),
            (nested or {}).get("displayName"),
        ):
            text = str(raw or "").strip().lower()
            if text:
                found.add(text)
    return frozenset(found)


def _azure_message_text(payload: Dict[str, Any]) -> str:
    parts: List[str] = []
    data = payload if isinstance(payload, dict) else {}
    for blob in (data.get("message"), data.get("detailedMessage")):
        item = _as_dict(blob)
        for key in ("text", "markdown", "html"):
            text = str(item.get(key) or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def azure_reviewer_just_assigned(
    payload: Dict[str, Any],
    pr: Dict[str, Any],
    *,
    collection_url: str = "",
    names: Optional[Iterable[str]] = None,
) -> bool:
    """True when the bot was added as a reviewer (Creasy TFS rules)."""
    want = list(names) if names is not None else _azure_trigger_names()
    if not want or not azure_bot_is_reviewer(pr, names=want):
        return False
    aliases = _name_aliases(want)
    msg = _azure_message_text(payload)
    added = _AZURE_ADDED.search(msg)
    if added:
        who = (added.group("who") or "").strip().lower()
        if who in aliases or any(a in who for a in aliases):
            return True
    key = _azure_pr_key(pr, collection_url)
    current = _azure_reviewer_ids(pr)
    previous = _AZURE_REVIEWER_CACHE.get(key)
    _AZURE_REVIEWER_CACHE[key] = current
    if _AZURE_CHANGED_LIST.search(msg):
        if previous is None:
            return True
        return bool(current - previous)
    if previous is None:
        return False
    return bool(current - previous) and azure_bot_is_reviewer(pr, names=want)


def azure_payload_is_draft(pr: Dict[str, Any]) -> bool:
    return bool(pr.get("isDraft") or pr.get("is_draft"))


def classify_azure_review_lifecycle(
    payload: Dict[str, Any],
    pr: Dict[str, Any],
    *,
    action: str,
    collection_url: str = "",
) -> str:
    """``open`` / ``assign`` / ``\"\"``."""
    act = (action or "").strip().lower()
    draft = azure_payload_is_draft(pr)
    if act in {"created", "create", "opened", "open"}:
        if not azure_bot_is_reviewer(pr):
            return ""
        if skip_drafts() and draft:
            return ""
        return "open"
    if act in {"updated", "update", "active"}:
        if azure_reviewer_just_assigned(
            payload, pr, collection_url=collection_url
        ):
            return "assign"
        return ""
    return ""


def note_is_edit(attrs: Dict[str, Any]) -> bool:
    """GitLab 16.11+ Note Hook also fires when a comment is edited."""
    return str((attrs or {}).get("action") or "").strip().lower() == "update"
