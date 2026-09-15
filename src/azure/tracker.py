"""JiraClient-shaped adapter for Azure DevOps Server work items."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.azure.client import AzureDevOpsClient
from src.azure.keys import is_azure_work_item_key, parse_azure_work_item_key
from src.azure.log import azure_info, azure_warning
from src.azure.workitems import (
    azure_html_to_text,
    identity_as_assignee,
    join_azure_tags,
    normalize_work_item,
    parse_azure_tags,
    remember_work_item,
    work_item_coords,
)


_IN_PROGRESS_NAMES = (
    "active",
    "in progress",
    "doing",
    "committed",
    "wip",
    "devam ediyor",
)


def _usable_assign_name(raw: Any) -> str:
    """Drop TFS descriptors / empty values — those 400 AssignedTo."""
    text = str(raw or "").strip()
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("microsoft.teamfoundation."):
        return ""
    if ";" in text and " " not in text:
        return ""
    return text


def fetch_pat_myself(
    *,
    host: str = "",
    collection_url: str = "",
    pat: str = "",
    client: Optional[AzureDevOpsClient] = None,
) -> Optional[Dict[str, Any]]:
    """PAT identity in a Jira-shaped dict (connectionData, not Settings probe)."""
    from src.azure.identity import fetch_bot_identity

    ident = fetch_bot_identity(
        host=host,
        collection_url=collection_url,
        pat=pat or (client.pat if client is not None else ""),
    )
    if not ident:
        return None
    names: List[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = _usable_assign_name(raw)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            names.append(text)

    for item in ident.get("names") or []:
        _add(item)
    uid = str(ident.get("id") or "").strip()
    if uid and client is not None:
        try:
            for extra in client.identity_aliases(uid) or []:
                _add(extra)
        except Exception:
            pass
    unique = ""
    for item in names:
        if "\\" in item or "@" in item:
            unique = item
            break
    display = names[0] if names else ""
    primary = unique or display or uid
    if not primary:
        return None
    email = next((n for n in names if "@" in n), "")
    return {
        "displayName": display or primary,
        "name": primary,
        "key": uid,
        "accountId": uid,
        "emailAddress": email,
        "uniqueName": unique or primary,
        "names": names,
    }


def pat_assign_candidates(me: Optional[Dict[str, Any]]) -> List[Any]:
    """AssignedTo values TFS 2022 accepts, uniqueName first then GUID / IdentityRef."""
    if not isinstance(me, dict):
        return []
    seen: set[str] = set()
    out: List[Any] = []

    def add(raw: Any) -> None:
        if isinstance(raw, dict):
            key = f"obj:{raw.get('id') or ''}:{raw.get('uniqueName') or ''}"
            if key in seen or not (raw.get("id") or raw.get("uniqueName")):
                return
            seen.add(key)
            out.append(raw)
            return
        text = _usable_assign_name(raw)
        if not text:
            return
        key = f"s:{text.lower()}"
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    names = [str(n).strip() for n in (me.get("names") or []) if str(n).strip()]
    unique = _usable_assign_name(me.get("uniqueName"))
    display = _usable_assign_name(me.get("displayName"))
    name = _usable_assign_name(me.get("name"))
    email = _usable_assign_name(me.get("emailAddress"))
    uid = str(me.get("key") or me.get("accountId") or "").strip()
    for item in (unique, email, name, *names):
        if "\\" in item or "@" in item:
            add(item)
    add(display)
    add(name)
    for item in names:
        add(item)
    if display and (unique or email):
        add(f"{display} <{unique or email}>")
    add(uid)
    if uid:
        add({"id": uid})
        ref: Dict[str, Any] = {"id": uid}
        if display:
            ref["displayName"] = display
        if unique:
            ref["uniqueName"] = unique
        add(ref)
    return out


def azure_tracker_for(
    issue_key: str,
    state: Any = None,
    *,
    coords: Optional[Dict[str, Any]] = None,
) -> Optional["AzureWorkItemTracker"]:
    key = (issue_key or "").strip()
    if not key:
        return None
    row = coords or work_item_coords(key, state)
    if row is None and is_azure_work_item_key(key):
        slug, wid = parse_azure_work_item_key(key)
        if wid > 0:
            row = {
                "host": "",
                "collection_url": "",
                "project": slug,
                "work_item_id": wid,
            }
            meta = getattr(state, "metadata", None) or {}
            if isinstance(meta, dict):
                row["host"] = str(meta.get("azure_host") or "")
                row["collection_url"] = str(meta.get("azure_collection_url") or "")
                row["project"] = str(meta.get("azure_project") or slug)
    if not row or int(row.get("work_item_id") or 0) <= 0:
        if not is_azure_work_item_key(key):
            meta = getattr(state, "metadata", None) or {}
            if str(meta.get("source") or "").strip().lower() != "azure_workitem":
                return None
        return None
    remember_work_item(key, row)
    return AzureWorkItemTracker(
        issue_key=key,
        host=str(row.get("host") or ""),
        collection_url=str(row.get("collection_url") or ""),
        project=str(row.get("project") or ""),
        work_item_id=int(row.get("work_item_id") or 0),
        work_item_type=str(row.get("work_item_type") or ""),
    )


class AzureWorkItemTracker:
    """Subset of ``JiraClient`` used by the processor and reporter."""

    def __init__(
        self,
        *,
        issue_key: str,
        host: str,
        collection_url: str,
        project: str,
        work_item_id: int,
        work_item_type: str = "",
        client: Optional[AzureDevOpsClient] = None,
    ) -> None:
        self.issue_key = issue_key
        self.host = host
        self.collection_url = collection_url
        self.project = project
        self.work_item_id = int(work_item_id)
        self.work_item_type = work_item_type
        self.client = client or AzureDevOpsClient(
            host=host or None,
            collection_url=collection_url or None,
        )

    def _load(self) -> Optional[Dict[str, Any]]:
        return self.client.get_work_item(self.project, self.work_item_id)

    def get_issue(
        self, issue_key: str, fields: Optional[List[str]] = None, **_kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        raw = self._load()
        if not raw:
            return None
        wit_fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
        links = raw.get("_links") if isinstance(raw.get("_links"), dict) else {}
        html_link = links.get("html") if isinstance(links.get("html"), dict) else {}
        issue = normalize_work_item(
            project=self.project,
            work_item_id=self.work_item_id,
            fields=wit_fields,
            host=self.host,
            collection_url=self.collection_url,
            web_url=str(html_link.get("href") or ""),
            rev=int(raw.get("rev") or 0),
        )
        if fields:
            keep = {str(x) for x in fields}
            slim = {
                name: value
                for name, value in (issue.get("fields") or {}).items()
                if name in keep
            }
            issue["fields"] = slim
        return issue

    def update_issue(
        self,
        issue_key: str,
        fields: Optional[Dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> bool:
        payload = dict(fields or {})
        desc = payload.get("description")
        if desc is None:
            return True
        posted = self.client.update_work_item_fields(
            self.project,
            self.work_item_id,
            {"System.Description": desc},
        )
        return posted is not None

    def add_comment(self, issue_key: str, body: str) -> Optional[Dict[str, Any]]:
        posted = self.client.add_work_item_comment(
            self.project, self.work_item_id, body
        )
        if not posted:
            return None
        ident = posted.get("id") or posted.get("rev") or "1"
        return {"id": ident, **posted}

    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        rows = self.client.get_work_item_comments(self.project, self.work_item_id)
        out: List[Dict[str, Any]] = []
        for row in rows:
            author = identity_as_assignee(
                row.get("createdBy") or row.get("modifiedBy")
            )
            text = azure_html_to_text(row.get("text") or row.get("renderedText") or "")
            out.append(
                {
                    "id": row.get("id"),
                    "body": text,
                    "author": author or {},
                    "created": row.get("createdDate"),
                }
            )
        return out

    def get_myself(self) -> Optional[Dict[str, Any]]:
        return fetch_pat_myself(
            host=self.host,
            collection_url=self.collection_url,
            pat=self.client.pat,
            client=self.client,
        )

    def assign_issue(
        self,
        issue_key: str,
        name: Any = "",
        *,
        account_id: str = "",
        **_kwargs: Any,
    ) -> bool:
        value: Any = name if name not in (None, "") else account_id
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return False
        elif isinstance(value, dict):
            if not (value.get("id") or value.get("uniqueName") or value.get("displayName")):
                return False
        else:
            return False
        posted = self.client.update_work_item_fields(
            self.project,
            self.work_item_id,
            {"System.AssignedTo": value},
        )
        return posted is not None

    def assign_to_pat_user(self, issue_key: str) -> bool:
        """Assign the work item to the identity behind the collection PAT."""
        me = self.get_myself()
        candidates = pat_assign_candidates(me)
        if not candidates:
            azure_warning(f"{self.issue_key}: cannot assign PAT user (identity empty)")
            return False
        raw = self._load()
        fields = raw.get("fields") if isinstance(raw, dict) else {}
        current = identity_as_assignee(
            fields.get("System.AssignedTo") if isinstance(fields, dict) else None
        )
        if current:
            current_vals = {
                str(x or "").strip().lower()
                for x in (
                    current.get("uniqueName"),
                    current.get("name"),
                    current.get("displayName"),
                    current.get("emailAddress"),
                    current.get("key"),
                    current.get("accountId"),
                )
                if str(x or "").strip()
            }
            for cand in candidates:
                if isinstance(cand, dict):
                    cid = str(cand.get("id") or "").strip().lower()
                    if cid and cid in current_vals:
                        azure_info(f"{self.issue_key}: already assigned to PAT user")
                        return True
                    continue
                if str(cand).strip().lower() in current_vals:
                    azure_info(f"{self.issue_key}: already assigned to PAT user")
                    return True
        last_label = ""
        for cand in candidates:
            last_label = (
                str(cand.get("uniqueName") or cand.get("id") or cand)
                if isinstance(cand, dict)
                else str(cand)
            )
            if self.assign_issue(issue_key, cand):
                azure_info(f"{self.issue_key}: assigned to PAT user {last_label}")
                return True
        azure_warning(
            f"{self.issue_key}: could not assign PAT user "
            f"(tried {len(candidates)} identities, last={last_label or '-'})"
        )
        return False

    def _current_tags(self) -> Optional[List[str]]:
        raw = self._load()
        if not raw or not isinstance(raw.get("fields"), dict):
            return None
        return parse_azure_tags(raw["fields"].get("System.Tags"))

    def add_labels(self, issue_key: str, labels: List[str]) -> bool:
        if not labels:
            return True
        current = self._current_tags()
        if current is None:
            azure_warning(f"{self.issue_key}: cannot add tags; get_work_item failed")
            return False
        merged = list(dict.fromkeys([*current, *[str(x) for x in labels if str(x).strip()]]))
        posted = self.client.update_work_item_fields(
            self.project, self.work_item_id, {"System.Tags": join_azure_tags(merged)}
        )
        return posted is not None

    def remove_labels(self, issue_key: str, labels: List[str]) -> bool:
        drop = {str(x).strip().lower() for x in (labels or []) if str(x).strip()}
        if not drop:
            return True
        current = self._current_tags()
        if current is None:
            azure_warning(f"{self.issue_key}: cannot remove tags; get_work_item failed")
            return False
        kept = [x for x in current if x.strip().lower() not in drop]
        posted = self.client.update_work_item_fields(
            self.project, self.work_item_id, {"System.Tags": join_azure_tags(kept)}
        )
        return posted is not None

    def replace_label(self, issue_key: str, old: str, new: str) -> bool:
        add = (new or "").strip()
        drop = (old or "").strip()
        if not add and not drop:
            return True
        current = self._current_tags()
        if current is None:
            azure_warning(f"{self.issue_key}: cannot replace tag; get_work_item failed")
            return False
        drop_l = drop.lower()
        kept = [x for x in current if x.strip().lower() != drop_l]
        if add and add.lower() not in {x.strip().lower() for x in kept}:
            kept.append(add)
        posted = self.client.update_work_item_fields(
            self.project, self.work_item_id, {"System.Tags": join_azure_tags(kept)}
        )
        return posted is not None

    def transition_to_in_progress(self, issue_key: str) -> bool:
        raw = self._load()
        if not raw or not isinstance(raw.get("fields"), dict):
            azure_warning(f"{self.issue_key}: cannot read work item for state change")
            return False
        fields = raw["fields"]
        current = str(fields.get("System.State") or "").strip()
        current_l = current.lower()
        if current_l in _IN_PROGRESS_NAMES:
            azure_info(f"{self.issue_key}: already {current}")
            return True
        wtype = self.work_item_type or str(fields.get("System.WorkItemType") or "")
        target = ""
        if wtype:
            for row in self.client.get_work_item_type_states(self.project, wtype):
                cat = str(row.get("category") or "").replace(" ", "").lower()
                name = str(row.get("name") or "").strip()
                if cat == "inprogress" and name:
                    target = name
                    break
        if not target:
            for hint in ("Active", "In Progress", "Doing", "Committed"):
                target = hint
                break
        posted = self.client.update_work_item_fields(
            self.project, self.work_item_id, {"System.State": target}
        )
        if posted:
            azure_info(f"{self.issue_key}: state {current or '-'} → {target}")
            return True
        azure_warning(
            f"{self.issue_key}: could not set System.State={target!r} "
            f"(current={current or '-'})"
        )
        return False
