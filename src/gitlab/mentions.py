"""Detect bot @mentions in GitLab MR comments (CE and EE)."""

from __future__ import annotations

import re
from typing import Iterable, List, Sequence


def normalize_mention(raw: str) -> str:
    """``@Berat_AI`` / ``berat_ai`` → ``berat_ai`` (lowercase, no leading @)."""
    text = (raw or "").strip()
    if text.startswith("@"):
        text = text[1:]
    # GitLab usernames: letters, digits, _, -, .
    text = re.sub(r"[^A-Za-z0-9_.-].*$", "", text)
    return text.lower()


def parse_mention_list(raw: str | Sequence[str] | None) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    out: List[str] = []
    seen: set[str] = set()
    for p in parts:
        n = normalize_mention(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def mentioned_usernames(note: str) -> List[str]:
    """Usernames referenced as ``@name`` in *note* (GitLab mention syntax)."""
    if not note:
        return []
    found: List[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_.-])@([A-Za-z0-9_.-]+)", note):
        name = match.group(1).lower()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def note_mentions_bot(note: str, bot_mentions: Iterable[str]) -> bool:
    bots = {normalize_mention(x) for x in bot_mentions if normalize_mention(x)}
    if not bots:
        return False
    return bool(bots.intersection(mentioned_usernames(note)))


def strip_bot_mentions(note: str, bot_mentions: Iterable[str]) -> str:
    """Remove configured @bot tokens from the comment body."""
    text = note or ""
    for name in parse_mention_list(list(bot_mentions)):
        text = re.sub(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(name)}\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return re.sub(r"[ \t]{2,}", " ", text).strip()
