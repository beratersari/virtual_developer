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


ASK_HANDOFF_REASON = "ignored /ask handoff"

_VSS_CHIP = re.compile(
    r"<a\s[^>]*data-vss-mention[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


def flatten_comment_text(note: str) -> str:
    """Plain text for command scans (Azure mention chips, leftover HTML)."""
    text = note or ""
    text = _VSS_CHIP.sub(lambda m: f" {m.group(1) or ''} ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text.replace("\xa0", " ").replace("&nbsp;", " ")


def _ask_handoff_names(bot_mentions: Iterable[str]) -> List[str]:
    """Configured bot names, longest first (display names before tokens)."""
    names: List[str] = []
    seen: set[str] = set()
    for raw in bot_mentions or []:
        text = str(raw or "").strip()
        if text.startswith("@"):
            text = text[1:].strip()
        candidates = [text] if text else []
        norm = normalize_mention(raw)
        if norm:
            candidates.append(norm)
        for name in candidates:
            key = name.lower()
            if key and key not in seen:
                seen.add(key)
                names.append(name)
    names.sort(key=len, reverse=True)
    return names


def _name_is_configured_bot(raw_name: str, names: List[str]) -> bool:
    token = (raw_name or "").strip()
    if token.startswith("@"):
        token = token[1:].strip()
    if not token:
        return False
    low = token.lower()
    first = low.split()[0] if low.split() else ""
    for name in names:
        key = name.lower()
        if low == key or first == key:
            return True
    return False


def note_is_ask_handoff(note: str, bot_mentions: Iterable[str]) -> bool:
    """True when the comment contains ``@bot /ask`` for a configured bot.

    That form is routed to another agent. Yaver must not start a job.
    ``/asking`` and ``/ask-review`` are not this command.
    """
    names = _ask_handoff_names(bot_mentions)
    if not names:
        return False
    raw = note or ""
    for match in _VSS_CHIP.finditer(raw):
        after = raw[match.end() :]
        if re.match(r"\s*/ask(?![A-Za-z0-9_-])", after, flags=re.IGNORECASE):
            if _name_is_configured_bot(match.group(1) or "", names):
                return True
    text = flatten_comment_text(raw)
    if not text:
        return False
    for name in names:
        if not name:
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(name)}\s*/ask(?![A-Za-z0-9_-])",
            text,
            flags=re.IGNORECASE,
        ):
            return True
    return False
