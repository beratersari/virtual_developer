"""Product identity shown to operators (dashboard, Jira, CLI)."""

from typing import Any, Optional

PRODUCT_NAME = "Yaver"
PRODUCT_TAGLINE = "the aide"
# Jira / GitLab comment lead-in (italic markdown).
COMMENT_PREFIX = f"*{PRODUCT_NAME}*"
# Creasy-style usage note. Marker must not contain @mentions.
USAGE_HEADING = f"**{PRODUCT_NAME} — how to run a command**"
USAGE_MARKER = "<!-- yaver-usage -->"


def product_version() -> str:
    from src import __version__

    return (__version__ or "0.0.0-dev").strip()


def is_yaver_reply(body: str) -> bool:
    """True for notes we posted (usage, job reply, leftover *Yaver* prefix)."""
    text = body or ""
    if USAGE_MARKER in text or USAGE_HEADING in text:
        return True
    lead = text.lstrip()
    return lead.startswith("*Yaver") or lead.startswith("**Yaver")


def format_reply_header(
    kind: str,
    *,
    model: str = "",
    job_id: str = "",
) -> str:
    """Creasy-style first line: **Yaver {ver} — Kind** · `model` · `job`."""
    title = (kind or "Update").strip() or "Update"
    line = f"**{PRODUCT_NAME} {product_version()} — {title}**"
    extras = []
    mid = (model or "").strip()
    jid = (job_id or "").strip()
    if mid:
        extras.append(f"`{mid}`")
    if jid:
        extras.append(f"`{jid}`")
    if extras:
        return line + " · " + " · ".join(extras)
    return line


def wrap_operator_reply(
    kind: str,
    body: str,
    *,
    model: str = "",
    job_id: str = "",
    state: Any = None,
) -> str:
    """Prefix a forge/Jira reply with the Creasy header."""
    mid = (model or "").strip()
    jid = (job_id or "").strip()
    if state is not None:
        meta = getattr(state, "metadata", None) or {}
        if not jid:
            jid = str(meta.get("current_job_id") or "").strip()
            if not jid:
                ids = meta.get("job_ids") or []
                if ids:
                    jid = str(ids[-1] or "").strip()
        if not mid:
            mid = str(meta.get("model") or "").strip()
    text = (body or "").strip()
    return f"{format_reply_header(kind, model=mid, job_id=jid)}\n\n{text}\n"


def format_execute_usage_note(bot_name: str = "yaver") -> str:
    """Thread reply when the bot is mentioned without ``/yaver``.

    Do not put ``@name`` / ``@mention`` in this text — those tokens start
    the other agent (Creasy). Same shape as Creasy's usage note.
    """
    _ = bot_name
    return (
        f"{USAGE_MARKER}\n"
        f"{USAGE_HEADING}\n\n"
        "I only run `/yaver`. Mention me and put `/yaver` in the same comment.\n\n"
        "- `/yaver <prompt>` — start a job on this thread\n"
    )
