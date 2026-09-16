"""Azure DevOps Server 2022.2 work-item intake (webhook, not poll).

Work-item intake is webhook-driven. New work is assignment while the
item is **To Do** or **In Progress**, or the same process-template
column (New / Proposed / Approved, Active / Doing / Committed).
Resolved and Done/Closed are not intake.
Local issue key is ``WIT-{PROJECT}-{id}`` (bare numeric ids still load).
Moving In Progress → To Do while still assigned does **not** re-queue
(Jira To Do return does not apply on Azure Boards).
Plan revise/implement is comment-only (``/planRefactor`` / ``/planExecute``).

Work-item REST uses the same 7.1 / 7.0 fallback as ``AzureDevOpsClient``.
"""

from __future__ import annotations

import hashlib
import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from src.azure.identity import fetch_bot_identity
from src.azure.keys import azure_work_item_key
from src.azure.log import azure_info, azure_warning, clip
from src.azure.urls import parse_tfs_collection_url
from src.gitlab.mentions import identity_key, normalize_guid, normalize_mention
from src.azure.webhook import WebhookDecision, _as_dict, _event_type, _header_map, _s
from src.jira.plan_labels import HANDOFF_EXECUTE, HANDOFF_REFACTOR
from src.jira.poller import JiraPoller
from src.jira.triggers import (
    assignee_looks_like_bot,
    identity_matches_bot,
    issue_has_trigger_label,
    parse_trigger_labels,
    poller_triggers_on,
)
from src.state.models import TaskStatus


AZURE_WORKITEM_EVENTS = frozenset(
    {
        "workitem.created",
        "workitem.updated",
        "workitem.restored",
        "workitem.commented",
    }
)

PLAN_REFACTOR_COMMAND = "planRefactor"
PLAN_EXECUTE_COMMAND = "planExecute"
PLAN_COMMAND_MISSING_REASON = "work item mention missing /planRefactor or /planExecute"

# Field names Azure DevOps Server 2022.2 sends on workitem.updated.
_ASSIGNEE_FIELDS = frozenset({"system.assignedto", "assigned to", "assignedto"})
_DESCRIPTION_FIELDS = frozenset(
    {
        "system.description",
        "system.title",
        "microsoft.vsts.tcm.reprosteps",
        "description",
        "title",
        "repro steps",
    }
)
_LABEL_FIELDS = frozenset({"system.tags", "tags", "label", "labels"})
_STATE_FIELDS = frozenset(
    {
        "system.state",
        "state",
        "system.boardcolumn",
        "system.boardcolumndone",
        "system.boardlane",
    }
)
_COMMENT_FIELDS = frozenset({"system.history", "history"})
_INTAKE_CHANGE_KINDS = frozenset({"assignee"})

# Process-template categories on GET workitemtypes/{type}/states (7.1 / 7.0).
_TODO_CATEGORIES = frozenset({"proposed", "new"})
_IN_PROGRESS_CATEGORIES = frozenset({"inprogress", "in progress"})
# To Do / In Progress and the official process-template names for those
# columns. Resolved is not In Progress. Do not treat category "new" as
# intake — unknown states default to that key in work_item_fields_to_jira.
_TODO_STATE_NAMES = frozenset(
    {
        "to do",
        "todo",
        "new",
        "proposed",
        "approved",
        "open",
        "backlog",
        "selected for development",
        "ready for development",
        "yapılacak",
        "yapilacak",
        "yapılacaklar",
        "yapilacaklar",
    }
)
_IN_PROGRESS_STATE_NAMES = frozenset(
    {
        "in progress",
        "inprogress",
        "active",
        "doing",
        "committed",
        "wip",
        "devam ediyor",
        "devamediyor",
    }
)
_DONE_STATE_NAMES = frozenset(
    {
        "done",
        "closed",
        "completed",
        "removed",
        "cut",
        "kapatıldı",
        "kapatildi",
        "tamamlandı",
        "tamamlandi",
        "bitti",
    }
)

_COORDS: Dict[str, Dict[str, Any]] = {}
# TFS often sends workitem.commented *and* a History workitem.updated.
_SEEN_COMMENTS: Dict[str, float] = {}
_SEEN_COMMENT_TTL_SEC = 180.0

_HTML_BREAK = re.compile(r"(?i)<br\s*/?>")
_HTML_BLOCK = re.compile(r"(?i)</(p|div|li|h[1-6]|tr)>")
_HTML_TAG = re.compile(r"<[^>]+>")


def azure_html_to_text(raw: Any) -> str:
    """Flatten Azure HTML description / comment bodies for ``{params}``."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        for key in ("text", "html", "value", "$value"):
            if raw.get(key) is not None:
                return azure_html_to_text(raw.get(key))
        return ""
    text = str(raw)
    if not text.strip():
        return ""
    text = _HTML_BREAK.sub("\n", text)
    text = _HTML_BLOCK.sub("\n", text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_azure_tags(raw: Any) -> List[str]:
    """``System.Tags`` is a semicolon-separated string on Server 2022.2."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        out: List[str] = []
        seen: Set[str] = set()
        for item in raw:
            name = str(item or "").strip()
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
        return out
    text = str(raw or "")
    out = []
    seen = set()
    for part in re.split(r"[;,]", text):
        name = part.strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def join_azure_tags(labels: Iterable[Any]) -> str:
    names = parse_azure_tags(list(labels or []))
    return "; ".join(names)


def identity_as_assignee(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {"displayName": text, "name": text, "uniqueName": text}
    if not isinstance(raw, dict):
        return None
    display = _s(raw.get("displayName") or raw.get("customDisplayName"))
    unique = _s(raw.get("uniqueName") or raw.get("name") or raw.get("mailAddress"))
    ident = _s(raw.get("id") or raw.get("descriptor"))
    if not display and not unique and not ident:
        return None
    email = unique if "@" in unique else _s(raw.get("mailAddress"))
    return {
        "displayName": display or unique,
        "name": unique or display,
        "uniqueName": unique,
        "key": ident,
        "accountId": ident,
        "emailAddress": email,
    }


def assignee_matches_azure_bot(
    assignee: Optional[dict],
    needles: Optional[Iterable[str]] = None,
) -> bool:
    if not assignee or not isinstance(assignee, dict):
        return False
    if assignee_looks_like_bot(assignee, needles=needles):
        return True
    return identity_matches_bot(
        assignee.get("uniqueName"),
        assignee.get("descriptor"),
        needles=needles,
    )


def remember_work_item(issue_key: str, coords: Dict[str, Any]) -> None:
    key = (issue_key or "").strip()
    if not key or not isinstance(coords, dict):
        return
    row = {
        "host": _s(coords.get("host")),
        "collection_url": _s(coords.get("collection_url")),
        "project": _s(coords.get("project")),
        "work_item_id": int(coords.get("work_item_id") or 0),
        "work_item_type": _s(coords.get("work_item_type")),
        "web_url": _s(coords.get("web_url")),
    }
    if row["work_item_id"] <= 0:
        return
    _COORDS[key] = row


def work_item_coords(
    issue_key: str, state: Any = None
) -> Optional[Dict[str, Any]]:
    key = (issue_key or "").strip()
    if not key:
        return None
    cached = _COORDS.get(key)
    if cached and int(cached.get("work_item_id") or 0) > 0:
        return dict(cached)
    meta = {}
    if state is not None:
        meta = getattr(state, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
    if str(meta.get("source") or "").strip().lower() == "azure_workitem":
        wid = int(meta.get("azure_work_item_id") or 0)
        if wid > 0:
            row = {
                "host": _s(meta.get("azure_host")),
                "collection_url": _s(meta.get("azure_collection_url")),
                "project": _s(meta.get("azure_project")),
                "work_item_id": wid,
                "work_item_type": _s(meta.get("azure_work_item_type")),
                "web_url": _s(meta.get("azure_work_item_url")),
            }
            _COORDS[key] = row
            return dict(row)
    return None


def tracker_metadata(coords: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "azure_workitem",
        "azure_host": _s(coords.get("host")),
        "azure_collection_url": _s(coords.get("collection_url")),
        "azure_project": _s(coords.get("project")),
        "azure_work_item_id": int(coords.get("work_item_id") or 0),
        "azure_work_item_type": _s(coords.get("work_item_type")),
        "azure_work_item_url": _s(coords.get("web_url")),
    }


def _field_name(raw: Any) -> str:
    return str(raw or "").strip().lower()


def changed_workitem_fields(resource: Dict[str, Any]) -> Set[str]:
    fields = resource.get("fields")
    if not isinstance(fields, dict):
        return set()
    names: Set[str] = set()
    for key in fields:
        name = _field_name(key)
        if name:
            names.add(name)
    return names


def classify_workitem_changes(changed: Iterable[str]) -> Set[str]:
    kinds: Set[str] = set()
    for name in changed:
        n = _field_name(name)
        if n in _ASSIGNEE_FIELDS:
            kinds.add("assignee")
        elif n in _DESCRIPTION_FIELDS:
            kinds.add("description")
        elif n in _LABEL_FIELDS:
            kinds.add("label")
        elif n in _STATE_FIELDS or "kanban.column" in n:
            kinds.add("state")
        elif n in _COMMENT_FIELDS:
            kinds.add("comment")
    return kinds


def _identity_from_change(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict) and ("newValue" in raw or "oldValue" in raw):
        return identity_as_assignee(raw.get("newValue"))
    return identity_as_assignee(raw)


def _tags_from_change(raw: Any) -> List[str]:
    if isinstance(raw, dict) and "newValue" in raw:
        return parse_azure_tags(raw.get("newValue"))
    return parse_azure_tags(raw)


def _old_tags_from_change(raw: Any) -> List[str]:
    if isinstance(raw, dict) and "oldValue" in raw:
        return parse_azure_tags(raw.get("oldValue"))
    return []


def _collection_from_payload(payload: Dict[str, Any], resource: Dict[str, Any]) -> str:
    containers = _as_dict(payload.get("resourceContainers"))
    collection = _as_dict(containers.get("collection"))
    base = _s(collection.get("baseUrl") or collection.get("base_url"))
    if base:
        return parse_tfs_collection_url(base) or base.rstrip("/")
    url = _s(
        resource.get("url")
        or _as_dict(_as_dict(resource.get("_links")).get("self")).get("href")
        or _as_dict(_as_dict(resource.get("revision")).get("_links")).get("html")
    )
    if "/_apis/" in url:
        return parse_tfs_collection_url(url) or url.split("/_apis/", 1)[0].rstrip("/")
    return ""


def _host_from_collection(collection_url: str) -> str:
    try:
        parsed = urlparse(collection_url)
    except Exception:
        return ""
    name = (parsed.hostname or "").lower()
    if not name:
        return ""
    if parsed.port and parsed.port not in (80, 443):
        return f"{name}:{parsed.port}"
    return name


def _project_from_payload(
    payload: Dict[str, Any], fields: Dict[str, Any], resource: Dict[str, Any]
) -> str:
    name = _s(fields.get("System.TeamProject") or fields.get("System.AreaPath"))
    if "\\" in name:
        name = name.split("\\", 1)[0].strip()
    if name:
        return name
    containers = _as_dict(payload.get("resourceContainers"))
    project = _as_dict(containers.get("project"))
    base = _s(project.get("baseUrl") or project.get("base_url"))
    if base:
        tail = base.rstrip("/").split("/")[-1]
        if tail and tail.lower() not in {"tfs", "_apis"}:
            return tail
    return _s(resource.get("project") or resource.get("teamProject"))


def _work_item_id(resource: Dict[str, Any], revision: Dict[str, Any]) -> int:
    for raw in (
        resource.get("workItemId"),
        resource.get("id"),
        revision.get("id"),
    ):
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        if iid > 0:
            return iid
    return 0


def _web_url(collection_url: str, project: str, work_item_id: int, resource: Dict[str, Any]) -> str:
    links = _as_dict(resource.get("_links"))
    html_link = _as_dict(links.get("html")).get("href")
    if _s(html_link):
        return _s(html_link)
    rev_links = _as_dict(_as_dict(resource.get("revision")).get("_links"))
    href = _s(_as_dict(rev_links.get("html")).get("href"))
    if href:
        return href
    if collection_url and project and work_item_id > 0:
        return (
            f"{collection_url.rstrip('/')}/{project}/_workitems/edit/{work_item_id}"
        )
    return ""


def _status_category(state_name: str, category: str = "") -> str:
    cat = (category or "").strip().lower().replace(" ", "")
    if cat in _TODO_CATEGORIES:
        return "new"
    if cat in _IN_PROGRESS_CATEGORIES:
        return "indeterminate"
    if cat in {"resolved", "completed", "removed", "done"}:
        return "done"
    name = (state_name or "").strip().lower()
    if JiraPoller._is_todo_status_name(name) or name in _TODO_STATE_NAMES:
        return "new"
    if name in _IN_PROGRESS_STATE_NAMES:
        return "indeterminate"
    return ""


def _collection_norm(url: str) -> str:
    from src.azure.urls import parse_tfs_collection_url

    raw = parse_tfs_collection_url(url or "") or (url or "").strip()
    return raw.rstrip("/").lower()


def find_work_item_key_by_id(
    work_item_id: int,
    *,
    collection_url: str = "",
    state_manager: Any = None,
) -> str:
    """Local ``WIT-…`` key for this TFS id, scoped by collection when given.

    ``#42`` is not unique across collections. If *collection_url* is set,
    only that collection matches. If it is empty, return a key only when
    exactly one local work item has that id.
    """
    from src.azure.keys import is_azure_work_item_key, parse_azure_work_item_key

    try:
        want = int(work_item_id)
    except (TypeError, ValueError):
        return ""
    if want <= 0:
        return ""
    want_col = _collection_norm(collection_url)
    sm = state_manager
    if sm is None:
        try:
            from src.state.manager import JiraStateManager

            sm = JiraStateManager()
        except Exception:
            sm = None
    if sm is None or not hasattr(sm, "get_all_states"):
        return ""
    hits: list[str] = []
    seen: set[str] = set()
    for state in sm.get_all_states() or []:
        key = str(getattr(state, "issue_key", "") or "").strip()
        if not key or not is_azure_work_item_key(key):
            continue
        meta = getattr(state, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        source = str(meta.get("source") or "").strip().lower()
        if source and source not in {"azure_workitem", "azure"}:
            continue
        wid = 0
        try:
            wid = int(meta.get("azure_work_item_id") or 0)
        except (TypeError, ValueError):
            wid = 0
        if wid <= 0:
            _slug, wid = parse_azure_work_item_key(key)
        if wid != want:
            continue
        if want_col:
            have = _collection_norm(
                str(meta.get("azure_collection_url") or "")
            ) or _collection_norm(str((work_item_coords(key) or {}).get("collection_url") or ""))
            if have and have != want_col:
                continue
        if key not in seen:
            seen.add(key)
            hits.append(key)
    if len(hits) == 1:
        return hits[0]
    return ""


def find_work_item_key_by_git(
    repository_url: str,
    source_branch: str,
    target_branch: str,
    *,
    state_manager: Any = None,
) -> str:
    """Local work-item key whose {params} match repo + source + target.

    Used when an Azure PR title has no Jira/WIT key. Returns "" when none
    or more than one distinct key matches (do not guess).
    """
    from src.azure.keys import is_azure_work_item_key
    from src.issue_git_spec import parse_issue_git_spec
    from src.state.session_bind_store import normalize_branch, normalize_repo_key

    want_repo = normalize_repo_key(repository_url)
    want_src = normalize_branch(source_branch)
    want_tgt = normalize_branch(target_branch)
    if not want_repo or not want_src or not want_tgt:
        return ""

    def _spec_of(state: Any) -> tuple[str, str, str]:
        meta = getattr(state, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        repo = str(meta.get("repository_url") or "")
        src = str(meta.get("source_branch") or "")
        tgt = str(meta.get("target_branch") or "")
        if repo and src and tgt:
            return repo, src, tgt
        parsed, err = parse_issue_git_spec(
            getattr(state, "issue_summary", "") or "",
            getattr(state, "description", "") or "",
        )
        if err or parsed is None:
            return "", "", ""
        return parsed.repository_url, parsed.source_branch, parsed.target_branch

    hits: list[str] = []
    seen: set[str] = set()
    sm = state_manager
    if sm is None:
        try:
            from src.state.manager import JiraStateManager

            sm = JiraStateManager()
        except Exception:
            sm = None
    if sm is not None and hasattr(sm, "get_all_states"):
        for state in sm.get_all_states() or []:
            key = str(getattr(state, "issue_key", "") or "").strip()
            if not key or not is_azure_work_item_key(key):
                continue
            meta = getattr(state, "metadata", None) or {}
            source = str(meta.get("source") or "").strip().lower()
            if source and source not in {"azure_workitem", "azure"}:
                continue
            repo, src, tgt = _spec_of(state)
            if normalize_repo_key(repo) != want_repo:
                continue
            if normalize_branch(src) != want_src:
                continue
            if normalize_branch(tgt) != want_tgt:
                continue
            if key not in seen:
                seen.add(key)
                hits.append(key)
    if len(hits) == 1:
        return hits[0]
    return ""


def _norm_state_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (name or "").strip().lower()).strip()


def _fields_state_name(fields: Optional[Dict[str, Any]]) -> str:
    if not fields or not isinstance(fields, dict):
        return ""
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    return _norm_state_name(str((status or {}).get("name") or ""))


def work_item_is_todo_column(fields: Optional[Dict[str, Any]]) -> bool:
    """True for To Do and process-template equivalents (New, Proposed, …)."""
    name = _fields_state_name(fields)
    compact = name.replace(" ", "")
    if name in _TODO_STATE_NAMES or compact in _TODO_STATE_NAMES:
        return True
    return compact in {
        "todo",
        "yapilacak",
        "yapilacaklar",
        "yapılacak",
        "yapılacaklar",
    }


def work_item_is_in_progress_column(fields: Optional[Dict[str, Any]]) -> bool:
    """True for In Progress and equivalents (Active, Doing, Committed)."""
    name = _fields_state_name(fields)
    compact = name.replace(" ", "")
    return name in _IN_PROGRESS_STATE_NAMES or compact in _IN_PROGRESS_STATE_NAMES


def work_item_is_intake_column(fields: Optional[Dict[str, Any]]) -> bool:
    """Azure intake: To Do / In Progress and their process-template names."""
    return work_item_is_todo_column(fields) or work_item_is_in_progress_column(fields)


def work_item_is_done(fields: Optional[Dict[str, Any]]) -> bool:
    """True for Done/Closed/Completed. Resolved/Active are not Done."""
    if not fields or not isinstance(fields, dict):
        return False
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    name = _norm_state_name(str((status or {}).get("name") or ""))
    if name in _DONE_STATE_NAMES or name.replace(" ", "") in _DONE_STATE_NAMES:
        return True
    category = str(
        ((status or {}).get("statusCategory") or {}).get("key") or ""
    ).strip().lower()
    if category == "done" and name not in {"resolved", "resolve"}:
        return True
    return False


def work_item_fields_to_jira(
    fields: Dict[str, Any],
    *,
    state_category: str = "",
) -> Dict[str, Any]:
    title = _s(fields.get("System.Title"))
    description = azure_html_to_text(
        fields.get("System.Description")
        or fields.get("Microsoft.VSTS.TCM.ReproSteps")
        or ""
    )
    state_name = _s(fields.get("System.State")) or "New"
    category = state_category or _status_category(state_name)
    labels = parse_azure_tags(fields.get("System.Tags"))
    assignee = identity_as_assignee(fields.get("System.AssignedTo"))
    work_type = _s(fields.get("System.WorkItemType")) or "Issue"
    return {
        "summary": title,
        "description": description,
        "labels": labels,
        "assignee": assignee,
        "issuetype": {"name": work_type},
        "status": {
            "name": state_name,
            "statusCategory": {"key": category or "new"},
        },
    }


def normalize_work_item(
    *,
    project: str,
    work_item_id: int,
    fields: Dict[str, Any],
    host: str = "",
    collection_url: str = "",
    web_url: str = "",
    state_category: str = "",
    rev: int = 0,
) -> Dict[str, Any]:
    jira_fields = work_item_fields_to_jira(fields, state_category=state_category)
    key = azure_work_item_key(project or "project", work_item_id)
    coords = {
        "host": host,
        "collection_url": collection_url,
        "project": project,
        "work_item_id": work_item_id,
        "work_item_type": _s((jira_fields.get("issuetype") or {}).get("name")),
        "web_url": web_url,
    }
    remember_work_item(key, coords)
    return {
        "key": key,
        "id": str(work_item_id),
        "fields": jira_fields,
        "self": web_url,
        "azure": coords,
        "rev": rev,
    }


def changelog_from_resource(resource: Dict[str, Any]) -> Dict[str, Any]:
    """Jira-shaped changelog so plan-handoff helpers stay unchanged."""
    items: List[Dict[str, Any]] = []
    fields = resource.get("fields")
    if not isinstance(fields, dict):
        return {"items": items}
    for name, raw in fields.items():
        n = _field_name(name)
        if n in _LABEL_FIELDS:
            old = _old_tags_from_change(raw)
            new = _tags_from_change(raw)
            items.append(
                {
                    "field": "labels",
                    "fieldId": "labels",
                    "fromString": " ".join(old),
                    "toString": " ".join(new),
                }
            )
        elif n in _ASSIGNEE_FIELDS:
            old = identity_as_assignee(
                raw.get("oldValue") if isinstance(raw, dict) else None
            )
            new = _identity_from_change(raw)
            items.append(
                {
                    "field": "assignee",
                    "fieldId": "assignee",
                    "fromString": (old or {}).get("displayName") or "",
                    "to": (new or {}).get("key") or (new or {}).get("name") or "",
                    "toString": (new or {}).get("displayName")
                    or (new or {}).get("name")
                    or "",
                }
            )
        elif n in _STATE_FIELDS:
            old = ""
            new = ""
            if isinstance(raw, dict):
                old = str(raw.get("oldValue") or "")
                new = str(raw.get("newValue") or "")
            items.append(
                {
                    "field": "status",
                    "fieldId": "status",
                    "fromString": old,
                    "toString": new,
                }
            )
    return {"items": items}


@dataclass
class AzureWorkItemEvent:
    issue_key: str
    work_item_id: int
    project: str
    host: str
    collection_url: str
    web_url: str
    event_type: str
    change_kinds: List[str]
    rev: int
    issue: Dict[str, Any]
    changelog: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    plan_handoff: str = ""
    plan_comment: str = ""
    comment_body: str = ""

    def to_jira_event(self, *, is_update: bool, plan_handoff: str = "") -> Dict[str, Any]:
        handoff = (plan_handoff or self.plan_handoff or "").strip()
        kinds = list(self.change_kinds)
        if handoff:
            kinds = kinds or [f"plan-{handoff}"]
        event = {
            "webhookEvent": (
                "jira:issue_updated" if is_update else "jira:issue_created"
            ),
            "issue": self.issue,
            "changelog": self.changelog,
            "azure_workitem": True,
            "jira_event_id": (
                f"azure-wi-{self.project}-{self.work_item_id}-r{self.rev}-"
                f"{'-'.join(kinds) or 'created'}"
            ),
        }
        if handoff:
            event["plan_handoff"] = handoff
        if self.plan_comment:
            event["plan_comment"] = self.plan_comment
        return event


@dataclass
class WorkItemIntakeDecision:
    action: str
    reason: str
    is_update: bool = False
    plan_handoff: str = ""
    matched_assignee: bool = False
    matched_label: bool = False
    is_todo: bool = False
    is_done: bool = False
    will_process: bool = False


def evaluate_work_item_intake(
    issue: Dict[str, Any],
    *,
    state: Any = None,
    prev_status: str = "",
    required_labels: Optional[Iterable[str]] = None,
    trigger_needles: Optional[Iterable[str]] = None,
) -> WorkItemIntakeDecision:
    """Single-item copy of ``JiraPoller.poll_board`` + ``check_status_changes``."""
    fields = issue.get("fields") or {}
    labels = list(fields.get("labels") or [])
    assignee = fields.get("assignee")
    matched_assignee = assignee_matches_azure_bot(assignee, needles=trigger_needles)
    required = parse_trigger_labels(required_labels)
    matched_label = (
        issue_has_trigger_label(labels, required) if required else False
    )
    should_process = poller_triggers_on(
        assigned_to_bot=matched_assignee,
        labels=labels,
        required_labels=required,
    )
    is_todo = work_item_is_todo_column(fields)
    is_done = work_item_is_done(fields)
    board_ok = work_item_is_intake_column(fields)
    local_st = getattr(state, "status", None) if state is not None else None
    in_flight = local_st in {
        TaskStatus.PENDING,
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
    }
    waiting_plan = local_st == TaskStatus.PLAN_READY

    if in_flight:
        return WorkItemIntakeDecision(
            action="skip",
            reason=f"in-flight ({local_st.value if local_st else None})",
            matched_assignee=matched_assignee,
            matched_label=matched_label,
            is_todo=is_todo,
            is_done=is_done,
        )

    # Azure work items never hand off from tags. plan_ready waits for a
    # work-item comment: @mention /planRefactor or @mention /planExecute.
    if waiting_plan:
        return WorkItemIntakeDecision(
            action="skip",
            reason="plan_ready; waiting for /planExecute or /planRefactor",
            is_update=True,
            matched_assignee=matched_assignee,
            matched_label=matched_label,
            is_todo=is_todo,
            is_done=is_done,
        )

    if is_done:
        return WorkItemIntakeDecision(
            action="skip",
            reason="done",
            matched_assignee=matched_assignee,
            matched_label=matched_label,
            is_todo=is_todo,
            is_done=True,
        )

    if not board_ok:
        return WorkItemIntakeDecision(
            action="skip",
            reason="not todo or in progress",
            matched_assignee=matched_assignee,
            matched_label=matched_label,
            is_todo=is_todo,
            is_done=is_done,
        )

    # First sighting: To Do / In Progress and process-template equivalents.
    # Do **not** re-queue because the item moved In Progress → To Do while
    # still assigned (Jira To Do return does not apply on Azure Boards).
    if should_process and state is None:
        return WorkItemIntakeDecision(
            action="accept",
            reason="new",
            matched_assignee=matched_assignee,
            matched_label=matched_label,
            is_todo=is_todo,
            is_done=is_done,
            will_process=True,
        )

    if (
        should_process
        and local_st == TaskStatus.ERROR
        and bool((getattr(state, "metadata", None) or {}).get("requeue_eligible"))
    ):
        meta = getattr(state, "metadata", None) or {}
        fp = JiraPoller.issue_text_fingerprint(issue, light=False)
        last = meta.get("last_intake_fingerprint")
        if last is not None and last != fp:
            return WorkItemIntakeDecision(
                action="accept",
                reason="issue text changed",
                is_update=True,
                matched_assignee=matched_assignee,
                matched_label=matched_label,
                is_todo=is_todo,
                is_done=is_done,
                will_process=True,
            )

    return WorkItemIntakeDecision(
        action="skip",
        reason="not eligible",
        matched_assignee=matched_assignee,
        matched_label=matched_label,
        is_todo=is_todo,
        is_done=is_done,
    )


def parse_workitem_payload(payload: Any) -> Optional[AzureWorkItemEvent]:
    data = payload if isinstance(payload, dict) else {}
    resource = _as_dict(data.get("resource"))
    revision = _as_dict(resource.get("revision") or resource.get("workItem"))
    fields = _as_dict(revision.get("fields"))
    if not fields and isinstance(resource.get("fields"), dict):
        # Some 2022.2 payloads only send changed fields; merge newValues.
        merged: Dict[str, Any] = {}
        for key, raw in resource.get("fields").items():
            if isinstance(raw, dict) and "newValue" in raw:
                merged[str(key)] = raw.get("newValue")
            else:
                merged[str(key)] = raw
        fields = merged
    work_item_id = _work_item_id(resource, revision)
    if work_item_id <= 0:
        return None
    collection_url = _collection_from_payload(data, resource)
    project = _project_from_payload(data, fields, resource)
    host = _host_from_collection(collection_url)
    try:
        rev = int(resource.get("rev") or revision.get("rev") or 0)
    except (TypeError, ValueError):
        rev = 0
    kinds = classify_workitem_changes(changed_workitem_fields(resource))
    event_type = _s(data.get("eventType") or data.get("event_type")).lower()
    if event_type == "workitem.created":
        kinds = kinds or {"assignee"}
    issue = normalize_work_item(
        project=project or "project",
        work_item_id=work_item_id,
        fields=fields,
        host=host,
        collection_url=collection_url,
        web_url=_web_url(collection_url, project, work_item_id, resource),
        rev=rev,
    )
    return AzureWorkItemEvent(
        issue_key=str(issue.get("key") or ""),
        work_item_id=work_item_id,
        project=project,
        host=host,
        collection_url=collection_url,
        web_url=str(issue.get("self") or ""),
        event_type=event_type,
        change_kinds=sorted(kinds),
        rev=rev,
        issue=issue,
        changelog=changelog_from_resource(resource),
        raw=data,
    )


def extract_workitem_comment_text(payload: Any) -> str:
    """Plain text of the new work-item comment (2022.2 History or comment object)."""
    data = payload if isinstance(payload, dict) else {}
    resource = _as_dict(data.get("resource"))
    comment = _as_dict(resource.get("comment"))
    for raw in (
        comment.get("text"),
        comment.get("content"),
        comment.get("renderedText"),
    ):
        text = azure_html_to_text(raw)
        if text:
            return text
    fields = resource.get("fields")
    if isinstance(fields, dict):
        hist = fields.get("System.History") or fields.get("history")
        if isinstance(hist, dict):
            text = azure_html_to_text(hist.get("newValue"))
            if text:
                return text
        text = azure_html_to_text(hist)
        if text:
            return text
    revision = _as_dict(resource.get("revision") or resource.get("workItem"))
    rev_fields = _as_dict(revision.get("fields"))
    text = azure_html_to_text(
        rev_fields.get("System.History") or rev_fields.get("history")
    )
    return text


def format_workitem_plan_usage_note(bot_name: str = "yaver") -> str:
    """Work-item-only usage. Do not use on Jira, GitLab, or Azure PR comments."""
    from src.brand import USAGE_HEADING, USAGE_MARKER
    from src.operator_copy import USAGE_WORK_ITEM

    _ = bot_name
    return f"{USAGE_MARKER}\n{USAGE_HEADING}\n\n{USAGE_WORK_ITEM}"


def clear_workitem_comment_claims() -> None:
    _SEEN_COMMENTS.clear()


def _workitem_comment_claim_key(event: AzureWorkItemEvent) -> str:
    note = re.sub(r"\s+", " ", (event.comment_body or "").strip().lower())
    digest = hashlib.sha256(note.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{event.collection_url}|{event.work_item_id}|{digest}"


def claim_workitem_comment(event: AzureWorkItemEvent) -> bool:
    """True the first time we see this comment; False on the TFS twin hook."""
    now = time.monotonic()
    stale = [k for k, ts in _SEEN_COMMENTS.items() if now - ts > _SEEN_COMMENT_TTL_SEC]
    for key in stale:
        _SEEN_COMMENTS.pop(key, None)
    key = _workitem_comment_claim_key(event)
    if key in _SEEN_COMMENTS:
        return False
    _SEEN_COMMENTS[key] = now
    return True


def workitem_plan_command(note: str, bot_mentions: Iterable[str]) -> str:
    """Return ``execute`` / ``refactor`` when ``@bot /planExecute|planRefactor``."""
    from src.gitlab.mentions import note_has_slash_command

    names = list(bot_mentions or [])
    if note_has_slash_command(note, names, PLAN_EXECUTE_COMMAND):
        return HANDOFF_EXECUTE
    if note_has_slash_command(note, names, PLAN_REFACTOR_COMMAND):
        return HANDOFF_REFACTOR
    return ""


def strip_workitem_plan_command(note: str, bot_mentions: Iterable[str]) -> str:
    from src.azure.mentions import strip_azure_bot_mentions
    from src.gitlab.mentions import strip_slash_command

    text = strip_azure_bot_mentions(note, bot_mentions)
    text = strip_slash_command(text, PLAN_EXECUTE_COMMAND)
    text = strip_slash_command(text, PLAN_REFACTOR_COMMAND)
    return text.strip()


def is_azure_workitem_comment_event(
    payload: Any, headers: Optional[Dict[str, str]] = None
) -> bool:
    header_map = _header_map(headers)
    data = payload if isinstance(payload, dict) else {}
    event_name = _event_type(data, header_map)
    if event_name == "workitem.commented":
        return True
    if event_name != "workitem.updated":
        return False
    parsed = parse_workitem_payload(data)
    if parsed is None:
        return False
    kinds = set(parsed.change_kinds)
    return kinds == {"comment"}


def decide_azure_workitem_comment_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    bot_mentions: Optional[List[str]] = None,
) -> WebhookDecision:
    """Azure work-item comments only: ``@bot /planRefactor`` or ``/planExecute``."""
    from src.azure.mentions import (
        author_is_configured_bot,
        expand_guid_mentions,
        extract_mention_guids,
        mention_scan,
        note_is_other_agent_handoff,
        note_mentions_bot,
        other_agent_handoff_reason,
        parse_mention_list,
    )
    from src.brand import is_yaver_reply

    header_map = _header_map(headers)
    data = payload if isinstance(payload, dict) else {}
    event_name = _event_type(data, header_map)
    if not enabled:
        azure_info("workitem comment reject reason='azure webhook disabled'")
        return WebhookDecision(False, "azure webhook disabled")

    parsed = parse_workitem_payload(data)
    if parsed is None:
        azure_info("workitem comment reject reason='missing work item id'")
        return WebhookDecision(False, "missing work item id")

    note = extract_workitem_comment_text(data)
    if not note:
        azure_info(
            f"workitem comment reject reason='empty comment' "
            f"id={parsed.work_item_id}"
        )
        return WebhookDecision(False, "empty work item comment")
    if is_yaver_reply(note):
        azure_info(
            f"workitem comment reject reason='ignored bot reply' "
            f"id={parsed.work_item_id} preview={clip(note)!r}"
        )
        return WebhookDecision(False, "ignored bot reply")

    mentions = parse_mention_list(bot_mentions)
    resource = _as_dict(data.get("resource"))
    author = _as_dict(
        resource.get("revisedBy")
        or _as_dict(resource.get("comment")).get("author")
    )
    if author_is_configured_bot(author, mentions) or actor_is_pat_user(
        identity_as_assignee(author) or author,
        host=parsed.host,
        collection_url=parsed.collection_url,
    ):
        azure_info(
            f"workitem comment reject reason='ignored bot author' "
            f"id={parsed.work_item_id}"
        )
        return WebhookDecision(False, "ignored bot author")

    scan = mention_scan(note, mentions)
    if not mentions:
        azure_info(
            "workitem comment reject reason='no AZURE_TRIGGER_USER configured'"
        )
        return WebhookDecision(False, "no AZURE_TRIGGER_USER configured")

    pending_guids = extract_mention_guids(note)
    mentioned = note_mentions_bot(note, mentions)
    if not mentioned and pending_guids:
        try:
            from src.azure.client import AzureDevOpsClient

            extra = expand_guid_mentions(
                note,
                mentions,
                lookup=AzureDevOpsClient(
                    host=parsed.host,
                    collection_url=parsed.collection_url,
                ).identity_aliases,
            )
            mentioned = bool(extra)
        except Exception:
            mentioned = False
    if not mentioned:
        azure_info(
            f"workitem comment reject reason='bot not mentioned' "
            f"id={parsed.work_item_id} extracted={scan.get('extracted')} "
            f"preview={clip(note)!r}"
        )
        return WebhookDecision(False, "bot not mentioned")

    if note_is_other_agent_handoff(note, mentions):
        reason = other_agent_handoff_reason(note, mentions)
        azure_info(
            f"workitem comment reject reason={reason!r} id={parsed.work_item_id}"
        )
        return WebhookDecision(False, reason)

    command = workitem_plan_command(note, mentions)
    parsed.comment_body = note
    parsed.plan_comment = strip_workitem_plan_command(note, mentions)
    parsed.plan_handoff = command
    parsed.change_kinds = ["comment"]
    if not claim_workitem_comment(parsed):
        azure_info(
            f"workitem comment reject reason='duplicate comment event' "
            f"id={parsed.work_item_id} rev={parsed.rev}"
        )
        return WebhookDecision(False, "duplicate comment event")
    if not command:
        azure_info(
            f"workitem comment reject reason={PLAN_COMMAND_MISSING_REASON!r} "
            f"id={parsed.work_item_id} preview={clip(note)!r}"
        )
        return WebhookDecision(
            False,
            PLAN_COMMAND_MISSING_REASON,
            event=parsed,  # type: ignore[arg-type]
            usage_note=True,
        )
    azure_info(
        f"workitem comment accept key={parsed.issue_key} "
        f"id={parsed.work_item_id} cmd={command} "
        f"event={event_name} preview={clip(parsed.plan_comment)!r}"
    )
    return WebhookDecision(True, "accepted", event=parsed)  # type: ignore[arg-type]


def post_azure_workitem_usage_note(
    event: AzureWorkItemEvent, bot_name: str = ""
) -> bool:
    """Post the work-item plan-command usage note. Never used on a PR."""
    from src.azure.client import AzureDevOpsClient
    from src.brand import USAGE_HEADING, USAGE_HEADING_LEGACY, USAGE_MARKER

    project = (event.project or "").strip()
    if event.work_item_id <= 0:
        return False
    client = AzureDevOpsClient(
        host=event.host or "",
        collection_url=event.collection_url or "",
    )
    try:
        existing = client.get_work_item_comments(project, event.work_item_id)
    except Exception:
        existing = []
    for row in existing[-8:]:
        raw = ""
        if isinstance(row, dict):
            raw = str(row.get("text") or row.get("renderedText") or "")
        flat = azure_html_to_text(raw)
        if (
            USAGE_MARKER in raw
            or USAGE_HEADING in raw
            or USAGE_HEADING_LEGACY in raw
            or USAGE_HEADING in flat
            or USAGE_HEADING_LEGACY in flat
        ):
            azure_info(
                f"workitem usage note skipped already posted "
                f"id={event.work_item_id}"
            )
            return True
    posted = client.add_work_item_comment(
        project,
        event.work_item_id,
        format_workitem_plan_usage_note(bot_name),
    )
    azure_info(
        f"workitem usage note posted={bool(posted)} "
        f"id={event.work_item_id} project={project or '-'}"
    )
    return posted is not None


def workitem_revised_by(payload: Any) -> Optional[Dict[str, Any]]:
    """Actor on a 2022.2 work-item hook (``revisedBy`` / ``System.ChangedBy``)."""
    data = payload if isinstance(payload, dict) else {}
    resource = _as_dict(data.get("resource"))
    revision = _as_dict(resource.get("revision") or resource.get("workItem"))
    fields = _as_dict(revision.get("fields"))
    for raw in (
        resource.get("revisedBy"),
        revision.get("revisedBy"),
        fields.get("System.ChangedBy"),
        fields.get("System.AuthorizedAs"),
        _as_dict(resource.get("comment")).get("revisedBy"),
        _as_dict(resource.get("comment")).get("author"),
    ):
        ident = identity_as_assignee(raw)
        if ident:
            return ident
    return None


def actor_is_pat_user(
    actor: Optional[Dict[str, Any]],
    *,
    host: str = "",
    collection_url: str = "",
) -> bool:
    """True when the hook actor is the collection PAT identity (our own writes)."""
    if not actor:
        return False
    ident = fetch_bot_identity(host=host, collection_url=collection_url)
    if not ident:
        return False
    actor_id = normalize_guid(
        str(actor.get("key") or actor.get("accountId") or actor.get("id") or "")
    )
    pat_id = normalize_guid(str(ident.get("id") or ""))
    if actor_id and pat_id and actor_id == pat_id:
        return True
    actor_keys = {
        identity_key(x) or normalize_mention(str(x))
        for x in (
            actor.get("uniqueName"),
            actor.get("name"),
            actor.get("displayName"),
            actor.get("emailAddress"),
        )
        if x
    }
    actor_keys.discard("")
    pat_keys = {
        identity_key(n) or normalize_mention(str(n))
        for n in (ident.get("names") or [])
        if n
    }
    pat_keys.discard("")
    return bool(actor_keys and pat_keys and actor_keys.intersection(pat_keys))


def decide_azure_workitem_webhook(
    payload: Any,
    *,
    headers: Optional[Dict[str, str]] = None,
    enabled: bool = True,
) -> WebhookDecision:
    """Accept created/updated only for Assigned To. Comments use the comment path."""
    header_map = _header_map(headers)
    data = payload if isinstance(payload, dict) else {}
    event_name = _event_type(data, header_map)

    if not enabled:
        azure_info("workitem reject reason='azure webhook disabled'")
        return WebhookDecision(False, "azure webhook disabled")
    if event_name and event_name not in AZURE_WORKITEM_EVENTS:
        azure_info(f"workitem reject reason='ignored event' event={event_name!r}")
        return WebhookDecision(False, f"ignored event {event_name!r}")

    parsed = parse_workitem_payload(data)
    if parsed is None:
        azure_info("workitem reject reason='missing work item id'")
        return WebhookDecision(False, "missing work item id")

    actor = workitem_revised_by(data)
    if actor_is_pat_user(
        actor, host=parsed.host, collection_url=parsed.collection_url
    ):
        azure_info(
            f"workitem reject reason='ignored PAT update' "
            f"id={parsed.work_item_id} rev={parsed.rev} "
            f"actor={actor.get('displayName') or actor.get('name') or '-'}"
        )
        return WebhookDecision(False, "ignored PAT update")

    if event_name == "workitem.updated" and not parsed.change_kinds:
        azure_info(
            f"workitem reject reason='ignored field noise' "
            f"id={parsed.work_item_id} rev={parsed.rev}"
        )
        return WebhookDecision(False, "ignored work item field noise")

    if event_name == "workitem.commented" or set(parsed.change_kinds) == {
        "comment"
    }:
        azure_info(
            f"workitem reject reason='work item comment' "
            f"id={parsed.work_item_id} event={event_name!r}"
        )
        return WebhookDecision(False, "work item comment")

    interesting = set(parsed.change_kinds)
    if event_name == "workitem.updated" and not interesting.intersection(
        _INTAKE_CHANGE_KINDS
    ):
        azure_info(
            f"workitem reject reason='no assignee change' "
            f"id={parsed.work_item_id} kinds={parsed.change_kinds}"
        )
        return WebhookDecision(False, "no assignee change")

    azure_info(
        f"workitem accept key={parsed.issue_key} id={parsed.work_item_id} "
        f"project={parsed.project or '-'} kinds={parsed.change_kinds} "
        f"event={event_name} preview={clip(str((parsed.issue.get('fields') or {}).get('summary') or ''))!r}"
    )
    # Reuse WebhookDecision.event for the parsed work-item event.
    return WebhookDecision(True, "accepted", event=parsed)  # type: ignore[arg-type]


def fetch_work_item_issue(
    *,
    host: str,
    project: str,
    work_item_id: int,
    collection_url: str = "",
    client: Any = None,
) -> Optional[Dict[str, Any]]:
    """GET a work item and return the Jira-shaped issue dict."""
    from src.azure.client import AzureDevOpsClient
    from src.azure_connection import remembered_azure_collection

    try:
        iid = int(work_item_id)
    except (TypeError, ValueError):
        return None
    if iid <= 0:
        return None
    collection = (collection_url or "").strip()
    if not collection and host:
        collection = remembered_azure_collection(host)
    azure_info(
        f"wit get start id={iid} project={project or '-'} "
        f"collection={collection or host or '-'}"
    )
    ado = client or AzureDevOpsClient(
        host=host or collection or None,
        collection_url=collection or None,
    )
    raw = ado.get_work_item(project, iid)
    if not raw or not isinstance(raw.get("fields"), dict):
        azure_warning(
            f"wit get miss id={iid} project={project or '-'} "
            f"collection={collection or host or '-'}"
        )
        return None
    fields = raw["fields"]
    wtype = str(fields.get("System.WorkItemType") or "")
    state_name = str(fields.get("System.State") or "")
    category = ""
    if wtype:
        for row in ado.get_work_item_type_states(project, wtype):
            if str(row.get("name") or "").strip().lower() == state_name.strip().lower():
                category = _status_category(state_name, str(row.get("category") or ""))
                break
    links = raw.get("_links") if isinstance(raw.get("_links"), dict) else {}
    html_link = ""
    if isinstance(links.get("html"), dict):
        html_link = str(links["html"].get("href") or "")
    return normalize_work_item(
        project=project,
        work_item_id=iid,
        fields=fields,
        host=host,
        collection_url=collection or ado.collection_url or ado.api_base,
        web_url=html_link,
        state_category=category,
        rev=int(raw.get("rev") or 0),
    )


def lookup_azure_work_item(
    *,
    host: str,
    project: str,
    work_item_id: int,
    collection_url: str = "",
    state: Any = None,
    required_labels: Optional[Iterable[str]] = None,
    trigger_needles: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    azure_info(
        f"wit lookup start id={work_item_id} project={project or '-'} "
        f"collection={collection_url or host or '-'}"
    )
    issue = fetch_work_item_issue(
        host=host,
        project=project,
        work_item_id=work_item_id,
        collection_url=collection_url,
    )
    if issue is None:
        azure_warning(
            f"wit lookup fail id={work_item_id} project={project or '-'} "
            f"collection={collection_url or host or '-'}"
        )
        return {
            "ok": False,
            "error": (
                "Work item not found. Check collection URL, id, and that the "
                "PAT has Work Items (Read)."
            ),
            "host": host,
            "project": project,
            "work_item_id": work_item_id,
        }
    prev = ""
    if state is not None:
        meta = getattr(state, "metadata", None) or {}
        prev = str(meta.get("last_board_status") or "")
    view = lookup_work_item_view(
        issue,
        state=state,
        prev_status=prev,
        required_labels=required_labels,
        trigger_needles=trigger_needles,
    )
    view["ok"] = True
    azure_info(
        f"wit lookup ok key={view.get('issue_key')} id={work_item_id} "
        f"state={view.get('state') or '-'} todo={view.get('is_todo')} "
        f"assignee={view.get('matched_assignee')} "
        f"will_process={view.get('will_process')} "
        f"action={view.get('action')} reason={view.get('reason')!r}"
    )
    return view


def lookup_work_item_view(
    issue: Dict[str, Any],
    *,
    state: Any = None,
    prev_status: str = "",
    required_labels: Optional[Iterable[str]] = None,
    trigger_needles: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Settings lookup DTO — same eligibility flags as the poll snapshot."""
    fields = issue.get("fields") or {}
    decision = evaluate_work_item_intake(
        issue,
        state=state,
        prev_status=prev_status,
        required_labels=required_labels,
        trigger_needles=trigger_needles,
    )
    assignee = fields.get("assignee") or {}
    coords = (issue.get("azure") or {}) if isinstance(issue.get("azure"), dict) else {}
    return {
        "ok": True,
        "issue_key": issue.get("key") or "",
        "work_item_id": int(coords.get("work_item_id") or issue.get("id") or 0),
        "project": coords.get("project") or "",
        "host": coords.get("host") or "",
        "collection_url": coords.get("collection_url") or "",
        "web_url": coords.get("web_url") or issue.get("self") or "",
        "summary": fields.get("summary") or "",
        "description": fields.get("description") or "",
        "state": ((fields.get("status") or {}).get("name") or ""),
        "state_category": (
            ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
            or ""
        ),
        "assignee": (assignee.get("displayName") or assignee.get("name") or None),
        "labels": list(fields.get("labels") or []),
        "work_item_type": ((fields.get("issuetype") or {}).get("name") or ""),
        "matched_assignee": decision.matched_assignee,
        "matched_label": decision.matched_label,
        "is_todo": decision.is_todo,
        "is_done": decision.is_done,
        "will_process": decision.will_process,
        "action": decision.action,
        "reason": decision.reason,
        "plan_handoff": decision.plan_handoff or None,
        "local_status": (
            state.status.value if state is not None and getattr(state, "status", None) else None
        ),
    }


__all__ = [
    "AZURE_WORKITEM_EVENTS",
    "PLAN_COMMAND_MISSING_REASON",
    "PLAN_EXECUTE_COMMAND",
    "PLAN_REFACTOR_COMMAND",
    "AzureWorkItemEvent",
    "WorkItemIntakeDecision",
    "actor_is_pat_user",
    "assignee_matches_azure_bot",
    "azure_html_to_text",
    "changelog_from_resource",
    "claim_workitem_comment",
    "clear_workitem_comment_claims",
    "classify_workitem_changes",
    "decide_azure_workitem_comment_webhook",
    "decide_azure_workitem_webhook",
    "evaluate_work_item_intake",
    "find_work_item_key_by_git",
    "find_work_item_key_by_id",
    "work_item_is_done",
    "work_item_is_in_progress_column",
    "work_item_is_intake_column",
    "work_item_is_todo_column",
    "extract_workitem_comment_text",
    "format_workitem_plan_usage_note",
    "is_azure_workitem_comment_event",
    "post_azure_workitem_usage_note",
    "fetch_work_item_issue",
    "identity_as_assignee",
    "lookup_azure_work_item",
    "is_azure_work_item_key",
    "join_azure_tags",
    "lookup_work_item_view",
    "normalize_work_item",
    "parse_azure_tags",
    "parse_workitem_payload",
    "remember_work_item",
    "tracker_metadata",
    "work_item_coords",
    "work_item_fields_to_jira",
    "workitem_plan_command",
]
