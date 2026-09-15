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
        from src.azure_connection import probe_azure_connection

        host = self.host or self.collection_url
        if not host:
            return None
        try:
            probe = probe_azure_connection(host, pat=self.client.pat)
        except Exception:
            return None
        user = probe.get("user") if isinstance(probe, dict) else None
        if not isinstance(user, dict):
            return None
        name = str(user.get("name") or user.get("username") or "").strip()
        if not name and not user.get("id"):
            return None
        username = str(user.get("username") or name)
        return {
            "displayName": name or username,
            "name": username,
            "key": user.get("id"),
            "emailAddress": username if "@" in username else "",
        }

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
