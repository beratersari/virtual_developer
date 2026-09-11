"""Parse Azure DevOps Server 2022.2 service hooks (TFS on-prem).

Project-level **Pull request commented** and **Pull request**
(created / updated / merged / abandoned) hooks exist on Azure DevOps
Server 2022.2. Operators register a Web Hooks subscription and send
No webhook secret or password — the hook URL is enough.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from src.azure.keys import resolve_pr_issue_key
from src.azure.log import azure_info, clip
from src.azure.mentions import (
    EXECUTE_COMMAND,
    EXECUTE_MISSING_REASON,
    author_is_configured_bot,
    expand_guid_mentions,
    extract_mention_guids,
    format_execute_usage_note,
    mention_scan,
    note_is_execute_command,
    note_is_other_agent_handoff,
    note_mentions_bot,
    other_agent_handoff_reason,
    parse_mention_list,
    strip_azure_bot_mentions,
    strip_slash_command,
)
from src.brand import is_yaver_reply
from src.gitlab.webhook import WebhookDecision, validate_webhook_token


AZURE_COMMENT_EVENTS = frozenset(
    {
        "ms.vss-code.git-pullrequest-comment-event",
        "git.pullrequest.commented",
        "git.pullrequest.comment.event",
    }
)
AZURE_PR_EVENTS = frozenset(
    {
        "git.pullrequest.created",
        "git.pullrequest.updated",
        "git.pullrequest.merged",
        "git.pullrequest.abandoned",
        "git.pullrequest.reopened",
    }
)


def azure_comment_key(
    pr_id: Any,
    thread_id: str = "",
    comment_id: str = "",
    body: str = "",
    repository_url: str = "",
    project_path: str = "",
) -> str:
    """Stable queue/dedup id. TFS comment ids restart at 1 on every thread.

    With a thread id the key is ``repo:pr:thread:comment``. Without one, two
    new threads would both be ``repo:pr::1``; include a body hash so they
    stay distinct. The same webhook retry still matches (same body).

    PR numbers and thread ids restart per repository, so the key includes
    the git remote (or project/repo path) when the caller has it.
    """
    from src.state.session_bind_store import normalize_repo_key

    pid = str(pr_id or "").strip()
    tid = str(thread_id or "").strip()
    cid = str(comment_id or "").strip()
    if not pid and not tid and not cid:
        return ""
    repo = normalize_repo_key(repository_url or "") or (
        (project_path or "").strip().strip("/").lower()
    )
    if tid:
        tail = f"{pid}:{tid}:{cid}"
    else:
        text = (body or "").strip()
        if text:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            tail = f"{pid}:{digest}:{cid}"
        else:
            tail = f"{pid}::{cid}"
    return f"{repo}:{tail}" if repo else tail


@dataclass
class AzurePrCommentEvent:
    """One PR comment that mentioned the bot."""

    issue_key: str
    comment_id: str
    comment_body: str
    prompt: str
    author_username: str
    author_name: str
    collection_url: str
    project: str
    repository_id: str
    repository_name: str
    project_path: str
    repository_url: str
    host: str
    pr_id: int
    pr_title: str
    pr_description: str
    source_branch: str
    target_branch: str
    pr_url: str
    thread_id: str = ""
    webhook_event: str = "ms.vss-code.git-pullrequest-comment-event"
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_key": self.issue_key,
            "comment_id": self.comment_id,
            "comment_body": self.comment_body,
            "prompt": self.prompt,
            "author_username": self.author_username,
            "author_name": self.author_name,
            "collection_url": self.collection_url,
            "project": self.project,
            "repository_id": self.repository_id,
            "repository_name": self.repository_name,
            "project_path": self.project_path,
            "repository_url": self.repository_url,
            "host": self.host,
            "pr_id": self.pr_id,
            "pr_title": self.pr_title,
            "pr_description": self.pr_description,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "pr_url": self.pr_url,
            "thread_id": self.thread_id,
            "webhook_event": self.webhook_event,
            "raw": self.raw if isinstance(self.raw, dict) else {},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AzurePrCommentEvent":
        d = data or {}
        return cls(
            issue_key=str(d.get("issue_key") or ""),
            comment_id=str(d.get("comment_id") or ""),
            comment_body=str(d.get("comment_body") or ""),
            prompt=str(d.get("prompt") or ""),
            author_username=str(d.get("author_username") or ""),
            author_name=str(d.get("author_name") or ""),
            collection_url=str(d.get("collection_url") or ""),
            project=str(d.get("project") or ""),
            repository_id=str(d.get("repository_id") or ""),
            repository_name=str(d.get("repository_name") or ""),
            project_path=str(d.get("project_path") or ""),
            repository_url=str(d.get("repository_url") or ""),
            host=str(d.get("host") or ""),
            pr_id=int(d.get("pr_id") or 0),
            pr_title=str(d.get("pr_title") or ""),
            pr_description=str(d.get("pr_description") or ""),
            source_branch=str(d.get("source_branch") or ""),
            target_branch=str(d.get("target_branch") or ""),
            pr_url=str(d.get("pr_url") or ""),
            thread_id=str(d.get("thread_id") or ""),
            webhook_event=str(
                d.get("webhook_event")
                or "ms.vss-code.git-pullrequest-comment-event"
            ),
            raw=d.get("raw") if isinstance(d.get("raw"), dict) else {},
        )


@dataclass
class AzurePrLifecycleEvent:
    """Pull request created / updated / merged / abandoned."""

    issue_key: str
    action: str
    state: str
    collection_url: str
    project: str
    repository_id: str
    repository_name: str
    project_path: str
    repository_url: str
    host: str
    pr_id: int
    pr_title: str
    pr_description: str
    source_branch: str
    target_branch: str
    pr_url: str
    webhook_event: str = "git.pullrequest.updated"
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_merged(self) -> bool:
        if self.action in {"merge", "merged", "completed"}:
            return True
        return self.state in {"completed", "merged"}

    @property
    def is_closed(self) -> bool:
        """True for an abandoned (not merged) PR. Completed is separate."""
        if self.is_merged:
            return False
        return self.action in {"abandon", "abandoned", "close", "closed"} or (
            self.state in {"abandoned", "closed"}
        )

    @property
    def should_delete_clone(self) -> bool:
        """Temp clone is disposable once the linked PR is completed or abandoned."""
        return self.is_merged or self.is_closed


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _s(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def extract_azure_thread_id(
    comment: Dict[str, Any], resource: Optional[Dict[str, Any]] = None
) -> str:
    """Thread id for an in-thread reply. Never use comment.id as the thread."""
    resource = resource or {}
    for raw in (
        comment.get("threadId"),
        comment.get("thread_id"),
        resource.get("threadId"),
        resource.get("thread_id"),
        resource.get("pullRequestThreadId"),
        _as_dict(resource.get("pullRequestThread")).get("id"),
        _as_dict(resource.get("thread")).get("id"),
    ):
        text = str(raw).strip() if raw is not None and raw != "" else ""
        if text and text.isdigit():
            return text
    hrefs: List[str] = []
    links = _as_dict(comment.get("_links"))
    for key in ("self", "threads", "thread"):
        hrefs.append(_s(_as_dict(links.get(key)).get("href")))
    hrefs.append(_s(comment.get("url")))
    resource_self = _as_dict(_as_dict(resource.get("_links")).get("self"))
    hrefs.append(_s(resource_self.get("href")))
    for href in hrefs:
        match = re.search(r"/threads/(\d+)", href or "", re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _header_map(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _event_type(payload: Dict[str, Any], headers: Dict[str, str]) -> str:
    return (
        _s(payload.get("eventType") or payload.get("event_type"))
        or _s(headers.get("x-azure-event"))
        or _s(headers.get("x-tfs-event"))
    ).lower()


def _provided_token(headers: Dict[str, str]) -> str:
    """Shared secret from custom header or Basic password (TFS Web Hooks UI)."""
    for key in (
        "x-azure-token",
        "x-tfs-token",
        "x-vss-token",
        "x-webhook-token",
    ):
        got = _s(headers.get(key))
        if got:
            return got
    auth = _s(headers.get("authorization"))
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth.lower().startswith("basic "):
        try:
            import base64

            decoded = base64.b64decode(auth[6:].strip(), validate=False).decode(
                "utf-8"
            )
        except Exception:
            return ""
        _user, sep, password = decoded.partition(":")
        if sep:
            return password
    return ""


def _host_from_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("git@"):
        rest = raw[4:]
        return rest.split(":", 1)[0].lower()
    if raw.startswith("ssh://"):
        rest = raw[6:]
        if rest.startswith("git@"):
            rest = rest[4:]
        return rest.split("/", 1)[0].split(":", 1)[0].lower()
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


def _strip_ref(ref: str) -> str:
    text = _s(ref)
    for prefix in ("refs/heads/", "refs/tags/"):
        if text.lower().startswith(prefix):
            return text[len(prefix) :]
    return text


def _repo_http_url(repo: Dict[str, Any]) -> str:
    for key in ("remoteUrl", "remote_url", "cloneUrl", "clone_url", "url"):
        url = _s(repo.get(key))
        if url and "/_apis/" not in url.lower():
            return url
    ssh = _s(repo.get("sshUrl") or repo.get("ssh_url"))
    if ssh.startswith("git@"):
        rest = ssh[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            return f"https://{host}/{path.lstrip('/')}"
    if ssh.startswith("ssh://"):
        rest = ssh[6:]
        if rest.startswith("git@"):
            rest = rest[4:]
        if "/" in rest:
            host, path = rest.split("/", 1)
            return f"https://{host}/{path.lstrip('/')}"
    web = _s(repo.get("webUrl") or repo.get("web_url"))
    if web:
        return web.rstrip("/") + ".git"
    return ssh


def parse_azure_git_url(url: str) -> Optional[Dict[str, str]]:
    """Split an Azure DevOps Server git URL into collection / project / repo.

    Accepts::

        https://tfs/tfs/DefaultCollection/Project/_git/Repo
        https://tfs:8080/DefaultCollection/Project/_git/Repo.git
        https://tfs/tfs/DefaultCollection/_git/Repo
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith("git@"):
        rest = raw[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            raw = f"https://{host}/{path.lstrip('/')}"
    elif raw.startswith("ssh://"):
        rest = raw[6:]
        if rest.startswith("git@"):
            rest = rest[4:]
        if "/" in rest:
            host, path = rest.split("/", 1)
            raw = f"https://{host}/{path.lstrip('/')}"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if parsed.port and parsed.port not in (80, 443):
        host_port = f"{host}:{parsed.port}"
    else:
        host_port = host
    path = unquote((parsed.path or "").strip("/"))
    if path.endswith(".git"):
        path = path[:-4]
    match = re.search(
        r"^(?P<prefix>.+?)/(?P<project>[^/]+)/_git/(?P<repo>[^/]+)/?$",
        path,
        re.IGNORECASE,
    )
    if match:
        prefix = match.group("prefix").strip("/")
        project = match.group("project")
        repo = match.group("repo")
        scheme = parsed.scheme or "https"
        collection_url = f"{scheme}://{parsed.netloc}/{prefix}".rstrip("/")
        return {
            "host": host_port,
            "collection_url": collection_url,
            "project": project,
            "repository": repo,
            "project_path": f"{prefix}/{project}/{repo}",
        }
    match = re.search(
        r"^(?P<prefix>.+?)/_git/(?P<repo>[^/]+)/?$",
        path,
        re.IGNORECASE,
    )
    if match:
        prefix = match.group("prefix").strip("/")
        repo = match.group("repo")
        scheme = parsed.scheme or "https"
        collection_url = f"{scheme}://{parsed.netloc}/{prefix}".rstrip("/")
        return {
            "host": host_port,
            "collection_url": collection_url,
            "project": "",
            "repository": repo,
            "project_path": f"{prefix}/{repo}",
        }
    return None


def parse_pull_request_url(url: str) -> Optional[tuple[str, str, int]]:
    """Split a PR web URL → host, project_path, id.

    ``https://tfs/tfs/Col/Proj/_git/Repo/pullrequest/12``
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    path = unquote((parsed.path or "").strip("/"))
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    match = re.search(
        r"^(.*?)/_git/([^/]+)/(?:pullrequest|pullRequest)/(\d+)/?$",
        path,
        re.IGNORECASE,
    )
    if not host or not match:
        return None
    try:
        pr_id = int(match.group(3))
    except (TypeError, ValueError):
        return None
    if pr_id <= 0:
        return None
    project_path = f"{match.group(1).strip('/')}/{match.group(2)}"
    return host, project_path, pr_id


def _collection_url(
    payload: Dict[str, Any], repo: Dict[str, Any], repo_url: str
) -> str:
    containers = _as_dict(payload.get("resourceContainers"))
    collection = _as_dict(containers.get("collection"))
    base = _s(collection.get("baseUrl") or collection.get("base_url"))
    if base:
        return base.rstrip("/")
    parsed = parse_azure_git_url(repo_url)
    if parsed:
        return parsed["collection_url"]
    api = _s(repo.get("url"))
    if "/_apis/" in api:
        return api.split("/_apis/", 1)[0].rstrip("/")
    return ""


def _pr_web_url(
    pr: Dict[str, Any],
    repo: Dict[str, Any],
    collection_url: str,
    project: str,
    repo_name: str,
    pr_id: int,
) -> str:
    links = _as_dict(pr.get("_links") or repo.get("_links"))
    web = _as_dict(links.get("web"))
    href = _s(web.get("href") or pr.get("url") or "")
    if href and "/_apis/" not in href.lower() and "pullrequest" in href.lower():
        return href
    if collection_url and project and repo_name and pr_id > 0:
        return (
            f"{collection_url.rstrip('/')}/{project}/_git/{repo_name}"
            f"/pullrequest/{pr_id}"
        )
    return href


def _jira_project_keys(explicit: Optional[List[str]]) -> List[str]:
    if explicit is not None:
        return list(explicit)
    try:
        from src.config import settings as _settings

        return list(getattr(_settings, "jira_projects_list", None) or [])
    except Exception:
        return []


def _token_header_name(headers: Dict[str, str]) -> str:
    for key in (
        "x-azure-token",
        "x-tfs-token",
        "x-vss-token",
        "x-webhook-token",
    ):
        if _s(headers.get(key)):
            return key
    auth = _s(headers.get("authorization"))
    if auth.lower().startswith("bearer "):
        return "authorization:bearer"
    if auth.lower().startswith("basic "):
        return "authorization:basic"
    return "none"


def _auth_decision(
    *,
    enabled: bool,
    secret: str,
    headers: Dict[str, str],
    disabled_reason: str,
) -> Optional[WebhookDecision]:
    if not enabled:
        azure_info(f"webhook reject reason={disabled_reason!r}")
        return WebhookDecision(False, disabled_reason)
    azure_info("webhook auth skipped (no Azure webhook secret)")
    return None


def decide_azure_comment_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    secret: str = "",
    bot_mentions: Optional[List[str]] = None,
    bot_usernames: Optional[List[str]] = None,
    jira_project_keys: Optional[List[str]] = None,
) -> WebhookDecision:
    """Accept only PR comments that @mention the configured bot."""
    header_map = _header_map(headers)
    data = payload if isinstance(payload, dict) else {}
    event_name = _event_type(data, header_map)

    denied = _auth_decision(
        enabled=enabled,
        secret=secret,
        headers=header_map,
        disabled_reason="azure webhook disabled",
    )
    if denied is not None:
        return denied

    if event_name and event_name not in AZURE_COMMENT_EVENTS:
        azure_info(f"comment reject reason='ignored event' event={event_name!r}")
        return WebhookDecision(False, f"ignored event {event_name!r}")

    resource = _as_dict(data.get("resource"))
    comment = _as_dict(resource.get("comment"))
    pr = _as_dict(
        resource.get("pullRequest")
        or resource.get("pull_request")
        or resource
    )
    if not comment and _s(resource.get("content")):
        comment = resource
    note = _s(comment.get("content") or comment.get("comments"))
    if not note:
        azure_info(f"comment reject reason='empty comment' event={event_name!r}")
        return WebhookDecision(False, "empty comment")

    if is_yaver_reply(note):
        azure_info(
            f"comment reject reason='ignored bot reply' event={event_name!r} "
            f"preview={clip(note)!r}"
        )
        return WebhookDecision(False, "ignored bot reply")

    mentions = parse_mention_list(bot_mentions)
    scan = mention_scan(note, mentions)
    if not mentions:
        azure_info(
            "comment reject reason='no AZURE_TRIGGER_USER configured' "
            f"extracted={scan.get('extracted')}"
        )
        return WebhookDecision(False, "no AZURE_TRIGGER_USER configured")
    pending_guids = extract_mention_guids(note)
    mentioned = note_mentions_bot(note, mentions)
    if not mentioned and not pending_guids:
        azure_info(
            f"comment reject reason='bot not mentioned' event={event_name!r} "
            f"configured={scan.get('configured')} extracted={scan.get('extracted')} "
            f"preview={clip(note)!r}"
        )
        return WebhookDecision(False, "bot not mentioned")

    author = _as_dict(comment.get("author"))

    repo = _as_dict(pr.get("repository") or resource.get("repository"))
    project = _as_dict(repo.get("project") or resource.get("project"))
    try:
        pr_id = int(pr.get("pullRequestId") or pr.get("pull_request_id") or 0)
    except (TypeError, ValueError):
        pr_id = 0
    if pr_id <= 0:
        azure_info("comment reject reason='missing pull request id'")
        return WebhookDecision(False, "missing pull request id")

    repo_url = _repo_http_url(repo)
    parsed = parse_azure_git_url(repo_url) or {}
    collection_url = _collection_url(data, repo, repo_url)
    project_name = (
        _s(project.get("name"))
        or _s(parsed.get("project"))
        or _s(pr.get("project"))
    )
    repo_name = (
        _s(repo.get("name"))
        or _s(parsed.get("repository"))
        or _s(repo.get("id"))
    )
    repo_id = _s(repo.get("id")) or repo_name
    source = _strip_ref(
        _s(pr.get("sourceRefName") or pr.get("source_ref_name"))
    )
    target = _strip_ref(
        _s(pr.get("targetRefName") or pr.get("target_ref_name"))
    )
    if not repo_url or not source or not target:
        azure_info(
            "comment reject reason='pull request missing repository or source/target branch' "
            f"repo_url={repo_url!r} source={source!r} target={target!r}"
        )
        return WebhookDecision(
            False, "pull request missing repository or source/target branch"
        )

    prompt = strip_azure_bot_mentions(note, mentions)
    prompt = strip_slash_command(prompt, EXECUTE_COMMAND)
    if not prompt:
        prompt = note.strip()

    pr_title = _s(pr.get("title"))
    pr_description = _s(pr.get("description"))
    project_path = (
        _s(parsed.get("project_path"))
        or "/".join(p for p in (project_name, repo_name) if p)
    )
    host = (
        _host_from_url(repo_url)
        or _host_from_url(collection_url)
        or _s(parsed.get("host"))
    )
    trigger_names = list(mentions)
    from src.azure.identity import (
        fetch_bot_identity,
        reviewer_bot_aliases,
        seed_identity_aliases,
    )

    identity = fetch_bot_identity(host=host, collection_url=collection_url)
    trigger_names.extend(seed_identity_aliases(mentions, identity))
    trigger_names.extend(
        reviewer_bot_aliases(
            pr,
            trigger_names,
            bot_id=str((identity or {}).get("id") or ""),
        )
    )
    extra = expand_guid_mentions(note, trigger_names)
    if pending_guids and not extra and not mentioned:
        try:
            from src.azure.client import AzureDevOpsClient

            extra = expand_guid_mentions(
                note,
                trigger_names,
                lookup=AzureDevOpsClient(
                    host=host, collection_url=collection_url
                ).identity_aliases,
            )
        except Exception:
            extra = []
    if extra:
        trigger_names = list(trigger_names) + extra
        mentioned = True
    if not mentioned:
        mentioned = note_mentions_bot(note, trigger_names)
    author_ids = [
        _s(author.get("id")),
        _s(author.get("descriptor")),
    ]
    if author_is_configured_bot(
        [
            *author_ids,
            _s(author.get("uniqueName") or author.get("unique_name")),
            _s(author.get("directoryAlias") or author.get("principalName")),
            _s(author.get("displayName") or author.get("display_name")),
        ],
        bot_usernames or trigger_names,
    ) or (
        identity
        and _s(author.get("id"))
        and _s(author.get("id")).lower() == str(identity.get("id") or "").lower()
    ):
        azure_info(
            f"comment reject reason='ignored comment from bot user' "
            f"author={_s(author.get('uniqueName') or author.get('displayName') or author.get('id'))!r}"
        )
        return WebhookDecision(False, "ignored comment from bot user")
    if not mentioned:
        azure_info(
            f"comment reject reason='bot not mentioned' event={event_name!r} "
            f"configured={scan.get('configured')} extracted={scan.get('extracted')} "
            f"guids={pending_guids} preview={clip(note)!r}"
        )
        return WebhookDecision(False, "bot not mentioned")
    if note_is_other_agent_handoff(note, bot_mentions or trigger_names):
        reason = other_agent_handoff_reason(note, bot_mentions or trigger_names)
        azure_info(
            f"comment reject reason={reason!r} event={event_name!r} "
            f"preview={clip(note)!r}"
        )
        return WebhookDecision(False, reason)
    pr_url = _pr_web_url(
        pr, repo, collection_url, project_name, repo_name, pr_id
    )
    thread_id = extract_azure_thread_id(comment, resource)

    issue_key = resolve_pr_issue_key(
        pr_title=pr_title,
        pr_description=pr_description,
        project_path=project_path or f"project-{repo_id}",
        pr_id=pr_id,
        project_keys=_jira_project_keys(jira_project_keys),
    )

    event = AzurePrCommentEvent(
        issue_key=issue_key,
        comment_id=str(comment.get("id") or comment.get("commentId") or ""),
        comment_body=note.strip(),
        prompt=prompt,
        author_username=_s(
            author.get("uniqueName") or author.get("unique_name")
        ),
        author_name=_s(
            author.get("displayName") or author.get("display_name")
        ),
        collection_url=collection_url,
        project=project_name,
        repository_id=repo_id,
        repository_name=repo_name,
        project_path=project_path,
        repository_url=repo_url,
        host=host,
        pr_id=pr_id,
        pr_title=pr_title,
        pr_description=pr_description,
        source_branch=source,
        target_branch=target,
        pr_url=pr_url,
        thread_id=thread_id,
        webhook_event=event_name or "ms.vss-code.git-pullrequest-comment-event",
        raw=data,
    )
    try:
        from src.log_context import set_issue_key

        set_issue_key(event.issue_key)
    except Exception:
        pass
    # Intentional: /yaver must sit next to a configured trigger name
    # (AZURE_TRIGGER_USER). A GUID-only @<VSID> chip can count as a mention
    # (usage note) but does not start a job. Do not pass resolved GUID aliases
    # into this check.
    if not note_is_execute_command(note, bot_mentions or trigger_names):
        azure_info(
            f"comment reject reason={EXECUTE_MISSING_REASON!r} event={event_name!r} "
            f"thread={event.thread_id or '-'} preview={clip(note)!r}"
        )
        return WebhookDecision(
            False, EXECUTE_MISSING_REASON, event=event, usage_note=True
        )
    azure_info(
        f"comment accepted issue={event.issue_key} "
        f"pr={event.project_path}!{event.pr_id} comment={event.comment_id} "
        f"thread={event.thread_id or '-'} host={event.host} "
        f"collection={event.collection_url} "
        f"repo={event.repository_name} repo_id={event.repository_id} "
        f"source={event.source_branch} target={event.target_branch} "
        f"author={(event.author_username or event.author_name)!r} "
        f"title={pr_title[:80]!r} prompt_chars={len(prompt)} "
        f"preview={clip(prompt)!r}"
    )
    return WebhookDecision(True, "accepted", event=event)


def post_azure_usage_note(event: AzurePrCommentEvent, bot_name: str = "") -> bool:
    """Reply in the PR thread with /yaver usage. Never a new thread."""
    thread_id = (getattr(event, "thread_id", "") or "").strip()
    from src.azure.client import AzureDevOpsClient

    client = AzureDevOpsClient(
        host=getattr(event, "host", "") or "",
        collection_url=getattr(event, "collection_url", "") or "",
    )
    if not thread_id:
        thread_id = client.find_thread_id_for_comment(
            project=str(getattr(event, "project", "") or ""),
            repository=(
                getattr(event, "repository_id", "")
                or getattr(event, "repository_name", "")
            ),
            pr_id=int(getattr(event, "pr_id", 0) or 0),
            comment_id=str(getattr(event, "comment_id", "") or ""),
            comment_content=str(
                getattr(event, "comment_body", "") or getattr(event, "prompt", "") or ""
            ),
        )
        if thread_id:
            event.thread_id = thread_id
    if not thread_id:
        azure_info(
            "usage note skipped: no thread_id "
            f"{getattr(event, 'project_path', '')}!{getattr(event, 'pr_id', '')}"
        )
        return False
    posted = client.post_pr_comment(
        project=str(getattr(event, "project", "") or ""),
        repository=(
            getattr(event, "repository_id", "") or getattr(event, "repository_name", "")
        ),
        pr_id=int(getattr(event, "pr_id", 0) or 0),
        body=format_execute_usage_note(bot_name),
        thread_id=thread_id,
        allow_new_thread=False,
        parent_comment_id=str(getattr(event, "comment_id", "") or ""),
    )
    return posted is not None


def decide_azure_pr_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    secret: str = "",
    jira_project_keys: Optional[List[str]] = None,
) -> WebhookDecision:
    """Accept pull-request created / updated / merged / abandoned hooks."""
    header_map = _header_map(headers)
    data = payload if isinstance(payload, dict) else {}
    event_name = _event_type(data, header_map)

    denied = _auth_decision(
        enabled=enabled,
        secret=secret,
        headers=header_map,
        disabled_reason="azure webhook disabled",
    )
    if denied is not None:
        return denied

    if event_name and event_name not in AZURE_PR_EVENTS:
        azure_info(f"lifecycle reject reason='ignored event' event={event_name!r}")
        return WebhookDecision(False, f"ignored event {event_name!r}")

    resource = _as_dict(data.get("resource"))
    pr = _as_dict(
        resource.get("pullRequest") or resource.get("pull_request") or resource
    )
    repo = _as_dict(pr.get("repository") or resource.get("repository"))
    project = _as_dict(repo.get("project") or resource.get("project"))
    try:
        pr_id = int(pr.get("pullRequestId") or pr.get("pull_request_id") or 0)
    except (TypeError, ValueError):
        pr_id = 0
    if pr_id <= 0:
        azure_info(f"lifecycle reject reason='missing pull request id' event={event_name!r}")
        return WebhookDecision(False, "missing pull request id")

    status = _s(pr.get("status") or resource.get("status")).lower()
    if event_name.endswith(".merged"):
        action = "merge"
        if not status:
            status = "completed"
    elif event_name.endswith(".abandoned"):
        action = "abandoned"
        if not status:
            status = "abandoned"
    elif event_name.endswith(".created"):
        action = "created"
        if not status:
            status = "active"
    elif event_name.endswith(".reopened"):
        action = "reopened"
        if not status:
            status = "active"
    else:
        action = status or "updated"

    repo_url = _repo_http_url(repo)
    parsed = parse_azure_git_url(repo_url) or {}
    collection_url = _collection_url(data, repo, repo_url)
    project_name = _s(project.get("name")) or _s(parsed.get("project"))
    repo_name = _s(repo.get("name")) or _s(parsed.get("repository"))
    repo_id = _s(repo.get("id")) or repo_name
    source = _strip_ref(
        _s(pr.get("sourceRefName") or pr.get("source_ref_name"))
    )
    target = _strip_ref(
        _s(pr.get("targetRefName") or pr.get("target_ref_name"))
    )
    pr_title = _s(pr.get("title"))
    pr_description = _s(pr.get("description"))
    project_path = (
        _s(parsed.get("project_path"))
        or "/".join(p for p in (project_name, repo_name) if p)
    )
    host = (
        _host_from_url(repo_url)
        or _host_from_url(collection_url)
        or _s(parsed.get("host"))
    )
    pr_url = _pr_web_url(
        pr, repo, collection_url, project_name, repo_name, pr_id
    )

    issue_key = resolve_pr_issue_key(
        pr_title=pr_title,
        pr_description=pr_description,
        project_path=project_path or f"project-{repo_id}",
        pr_id=pr_id,
        project_keys=_jira_project_keys(jira_project_keys),
    )
    event = AzurePrLifecycleEvent(
        issue_key=issue_key,
        action=action,
        state=status,
        collection_url=collection_url,
        project=project_name,
        repository_id=repo_id,
        repository_name=repo_name,
        project_path=project_path,
        repository_url=repo_url,
        host=host,
        pr_id=pr_id,
        pr_title=pr_title,
        pr_description=pr_description,
        source_branch=source,
        target_branch=target,
        pr_url=pr_url,
        webhook_event=event_name or "git.pullrequest.updated",
        raw=data,
    )
    azure_info(
        f"lifecycle accepted issue={event.issue_key} "
        f"pr={event.project_path}!{event.pr_id} "
        f"action={event.action or '-'} state={event.state or '-'} "
        f"host={event.host} collection={event.collection_url} "
        f"source={event.source_branch} target={event.target_branch} "
        f"url={event.pr_url or '-'}"
    )
    return WebhookDecision(True, "accepted", event=event)
