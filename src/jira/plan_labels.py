"""Plan → build handoff labels and Jira comment matching.

Plan/build transition is label-driven (not ``Mode: build`` on the same
ticket). Direct ``Mode: build`` issues are unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from src.jira.triggers import (
    comment_is_bot_output,
    comment_mentions_target,
    jira_body_to_text,
    normalize_needles,
)

PLAN_READY_LABEL = "plan_ready"
PLAN_REFACTOR_LABEL = "plan_refactor"
PLAN_EXECUTE_LABEL = "plan_execute"
PLAN_EXECUTED_LABEL = "plan_executed"

HANDOFF_EXECUTE = "execute"
HANDOFF_REFACTOR = "refactor"

_IN_PROGRESS_NAMES = frozenset(
    {
        "in progress",
        "in-progress",
        "inprogress",
        "devam ediyor",
    }
)


def normalize_label_set(labels: Optional[Iterable[Any]]) -> Set[str]:
    out: Set[str] = set()
    for raw in labels or []:
        name = str(raw or "").strip().lower()
        if name:
            out.add(name)
    return out


def labels_from_fields(fields: Optional[dict]) -> Set[str]:
    if not isinstance(fields, dict):
        return set()
    return normalize_label_set(fields.get("labels"))


_TODO_NAMES = frozenset(
    {
        "to do",
        "todo",
        "open",
        "backlog",
        "new",
        "selected for development",
        "ready for development",
        "yapılacaklar",
        "yapilacaklar",
    }
)


def is_in_progress_status(fields: Optional[dict]) -> bool:
    """True for In Progress (locale-safe via statusCategory=indeterminate)."""
    if not isinstance(fields, dict):
        return False
    status = fields.get("status") or {}
    if not isinstance(status, dict):
        return False
    category = ((status.get("statusCategory") or {}).get("key") or "").lower()
    if category == "indeterminate":
        return True
    name = (status.get("name") or "").strip().lower()
    return name in _IN_PROGRESS_NAMES


def is_todo_like_status(fields: Optional[dict]) -> bool:
    """True for To Do / backlog-like columns (statusCategory=new)."""
    if not isinstance(fields, dict):
        return False
    status = fields.get("status") or {}
    if not isinstance(status, dict):
        return False
    category = ((status.get("statusCategory") or {}).get("key") or "").lower()
    if category == "new":
        return True
    name = (status.get("name") or "").strip().lower()
    return name in _TODO_NAMES


def infer_plan_handoff(fields: Optional[dict]) -> Optional[str]:
    """Return ``execute`` / ``refactor`` when Jira labels + column match.

    * ``plan_execute`` + In Progress → implement (even if Mode is still plan)
    * ``plan_refactor`` without ``plan_ready`` + To Do or In Progress → revise
    """
    labels = labels_from_fields(fields)
    if PLAN_EXECUTE_LABEL in labels and is_in_progress_status(fields):
        return HANDOFF_EXECUTE
    if (
        PLAN_REFACTOR_LABEL in labels
        and PLAN_READY_LABEL not in labels
        and (is_todo_like_status(fields) or is_in_progress_status(fields))
    ):
        return HANDOFF_REFACTOR
    return None


def changelog_added_labels(changelog: Optional[dict]) -> Set[str]:
    """Labels that appeared in a Jira changelog labels item."""
    items = (changelog or {}).get("items") if isinstance(changelog, dict) else None
    if not items:
        return set()
    added: Set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("fieldId") or "").strip().lower()
        if field != "labels":
            continue
        old = normalize_label_set(str(item.get("fromString") or "").split())
        new = normalize_label_set(str(item.get("toString") or "").split())
        added |= new - old
    return added


def changelog_is_plan_handoff(changelog: Optional[dict]) -> bool:
    added = changelog_added_labels(changelog)
    return PLAN_EXECUTE_LABEL in added or PLAN_REFACTOR_LABEL in added


def handoff_from_changelog(changelog: Optional[dict]) -> Optional[str]:
    """Handoff implied by labels that *appeared* in the changelog.

    Jira issue_updated payloads sometimes still carry the *old* label set
    on ``issue.fields``. Prefer the changelog so a rename
    ``plan_ready`` → ``plan_execute`` is not missed.
    """
    added = changelog_added_labels(changelog)
    if PLAN_EXECUTE_LABEL in added:
        return HANDOFF_EXECUTE
    if PLAN_REFACTOR_LABEL in added:
        return HANDOFF_REFACTOR
    return None


def pat_user_needles(
    myself: Optional[dict],
    *,
    extra: Optional[Iterable[str]] = None,
) -> List[str]:
    """Identity fragments for the Jira user behind the PAT (``/myself``)."""
    raw: List[str] = []
    if isinstance(myself, dict):
        for key in ("name", "key", "displayName", "accountId", "emailAddress"):
            val = myself.get(key)
            if val:
                raw.append(str(val))
    for item in extra or []:
        if item:
            raw.append(str(item))
    return normalize_needles(raw)


def comment_text(comment: Optional[dict]) -> str:
    if not isinstance(comment, dict):
        return ""
    return jira_body_to_text(comment.get("body"))


def latest_comment_tagging_pat_user(
    comments: Optional[Iterable[dict]],
    *,
    myself: Optional[dict] = None,
    mention_tokens: Optional[Iterable[str]] = None,
    extra_needles: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Newest non-bot comment that @mentions the PAT user.

    Comments are assumed oldest-first (Jira ``/comment`` default).
    Re-adding ``plan_refactor`` without a new comment **reuses** this
    latest mention (intentional — not a stuck loop).
    """
    needles = pat_user_needles(myself, extra=extra_needles)
    tokens = [str(t).strip() for t in (mention_tokens or []) if str(t).strip()]
    rows = list(comments or [])
    for comment in reversed(rows):
        if not isinstance(comment, dict):
            continue
        body = comment_text(comment)
        if not body.strip():
            continue
        if comment_is_bot_output(body):
            continue
        # Do not skip by author identity. Cloud setups often use the
        # operator's own API token, so the human and the PAT user are
        # the same account. Bot-authored Yaver comments are already
        # filtered by comment_is_bot_output ("AI Agent —" / *Yaver*).
        if comment_mentions_target(
            body, mention_tokens=tokens, assignee_needles=needles
        ):
            return body
    return None
