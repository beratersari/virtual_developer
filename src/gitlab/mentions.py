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
        # DOMAIN\user / user@host → account tail (Azure uniqueName).
        n = identity_key(p) or normalize_mention(p)
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


def identity_key(raw: str) -> str:
    """Account token for self-mention checks (last segment of DOMAIN\\user)."""
    text = (raw or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    if not text:
        return ""
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    elif "/" in text:
        text = text.rsplit("/", 1)[-1]
    if "@" in text:
        text = text.split("@", 1)[0]
    return normalize_mention(text)


def author_is_configured_bot(
    author_values: Iterable[str], bot_names: Iterable[str]
) -> bool:
    """True when the comment author is a configured trigger user.

    Uses the account tail of ``DOMAIN\\user`` / ``user@host`` so a domain
    prefix cannot mark every teammate as the bot.
    """
    bots = {identity_key(x) for x in bot_names or [] if identity_key(x)}
    if not bots:
        return False
    for raw in author_values or []:
        key = identity_key(raw)
        if key and key in bots:
            return True
    return False


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
REVIEW_HANDOFF_REASON = "ignored /review handoff"
EXECUTE_MISSING_REASON = "mention without /yaver"
EXECUTE_COMMAND = "yaver"

_VSS_CHIP = re.compile(
    r"<a\s[^>]*data-vss-mention[^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_GUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_VSS_MENTION_GUID = re.compile(
    r'data-vss-mention\s*=\s*["\'][^"\']*?(?:version\s*:\s*[\d.]+,\s*)?('
    + _GUID.pattern
    + r")",
    re.IGNORECASE,
)
_MD_MENTION_GUID = re.compile(r"@<(" + _GUID.pattern + r")>", re.IGNORECASE)
_PLAIN_MENTION_GUID = re.compile(
    r"(?<![A-Za-z0-9_.-])@(" + _GUID.pattern + r")(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_VSS_INNER = re.compile(
    r'data-vss-mention\s*=\s*["\'][^"\']*["\'][^>]*>([^<]+)',
    re.IGNORECASE,
)


def normalize_guid(raw: str) -> str:
    text = (raw or "").strip().strip("<>").lstrip("@").strip()
    match = _GUID.fullmatch(text)
    return match.group(0).lower() if match else ""


def extract_mention_guids(note: str) -> List[str]:
    """Azure user ids from ``data-vss-mention`` chips and ``@<guid>``."""
    if not note:
        return []
    found: List[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        gid = normalize_guid(raw)
        if gid and gid not in seen:
            seen.add(gid)
            found.append(gid)

    for pat in (_VSS_MENTION_GUID, _MD_MENTION_GUID, _PLAIN_MENTION_GUID):
        for match in pat.finditer(note):
            _add(match.group(1))
    for match in _VSS_INNER.finditer(note):
        _add(match.group(1) or "")
    return found


def flatten_comment_text(note: str) -> str:
    """Plain text for command scans (Azure mention chips, leftover HTML)."""
    text = note or ""
    # Markdown @<guid> is not HTML — do not strip it as a tag.
    text = _MD_MENTION_GUID.sub(lambda m: f" @{m.group(1)} ", text)
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
        ident = identity_key(raw)
        if ident:
            candidates.append(ident)
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


def note_has_slash_command(
    note: str, bot_mentions: Iterable[str], command: str
) -> bool:
    """True when the comment contains ``@bot /command`` for a configured bot.

    Command match is a word boundary so ``/asking`` is not ``/ask``.
    """
    cmd = (command or "").strip().lstrip("/")
    if not cmd:
        return False
    names = _ask_handoff_names(bot_mentions)
    if not names:
        return False
    raw = note or ""
    cmd_re = rf"/{re.escape(cmd)}(?![A-Za-z0-9_-])"
    configured_guids = {normalize_guid(n) for n in names}
    configured_guids.discard("")
    for match in _VSS_CHIP.finditer(raw):
        # TFS often inserts &nbsp; or a wrapper span between the chip
        # and /yaver. Flatten those. Another @mention must stay a miss.
        after = flatten_comment_text(raw[match.end() :])
        if re.match(rf"\s*{cmd_re}", after, flags=re.IGNORECASE):
            chip_guids = set(extract_mention_guids(match.group(0) or ""))
            if _name_is_configured_bot(match.group(1) or "", names) or (
                configured_guids and configured_guids.intersection(chip_guids)
            ):
                return True
    text = flatten_comment_text(raw)
    if not text:
        return False
    for name in names:
        if not name:
            continue
        if re.search(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(name)}\s*{cmd_re}",
            text,
            flags=re.IGNORECASE,
        ):
            return True
        # "@Yaver Bot /yaver" when configured as yaver. Extra words must
        # look like a display-name tail (capitalized). "@bot please /cmd"
        # and "@bot @alice /cmd" stay misses.
        tail_hit = re.search(
            rf"(?<![A-Za-z0-9_.-])@{re.escape(name)}((?:\s+\S+)*)\s*{cmd_re}",
            text,
            flags=re.IGNORECASE,
        )
        if tail_hit:
            words = (tail_hit.group(1) or "").split()
            if words and all(
                w[:1].isupper() and not w.startswith("@") for w in words
            ):
                return True
        gid = normalize_guid(name)
        if gid:
            guid_cmd = "@<?" + re.escape(gid) + r">?\s*" + cmd_re
            if re.search(guid_cmd, text, flags=re.IGNORECASE):
                return True
    return False


def note_is_ask_handoff(note: str, bot_mentions: Iterable[str]) -> bool:
    """True when the comment contains ``@bot /ask`` for a configured bot.

    That form is routed to another agent. Yaver must not start a job.
    ``/asking`` and ``/ask-review`` are not this command.
    """
    return note_has_slash_command(note, bot_mentions, "ask")


def note_is_review_handoff(note: str, bot_mentions: Iterable[str]) -> bool:
    """True when the comment contains ``@bot /review`` for a configured bot.

    Creasy owns ``/review``. Yaver must not start a job or post a usage note.
    ``/reviewing`` and ``/review-in-detail`` are not this command.
    """
    return note_has_slash_command(note, bot_mentions, "review")


def note_is_other_agent_handoff(note: str, bot_mentions: Iterable[str]) -> bool:
    """True for ``@bot /ask`` or ``@bot /review`` (silent; other agent)."""
    return note_is_ask_handoff(note, bot_mentions) or note_is_review_handoff(
        note, bot_mentions
    )


def other_agent_handoff_reason(note: str, bot_mentions: Iterable[str]) -> str:
    if note_is_review_handoff(note, bot_mentions):
        return REVIEW_HANDOFF_REASON
    return ASK_HANDOFF_REASON


def note_is_execute_command(note: str, bot_mentions: Iterable[str]) -> bool:
    """True when the comment contains ``@bot /yaver`` for a configured bot."""
    return note_has_slash_command(note, bot_mentions, EXECUTE_COMMAND)


def strip_slash_command(text: str, command: str) -> str:
    """Remove ``/command`` tokens (word-boundary) from a prompt body."""
    cmd = (command or "").strip().lstrip("/")
    if not cmd:
        return (text or "").strip()
    out = re.sub(
        rf"(?<![A-Za-z0-9_-])/{re.escape(cmd)}(?![A-Za-z0-9_-])",
        "",
        text or "",
        flags=re.IGNORECASE,
    )
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def format_execute_usage_note(bot_name: str = "yaver") -> str:
    """Thread reply when the bot is mentioned without ``/yaver``."""
    from src.brand import format_execute_usage_note as _format

    return _format(bot_name)


def is_usage_note(body: str) -> bool:
    """True for a Yaver usage note (so its examples do not start a job)."""
    from src.brand import is_yaver_reply

    return is_yaver_reply(body)
