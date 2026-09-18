"""Product identity shown to operators (dashboard, Jira, CLI)."""

from typing import Any, Optional

PRODUCT_NAME = "Yaver"
PRODUCT_TAGLINE = "the aide"
# Jira / GitLab comment lead-in (italic markdown).
COMMENT_PREFIX = f"*{PRODUCT_NAME}*"
# Creasy-style usage note. Marker must not contain @mentions.
from src.operator_copy import USAGE_HEADING_EN, USAGE_HEADING_TR

USAGE_HEADING = USAGE_HEADING_TR
USAGE_HEADING_LEGACY = USAGE_HEADING_EN
USAGE_MARKER = "<!-- yaver-usage -->"


def product_version() -> str:
    from src import __version__

    return (__version__ or "0.0.0-dev").strip()


def is_yaver_reply(body: str) -> bool:
    """True for notes we posted (usage, job reply, leftover *Yaver* prefix)."""
    text = body or ""
    if (
        USAGE_MARKER in text
        or USAGE_HEADING in text
        or USAGE_HEADING_LEGACY in text
    ):
        return True
    lead = text.lstrip()
    return (
        lead.startswith("*Yaver")
        or lead.startswith("**Yaver")
        or lead.startswith("<strong>Yaver")
        or "<strong>Yaver" in lead[:120]
    )


def _ids_from_state(state: Any) -> tuple[str, str]:
    """``(model, job_id)`` from issue metadata (live pointer, then history)."""
    if state is None:
        return "", ""
    meta = getattr(state, "metadata", None) or {}
    if not isinstance(meta, dict):
        return "", ""
    mid = str(meta.get("model") or "").strip()
    jid = str(meta.get("current_job_id") or "").strip()
    if not jid:
        ids = meta.get("job_ids") or []
        if isinstance(ids, (list, tuple)) and ids:
            jid = str(ids[-1] or "").strip()
    return mid, jid


def resolve_reply_ids(
    state: Any = None,
    *,
    model: str = "",
    job_id: str = "",
) -> tuple[str, str]:
    """Same fields Creasy reads off the JobRecord: model + job_id."""
    mid = (model or "").strip()
    jid = (job_id or "").strip()
    if not jid:
        try:
            from src.log_context import get_job_id

            jid = (get_job_id() or "").strip()
        except Exception:
            jid = ""
    sm, sj = _ids_from_state(state)
    if not mid:
        mid = sm
    if not jid:
        jid = sj
    if mid.lower() in {"unknown", "-"}:
        mid = ""
    if jid.lower() in {"unknown", "-"}:
        jid = ""
    return mid, jid


def format_reply_header(
    kind: str,
    *,
    model: str = "",
    job_id: str = "",
) -> str:
    """``**Yaver {ver} — Kind**`` plus model/job only when they are real."""
    from src.operator_copy import header_kind

    title = header_kind(kind)
    mid = (model or "").strip()
    jid = (job_id or "").strip()
    if mid.lower() in {"unknown", "-"}:
        mid = ""
    if jid.lower() in {"unknown", "-"}:
        jid = ""
    line = f"**{PRODUCT_NAME} {product_version()} — {title}**"
    if mid:
        line += f" · `{mid}`"
    if jid:
        line += f" · `{jid}`"
    return line


def wrap_operator_reply(
    kind: str,
    body: str,
    *,
    model: str = "",
    job_id: str = "",
    state: Any = None,
) -> str:
    """Prefix a forge/Jira reply with the Creasy header (always includes job_id)."""
    mid, jid = resolve_reply_ids(state, model=model, job_id=job_id)
    text = (body or "").strip()
    return f"{format_reply_header(kind, model=mid, job_id=jid)}\n\n{text}\n"


def format_execute_usage_note(bot_name: str = "yaver") -> str:
    """Thread reply when the bot is mentioned without ``/yaver``.

    Do not put ``@name`` / ``@mention`` in this text — those tokens start
    the other agent (Creasy). Same shape as Creasy's usage note.
    """
    from src.operator_copy import USAGE_MR_PR_WITH_REVIEW

    _ = bot_name
    return f"{USAGE_MARKER}\n{USAGE_HEADING}\n\n{USAGE_MR_PR_WITH_REVIEW}"
