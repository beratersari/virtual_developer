"""Shared Jira trigger matching (poller + webhook).

Assignment and mention checks must stay aligned with Server/DC 9.4 (``name`` /
``key`` / wiki ``[~user]``) and Cloud (``accountId`` / ADF mentions).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Optional

from src.brand import COMMENT_PREFIX

# Jira Server/DC wiki mention: [~fred] or [~accountid:557058:xxxx]
_WIKI_MENTION = re.compile(r"\[~([^\]]+)\]")
# @token used in TRIGGER_MENTIONS and free-text Cloud comments
_AT_TOKEN = re.compile(r"@([A-Za-z0-9._\-]+)")


def normalize_needles(raw: Iterable[str]) -> List[str]:
    """Lowercased, stripped fragments (empty dropped)."""
    out: List[str] = []
    seen: set[str] = set()
    for item in raw or []:
        n = str(item or "").strip().lower()
        if n.startswith("@"):
            n = n[1:].strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def identity_matches_bot(
    *candidates: Any,
    needles: Optional[Iterable[str]] = None,
) -> bool:
    """True when any candidate contains a configured bot name fragment."""
    parts = normalize_needles(needles or [])
    if not parts:
        return False
    values: List[str] = []
    for c in candidates:
        if c is None:
            continue
        text = str(c).strip().lower()
        if text and text not in {"none", "null", "unassigned"}:
            values.append(text)
    if not values:
        return False
    for val in values:
        for needle in parts:
            if needle and needle in val:
                return True
    return False


def assignee_looks_like_bot(
    assignee: Optional[dict],
    needles: Optional[Iterable[str]] = None,
) -> bool:
    """True when issue assignee matches ``TRIGGER_ASSIGNEE_NAMES`` fragments."""
    if not assignee or not isinstance(assignee, dict):
        return False
    return identity_matches_bot(
        assignee.get("displayName"),
        assignee.get("name"),
        assignee.get("key"),
        assignee.get("accountId"),
        assignee.get("emailAddress"),
        needles=needles,
    )


def author_looks_like_bot(
    author: Optional[dict],
    needles: Optional[Iterable[str]] = None,
) -> bool:
    """True when a comment author is the configured bot (loop guard)."""
    return assignee_looks_like_bot(author, needles=needles)


def jira_body_to_text(body: Any) -> str:
    """Plain text from a Server wiki string or Cloud ADF document."""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        parts: List[str] = []

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            ntype = node.get("type")
            if ntype == "text":
                parts.append(str(node.get("text") or ""))
            elif ntype == "mention":
                attrs = node.get("attrs") or {}
                text = str(attrs.get("text") or "").strip()
                ident = str(attrs.get("id") or attrs.get("accessLevel") or "").strip()
                if text:
                    parts.append(text if text.startswith("@") else f"@{text}")
                elif ident:
                    parts.append(f"[~{ident}]")
            elif ntype == "hardBreak":
                parts.append("\n")
            elif ntype == "emoji":
                attrs = node.get("attrs") or {}
                parts.append(str(attrs.get("shortName") or attrs.get("text") or ""))
            for child in node.get("content") or []:
                walk(child)
            if ntype in {"paragraph", "heading", "blockquote", "listItem"}:
                parts.append("\n")

        walk(body)
        return "".join(parts)
    return str(body)


def comment_is_bot_output(body: str) -> bool:
    """True for comments we posted (progress / ERROR / ack) — never re-trigger."""
    text = (body or "").lstrip()
    if not text:
        return False
    prefix = (COMMENT_PREFIX or "").strip()
    if prefix and text.startswith(prefix):
        return True
    # Reporter also uses "AI Agent —" on some older paths
    lower = text.lower()
    return lower.startswith("ai agent") or lower.startswith("*yaver*")


def wiki_mention_idents(text: str) -> List[str]:
    """Usernames / accountIds extracted from ``[~user]`` wiki mentions."""
    out: List[str] = []
    for raw in _WIKI_MENTION.findall(text or ""):
        ident = (raw or "").strip()
        if ident.lower().startswith("accountid:"):
            ident = ident.split(":", 1)[1].strip()
        if ident:
            out.append(ident)
    return out


def comment_mentions_target(
    body: str,
    *,
    mention_tokens: Optional[Iterable[str]] = None,
    assignee_needles: Optional[Iterable[str]] = None,
) -> bool:
    """True when the comment tags the configured bot.

    Accepts:
    * ``TRIGGER_MENTIONS`` tokens (``@DevBot``)
    * wiki ``[~username]`` / ``[~accountid:…]`` matching assignee fragments
    * ADF mention text already flattened by ``jira_body_to_text``
    """
    text = body or ""
    if not text.strip():
        return False
    lower = text.lower()
    mentions = [str(m or "").strip() for m in (mention_tokens or []) if str(m or "").strip()]
    for token in mentions:
        if token.lower() in lower:
            return True
    needles = normalize_needles(
        list(assignee_needles or []) + [t.lstrip("@") for t in mentions]
    )
    if not needles:
        return False
    for ident in wiki_mention_idents(text):
        if identity_matches_bot(ident, needles=needles):
            return True
    for at in _AT_TOKEN.findall(text):
        if identity_matches_bot(at, needles=needles):
            return True
    return False


def changelog_assigned_to_bot(
    changelog: Optional[dict],
    needles: Optional[Iterable[str]] = None,
) -> bool:
    """True only when assignee **became** the bot (not unassigned / not removed)."""
    items = (changelog or {}).get("items") if isinstance(changelog, dict) else None
    if not items:
        return False
    parts = normalize_needles(needles or [])
    if not parts:
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or item.get("fieldId") or "").strip().lower()
        if field != "assignee":
            continue
        to_id = item.get("to")
        to_str = item.get("toString")
        # Removal / unassigned: Jira Server sends to=null, toString=null
        if (to_id is None or to_id == "") and (to_str is None or str(to_str).strip() == ""):
            return False
        if identity_matches_bot(to_id, to_str, needles=parts):
            return True
        return False
    return False
