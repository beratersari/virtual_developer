"""Detect bot @mentions in Azure DevOps PR comments.

Azure DevOps Server mentions are ``@name`` in plain text (same as GitLab)
or an HTML identity chip::

    <a href="#" data-vss-mention="version:2.0,{guid}">@Display Name</a>

Configured bot names match either form. GitLab mention helpers are reused
for the ``@token`` path so both forges share one username list syntax.
"""

from __future__ import annotations

import re
from typing import Iterable, List

from src.gitlab.mentions import (
    ASK_HANDOFF_REASON,
    EXECUTE_MISSING_REASON,
    author_is_configured_bot,
    flatten_comment_text,
    format_execute_usage_note,
    identity_key,
    mentioned_usernames,
    normalize_mention,
    note_is_ask_handoff,
    note_is_execute_command,
    parse_mention_list,
    strip_bot_mentions,
    strip_slash_command,
)

_VSS_MENTION = re.compile(
    r'data-vss-mention\s*=\s*["\'][^"\']*["\'][^>]*>([^<]+)',
    re.IGNORECASE,
)
_HTML_AT = re.compile(
    r">\s*@([^<]+?)\s*<",
    re.IGNORECASE,
)


def html_mention_names(note: str) -> List[str]:
    """Display names / @labels extracted from Azure identity HTML."""
    if not note:
        return []
    found: List[str] = []
    seen: set[str] = set()
    for pat in (_VSS_MENTION, _HTML_AT):
        for match in pat.finditer(note):
            name = normalize_mention(match.group(1) or "")
            if name and name not in seen:
                seen.add(name)
                found.append(name)
    return found


def mention_scan(note: str, bot_mentions: Iterable[str]) -> dict:
    """Configured bots vs names found in the comment (for reject logs)."""
    bots = sorted(
        {normalize_mention(x) for x in bot_mentions if normalize_mention(x)}
    )
    extracted = sorted(
        set(mentioned_usernames(note)) | set(html_mention_names(note))
    )
    matched = bool(set(bots) & set(extracted))
    return {
        "configured": bots,
        "extracted": extracted,
        "matched": matched,
    }


def note_mentions_bot(note: str, bot_mentions: Iterable[str]) -> bool:
    bots = {normalize_mention(x) for x in bot_mentions if normalize_mention(x)}
    if not bots:
        return False
    names = set(mentioned_usernames(note))
    names.update(html_mention_names(note))
    # Azure display names can contain spaces; also match the raw configured
    # token as a case-insensitive substring after @ (plain or HTML).
    if bots.intersection(names):
        return True
    text = note or ""
    for bot in bots:
        if not bot:
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(bot)}\b",
            text,
            flags=re.IGNORECASE,
        ):
            return True
        # Display-name mention: "@Yaver Bot" when configured as "Yaver Bot"
        if " " in bot or "\\" in bot:
            if re.search(
                rf"(?<![A-Za-z0-9_.-])@{re.escape(bot)}(?![A-Za-z0-9_.-])",
                text,
                flags=re.IGNORECASE,
            ):
                return True
    return False


def strip_azure_bot_mentions(note: str, bot_mentions: Iterable[str]) -> str:
    """Remove configured @bot tokens and Azure mention chips from the body."""
    text = note or ""
    text = re.sub(
        r'<a\s[^>]*data-vss-mention[^>]*>\s*@?[^<]*</a>',
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = strip_bot_mentions(text, bot_mentions)
    for name in parse_mention_list(list(bot_mentions)):
        if " " in name or "\\" in name:
            text = re.sub(
                rf"(?<![A-Za-z0-9_.-])@{re.escape(name)}(?![A-Za-z0-9_.-])",
                "",
                text,
                flags=re.IGNORECASE,
            )
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


__all__ = [
    "ASK_HANDOFF_REASON",
    "EXECUTE_MISSING_REASON",
    "author_is_configured_bot",
    "flatten_comment_text",
    "format_execute_usage_note",
    "html_mention_names",
    "identity_key",
    "mention_scan",
    "normalize_mention",
    "note_is_ask_handoff",
    "note_is_execute_command",
    "note_mentions_bot",
    "parse_mention_list",
    "strip_azure_bot_mentions",
    "strip_bot_mentions",
    "strip_slash_command",
]
