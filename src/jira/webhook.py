"""Parse Jira webhooks (Server/DC 9.4 + Cloud) into a single intake event.

Jira Server 9.4.10 has no HMAC secret on admin webhooks. Operators put a
shared token in the URL (``/webhooks/jira?token=…``). Cloud may send
``X-Hub-Signature: sha256=…``. Both are accepted.

Triggers (anything else is ignored — prevents comment/transition loops):

* **Assignment to the bot** — changelog ``assignee.to`` matches
  ``TRIGGER_ASSIGNEE_NAMES``. Unassign / assign-*away* is ignored.
* **Issue created already assigned to the bot**.
* **Comment that mentions the bot** — ``TRIGGER_MENTIONS`` or wiki
  ``[~user]``. Comments authored by the bot, or our own ``*Yaver*`` posts,
  are ignored.

Accepted events become a ``process_event`` envelope with
``webhook_intake=True`` so the processor treats them as an explicit start
(not poller To Do noise).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs

from src.jira.triggers import (
    assignee_looks_like_bot,
    author_looks_like_bot,
    changelog_assigned_to_bot,
    comment_is_bot_output,
    comment_mentions_target,
    jira_body_to_text,
)
from src.logger import logger

INTAKE_POLL = "poll"
INTAKE_WEBHOOK = "webhook"

_ASSIGNMENT_EVENTS = frozenset({"jira:issue_updated", "jira:issue_created"})
_COMMENT_EVENTS = frozenset(
    {
        "comment_created",
        "jira:issue_commented",
        "comment_created_notification",
    }
)


@dataclass
class JiraWebhookDecision:
    accepted: bool
    reason: str
    event: Optional[Dict[str, Any]] = None
    http_status: int = 200
    trigger: str = ""
    event_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def normalize_intake_mode(raw: Any) -> str:
    """``poll`` (default) or ``webhook``."""
    text = str(raw or "").strip().lower()
    if text in {"webhook", "webhooks", "hook", "push", "http"}:
        return INTAKE_WEBHOOK
    return INTAKE_POLL


def validate_webhook_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time-ish compare. Empty expected → accept (dev)."""
    want = (expected or "").strip()
    if not want:
        return True
    got = (provided or "").strip()
    if len(got) != len(want):
        return False
    acc = 0
    for a, b in zip(got.encode("utf-8"), want.encode("utf-8")):
        acc |= a ^ b
    return acc == 0


def validate_hub_signature(raw_body: bytes, header_value: str, secret: str) -> bool:
    """Cloud-style ``X-Hub-Signature: sha256=<hex>``."""
    want = (secret or "").strip()
    hdr = (header_value or "").strip()
    if not want or not hdr:
        return False
    if hdr.lower().startswith("sha256="):
        got = hdr.split("=", 1)[1].strip()
    else:
        got = hdr
    digest = hmac.new(want.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    if len(got) != len(digest):
        return False
    return hmac.compare_digest(got.lower(), digest.lower())


def _header_map(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _query_map(query: Optional[Any]) -> Dict[str, str]:
    if query is None:
        return {}
    if isinstance(query, dict):
        out: Dict[str, str] = {}
        for k, v in query.items():
            if isinstance(v, (list, tuple)):
                out[str(k).lower()] = str(v[0] if v else "")
            else:
                out[str(k).lower()] = str(v)
        return out
    raw = str(query).lstrip("?")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {str(k).lower(): (vals[0] if vals else "") for k, vals in parsed.items()}


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _issue_key(issue: Dict[str, Any]) -> str:
    return str(issue.get("key") or "").strip()


def _normalize_issue_text_fields(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten ADF description/summary so ``{params}`` parsing works."""
    if not issue:
        return issue
    out = dict(issue)
    fields = dict(_as_dict(out.get("fields")))
    desc = fields.get("description")
    if desc is not None and not isinstance(desc, str):
        fields["description"] = jira_body_to_text(desc)
    summary = fields.get("summary")
    if summary is not None and not isinstance(summary, str):
        fields["summary"] = jira_body_to_text(summary)
    out["fields"] = fields
    return out


def _comment_id(comment: Dict[str, Any]) -> str:
    cid = comment.get("id") or comment.get("comment_id")
    return str(cid).strip() if cid is not None else ""


def _changelog_id(changelog: Dict[str, Any]) -> str:
    cid = changelog.get("id")
    return str(cid).strip() if cid is not None else ""


def extract_token(
    *,
    headers: Optional[Dict[str, str]] = None,
    query: Optional[Any] = None,
) -> str:
    """Secret from query ``token``/``secret``, Bearer, or ``X-Webhook-Token``."""
    header_map = _header_map(headers)
    q = _query_map(query)
    for key in ("token", "secret"):
        if q.get(key):
            return q[key]
    auth = header_map.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (
        header_map.get("x-webhook-token")
        or header_map.get("x-jira-webhook-token")
        or ""
    )


def authenticate_jira_webhook(
    *,
    secret: str,
    headers: Optional[Dict[str, str]] = None,
    query: Optional[Any] = None,
    raw_body: bytes = b"",
) -> Optional[str]:
    """Return an error reason, or None when the request is allowed.

    Empty ``secret`` is rejected in webhook mode (dashboard binds 0.0.0.0).
    """
    want = (secret or "").strip()
    if not want:
        return "webhook secret required"
    header_map = _header_map(headers)
    sig = header_map.get("x-hub-signature") or header_map.get("x-hub-signature-256") or ""
    if sig:
        if validate_hub_signature(raw_body, sig, want):
            return None
        return "invalid webhook signature"
    token = extract_token(headers=headers, query=query)
    if validate_webhook_token(token, want):
        return None
    return "invalid webhook token"


def _build_event(
    *,
    webhook_event: str,
    issue: Dict[str, Any],
    trigger: str,
    event_id: str,
    comment: Optional[Dict[str, Any]] = None,
    changelog: Optional[Dict[str, Any]] = None,
    timestamp: Any = None,
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    issue = _normalize_issue_text_fields(issue)
    envelope: Dict[str, Any] = {
        "webhookEvent": webhook_event,
        "webhook_intake": True,
        "webhook_trigger": trigger,
        "jira_event_id": event_id,
        "issue": issue,
        "timestamp": timestamp,
    }
    if comment:
        envelope["comment"] = comment
    if changelog:
        envelope["changelog"] = changelog
    if raw:
        envelope["_raw_webhook_event"] = str(raw.get("webhookEvent") or webhook_event)
    return envelope


def decide_jira_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    query: Optional[Any] = None,
    raw_body: bytes = b"",
    enabled: bool = True,
    secret: str = "",
    intake_mode: str = INTAKE_WEBHOOK,
    assignee_needles: Optional[List[str]] = None,
    mention_tokens: Optional[List[str]] = None,
) -> JiraWebhookDecision:
    """Accept only assignment-to-bot or mention-of-bot events."""
    mode = normalize_intake_mode(intake_mode)
    if not enabled or mode != INTAKE_WEBHOOK:
        return JiraWebhookDecision(False, "jira webhook intake disabled")

    auth_err = authenticate_jira_webhook(
        secret=secret, headers=headers, query=query, raw_body=raw_body
    )
    if auth_err:
        return JiraWebhookDecision(False, auth_err, http_status=401)

    data = payload if isinstance(payload, dict) else {}
    if not data and raw_body:
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
            data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            data = {}
    if not data:
        return JiraWebhookDecision(False, "invalid json", http_status=400)

    header_map = _header_map(headers)
    event_name = (
        str(data.get("webhookEvent") or data.get("webhook_event") or "").strip()
        or header_map.get("x-atlassian-webhook-identifier")
        or header_map.get("x-event-key")
        or ""
    )
    event_name_l = event_name.lower()
    issue_event = str(data.get("issue_event_type_name") or "").strip().lower()

    issue = _as_dict(data.get("issue"))
    key = _issue_key(issue)
    comment = _as_dict(data.get("comment"))
    changelog = _as_dict(data.get("changelog"))
    needles = list(assignee_needles or [])
    mentions = list(mention_tokens or [])

    # --- comments (created only; updates would loop on our own edits) ---
    is_comment = event_name_l in _COMMENT_EVENTS or issue_event in {
        "issue_commented",
        "issue_comment_added",
    }
    if is_comment or (comment and event_name_l not in _ASSIGNMENT_EVENTS):
        if not comment:
            return JiraWebhookDecision(False, "comment event missing comment")
        body = jira_body_to_text(comment.get("body"))
        if not key:
            return JiraWebhookDecision(False, "comment event missing issue key")
        if comment_is_bot_output(body):
            return JiraWebhookDecision(False, "ignored bot reply")
        author = _as_dict(comment.get("author") or comment.get("updateAuthor"))
        if author_looks_like_bot(author, needles=needles):
            return JiraWebhookDecision(False, "ignored comment from bot user")
        if not comment_mentions_target(
            body, mention_tokens=mentions, assignee_needles=needles
        ):
            return JiraWebhookDecision(False, "bot not mentioned")
        cid = _comment_id(comment) or f"{key}:body"
        event_id = f"comment:{cid}"
        event = _build_event(
            webhook_event="jira:issue_updated",
            issue=issue,
            trigger="mention",
            event_id=event_id,
            comment={**comment, "body": body},
            timestamp=data.get("timestamp"),
            raw=data,
        )
        logger.info(f"Jira webhook mention accepted: {key} comment={cid}")
        return JiraWebhookDecision(
            True, "accepted", event=event, trigger="mention", event_id=event_id, raw=data
        )

    # --- created already assigned to bot ---
    if event_name_l == "jira:issue_created" or issue_event == "issue_created":
        if not key:
            return JiraWebhookDecision(False, "created event missing issue key")
        fields = _as_dict(issue.get("fields"))
        if not assignee_looks_like_bot(fields.get("assignee"), needles=needles):
            return JiraWebhookDecision(False, "created issue not assigned to bot")
        actor = _as_dict(data.get("user") or data.get("account"))
        if actor and author_looks_like_bot(actor, needles=needles):
            # Dashboard/schedule create uses the PAT user — do not auto-start.
            return JiraWebhookDecision(False, "ignored bot-created issue")
        event_id = f"created:{key}"
        event = _build_event(
            webhook_event="jira:issue_created",
            issue=issue,
            trigger="created_assigned",
            event_id=event_id,
            timestamp=data.get("timestamp"),
            raw=data,
        )
        logger.info(f"Jira webhook created+assigned accepted: {key}")
        return JiraWebhookDecision(
            True,
            "accepted",
            event=event,
            trigger="created_assigned",
            event_id=event_id,
            raw=data,
        )

    # --- assignment (issue_updated changelog) ---
    if event_name_l == "jira:issue_updated" or issue_event in {
        "issue_assigned",
        "issue_updated",
        "issue_generic",
    }:
        if not key:
            return JiraWebhookDecision(False, "update event missing issue key")
        if changelog_assigned_to_bot(changelog, needles=needles):
            actor = _as_dict(data.get("user") or data.get("account"))
            if actor and author_looks_like_bot(actor, needles=needles):
                # We assign the PAT user on schedule/poll — do not re-intake.
                return JiraWebhookDecision(False, "ignored self-assignment")
            clid = _changelog_id(changelog) or key
            event_id = f"assignee:{clid}"
            event = _build_event(
                webhook_event="jira:issue_updated",
                issue=issue,
                trigger="assignment",
                event_id=event_id,
                changelog=changelog,
                timestamp=data.get("timestamp"),
                raw=data,
            )
            logger.info(f"Jira webhook assignment accepted: {key} changelog={clid}")
            return JiraWebhookDecision(
                True,
                "accepted",
                event=event,
                trigger="assignment",
                event_id=event_id,
                raw=data,
            )
        # Some Server configs omit comment_created and only send issue_updated
        # with a comment body (no assignee changelog). Treat as mention.
        if comment:
            body = jira_body_to_text(comment.get("body"))
            if (
                body
                and not comment_is_bot_output(body)
                and not author_looks_like_bot(
                    _as_dict(comment.get("author")), needles=needles
                )
                and comment_mentions_target(
                    body, mention_tokens=mentions, assignee_needles=needles
                )
            ):
                cid = _comment_id(comment) or f"{key}:body"
                event_id = f"comment:{cid}"
                event = _build_event(
                    webhook_event="jira:issue_updated",
                    issue=issue,
                    trigger="mention",
                    event_id=event_id,
                    comment={**comment, "body": body},
                    changelog=changelog or None,
                    timestamp=data.get("timestamp"),
                    raw=data,
                )
                logger.info(
                    f"Jira webhook mention (via issue_updated) accepted: {key} "
                    f"comment={cid}"
                )
                return JiraWebhookDecision(
                    True,
                    "accepted",
                    event=event,
                    trigger="mention",
                    event_id=event_id,
                    raw=data,
                )
        return JiraWebhookDecision(False, "ignored issue update (not assign-to-bot)")

    if event_name:
        return JiraWebhookDecision(False, f"ignored event {event_name!r}")
    return JiraWebhookDecision(False, "unrecognized jira webhook payload")
