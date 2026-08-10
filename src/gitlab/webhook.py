"""Parse GitLab Note Hook payloads (CE and EE / all plans).

Project-level **Note** / **Confidential Note** webhooks exist on GitLab.com
(free) and self-managed CE/EE. Group webhooks are EE-only — operators should
register the hook on the **project**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.gitlab.keys import gitlab_issue_key
from src.gitlab.mentions import (
    note_mentions_bot,
    normalize_mention,
    parse_mention_list,
    strip_bot_mentions,
)
from src.logger import logger


GITLAB_NOTE_EVENTS = frozenset({"Note Hook", "Confidential Note Hook"})
_REPLY_PREFIX = "*Virtual Developer*"


@dataclass
class GitlabMrNoteEvent:
    """One MR comment that mentioned the bot."""

    issue_key: str
    note_id: str
    note_body: str
    prompt: str
    author_username: str
    author_name: str
    project_id: int
    project_path: str
    repository_url: str
    host: str
    mr_iid: int
    mr_title: str
    mr_description: str
    source_branch: str
    target_branch: str
    mr_url: str
    discussion_id: str = ""
    webhook_event: str = "Note Hook"
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "note_id": self.note_id,
            "note_body": self.note_body,
            "prompt": self.prompt,
            "author_username": self.author_username,
            "author_name": self.author_name,
            "project_id": self.project_id,
            "project_path": self.project_path,
            "repository_url": self.repository_url,
            "host": self.host,
            "mr_iid": self.mr_iid,
            "mr_title": self.mr_title,
            "mr_description": self.mr_description,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "mr_url": self.mr_url,
            "discussion_id": self.discussion_id,
            "webhook_event": self.webhook_event,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitlabMrNoteEvent":
        d = data or {}
        return cls(
            issue_key=str(d.get("issue_key") or ""),
            note_id=str(d.get("note_id") or ""),
            note_body=str(d.get("note_body") or ""),
            prompt=str(d.get("prompt") or ""),
            author_username=str(d.get("author_username") or ""),
            author_name=str(d.get("author_name") or ""),
            project_id=int(d.get("project_id") or 0),
            project_path=str(d.get("project_path") or ""),
            repository_url=str(d.get("repository_url") or ""),
            host=str(d.get("host") or ""),
            mr_iid=int(d.get("mr_iid") or 0),
            mr_title=str(d.get("mr_title") or ""),
            mr_description=str(d.get("mr_description") or ""),
            source_branch=str(d.get("source_branch") or ""),
            target_branch=str(d.get("target_branch") or ""),
            mr_url=str(d.get("mr_url") or ""),
            discussion_id=str(d.get("discussion_id") or ""),
            webhook_event=str(d.get("webhook_event") or "Note Hook"),
            raw=d.get("raw") if isinstance(d.get("raw"), dict) else {},
        )


@dataclass
class WebhookDecision:
    accepted: bool
    reason: str
    event: Optional[GitlabMrNoteEvent] = None
    http_status: int = 200


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _repo_http_url(project: Dict[str, Any], repository: Dict[str, Any]) -> str:
    for key in ("http_url_to_repo", "http_url"):
        url = _s(project.get(key))
        if url:
            return url
    home = _s(project.get("web_url"))
    if home:
        return home.rstrip("/") + ".git"
    git_ssh = _s(repository.get("url")) or _s(project.get("git_ssh_url"))
    if git_ssh.startswith("git@"):
        # git@host:group/repo.git → https://host/group/repo.git
        rest = git_ssh[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            return f"https://{host}/{path.lstrip('/')}"
    return git_ssh


def _host_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@"):
        rest = raw[4:]
        return rest.split(":", 1)[0].lower()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        name = (parsed.hostname or "").lower()
        if parsed.port and parsed.port not in (80, 443):
            return f"{name}:{parsed.port}"
        return name
    except Exception:
        return ""


def validate_webhook_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time-ish compare. Empty expected → accept (dev / simulator)."""
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


def decide_gitlab_note_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    secret: str = "",
    bot_mentions: Optional[List[str]] = None,
    bot_usernames: Optional[List[str]] = None,
) -> WebhookDecision:
    """Accept only MR comments that @mention the configured bot."""
    headers = headers or {}
    header_map = {str(k).lower(): str(v) for k, v in headers.items()}
    event_name = header_map.get("x-gitlab-event") or header_map.get("x-gitlab-event".lower()) or ""

    if not enabled:
        return WebhookDecision(False, "gitlab webhook disabled")

    want = (secret or "").strip()
    if not want:
        return WebhookDecision(
            False, "webhook secret required", http_status=401
        )

    token = header_map.get("x-gitlab-token") or ""
    if not validate_webhook_token(token, secret):
        return WebhookDecision(False, "invalid webhook token", http_status=401)

    if event_name and event_name not in GITLAB_NOTE_EVENTS:
        return WebhookDecision(False, f"ignored event {event_name!r}")

    data = payload if isinstance(payload, dict) else {}
    kind = _s(data.get("object_kind") or data.get("event_type")).lower()
    if kind and kind not in {"note", "confidential_note"}:
        return WebhookDecision(False, f"ignored object_kind={kind!r}")

    attrs = _as_dict(data.get("object_attributes"))
    notable = _s(attrs.get("noteable_type") or attrs.get("noteableType"))
    if notable.lower() not in {"mergerequest", "merge_request", "merge request"}:
        return WebhookDecision(False, f"ignored noteable_type={notable!r}")

    note = attrs.get("note")
    if not isinstance(note, str) or not note.strip():
        return WebhookDecision(False, "empty note")

    if note.lstrip().startswith(_REPLY_PREFIX):
        return WebhookDecision(False, "ignored bot reply")

    mentions = parse_mention_list(bot_mentions)
    if not mentions:
        return WebhookDecision(False, "no GITLAB_BOT_MENTIONS configured")
    if not note_mentions_bot(note, mentions):
        return WebhookDecision(False, "bot not mentioned")

    user = _as_dict(data.get("user"))
    author = normalize_mention(
        _s(user.get("username")) or _s(user.get("username")) or _s(user.get("name"))
    )
    bots = set(parse_mention_list(bot_usernames) or mentions)
    if author and author in bots:
        return WebhookDecision(False, "ignored comment from bot user")

    project = _as_dict(data.get("project"))
    repository = _as_dict(data.get("repository"))
    mr = _as_dict(data.get("merge_request") or data.get("mergeRequest"))
    if not mr:
        return WebhookDecision(False, "missing merge_request")

    try:
        mr_iid = int(mr.get("iid") or attrs.get("noteable_iid") or 0)
    except (TypeError, ValueError):
        mr_iid = 0
    if mr_iid <= 0:
        return WebhookDecision(False, "missing merge request iid")

    try:
        project_id = int(project.get("id") or mr.get("target_project_id") or 0)
    except (TypeError, ValueError):
        project_id = 0

    path = (
        _s(project.get("path_with_namespace"))
        or _s(project.get("pathWithNamespace"))
        or _s(mr.get("target_project_path"))
    )
    repo_url = _repo_http_url(project, repository)
    host = _host_from_url(repo_url) or _host_from_url(_s(project.get("web_url")))
    source = _s(mr.get("source_branch") or mr.get("sourceBranch"))
    target = _s(mr.get("target_branch") or mr.get("targetBranch"))
    if not repo_url or not source or not target:
        return WebhookDecision(
            False, "merge request missing repository or source/target branch"
        )

    prompt = strip_bot_mentions(note, mentions)
    if not prompt:
        prompt = note.strip()

    note_id = str(attrs.get("id") or attrs.get("note_id") or "")
    event = GitlabMrNoteEvent(
        issue_key=gitlab_issue_key(path or f"project-{project_id}", mr_iid),
        note_id=note_id,
        note_body=note.strip(),
        prompt=prompt,
        author_username=_s(user.get("username")),
        author_name=_s(user.get("name")),
        project_id=project_id,
        project_path=path,
        repository_url=repo_url,
        host=host,
        mr_iid=mr_iid,
        mr_title=_s(mr.get("title")),
        mr_description=_s(mr.get("description")),
        source_branch=source,
        target_branch=target,
        mr_url=_s(mr.get("web_url") or mr.get("url")),
        discussion_id=_s(attrs.get("discussion_id") or attrs.get("discussionId")),
        webhook_event=event_name or "Note Hook",
        raw=data,
    )
    logger.info(
        f"GitLab MR note accepted: {event.issue_key} "
        f"{event.project_path}!{event.mr_iid} note={event.note_id}"
    )
    return WebhookDecision(True, "accepted", event=event)
