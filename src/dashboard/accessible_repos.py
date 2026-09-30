"""List git repositories the saved GitLab and Azure PATs can read."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from src.config import save_runtime_settings, settings
from src.dashboard.project_repos import (
    parse_project_repositories,
    project_repositories_to_json,
)
from src.logger import logger


def _gitlab_api_base(host: str) -> str:
    text = (host or "").strip()
    if "://" in text:
        parsed = urlparse(text)
        name = parsed.netloc or parsed.path
        scheme = parsed.scheme or "https"
        return f"{scheme}://{name}/api/v4"
    return f"https://{text}/api/v4"


def _branch_name(raw: Any) -> str:
    text = str(raw or "").strip()
    if text.lower().startswith("refs/heads/"):
        text = text[len("refs/heads/") :]
    return text[:255]


def list_gitlab_repositories() -> tuple[List[Dict[str, str]], List[str]]:
    """Membership projects for every saved GitLab host PAT."""
    mapping: Dict[str, str] = {}
    if hasattr(settings, "gitlab_host_pat_map"):
        try:
            mapping = dict(settings.gitlab_host_pat_map() or {})
        except Exception:
            mapping = {}
    found: List[Dict[str, str]] = []
    errors: List[str] = []
    seen: set[str] = set()
    for host, pat in mapping.items():
        token = str(pat or "").strip()
        name = str(host or "").strip()
        if not token or not name:
            continue
        rows, error = _gitlab_membership_projects(name, token)
        if error:
            errors.append(error)
        for row in rows:
            url = str(row.get("http_url_to_repo") or row.get("web_url") or "").strip()
            if not url:
                continue
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            label = str(
                row.get("path_with_namespace") or row.get("name") or ""
            ).strip()
            found.append(
                {
                    "label": label[:80],
                    "url": url,
                    "target_branch": _branch_name(row.get("default_branch")),
                    "source_branch": "",
                }
            )
    return found, errors


def _gitlab_membership_projects(
    host: str, pat: str
) -> tuple[List[Dict[str, Any]], str]:
    base = _gitlab_api_base(host)
    headers = {"PRIVATE-TOKEN": pat, "Accept": "application/json"}
    timeout = httpx.Timeout(30.0, connect=10.0)
    rows: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=timeout, verify=False, headers=headers) as client:
            page = 1
            while True:
                resp = client.get(
                    f"{base}/projects",
                    params={
                        "membership": "true",
                        "simple": "true",
                        "per_page": 100,
                        "page": page,
                        "order_by": "path",
                        "sort": "asc",
                    },
                )
                if resp.status_code == 401:
                    return [], f"GitLab {host}: unauthorized"
                if resp.status_code == 403:
                    return [], f"GitLab {host}: token cannot list projects"
                if resp.status_code != 200:
                    return rows, f"GitLab {host}: HTTP {resp.status_code}"
                batch = resp.json() if resp.content else []
                if not isinstance(batch, list) or not batch:
                    break
                rows.extend(item for item in batch if isinstance(item, dict))
                if len(batch) < 100:
                    break
                page += 1
    except Exception as exc:
        logger.warning(f"GitLab project list failed host={host}: {exc}")
        return rows, f"GitLab {host}: {exc}"
    return rows, ""


def list_azure_repositories() -> tuple[List[Dict[str, str]], List[str]]:
    """Git repositories in every saved Azure DevOps Server collection."""
    from src.azure.client import AzureDevOpsClient
    from src.azure.urls import parse_tfs_collection_url

    collections: List[str] = []
    if hasattr(settings, "azure_collection_pat_map"):
        try:
            collections = [
                url
                for url, pat in (settings.azure_collection_pat_map() or {}).items()
                if str(pat or "").strip() and parse_tfs_collection_url(str(url))
            ]
        except Exception:
            collections = []
    if not collections and (getattr(settings, "azure_pat", "") or "").strip():
        if hasattr(settings, "azure_collection_url_list"):
            collections = list(settings.azure_collection_url_list() or [])
    found: List[Dict[str, str]] = []
    errors: List[str] = []
    seen: set[str] = set()
    for raw in collections:
        collection = parse_tfs_collection_url(str(raw)) or str(raw).strip()
        if not collection:
            continue
        client = AzureDevOpsClient(collection_url=collection)
        if not client.pat:
            errors.append(f"Azure {collection}: no PAT for this collection")
            continue
        try:
            rows = client.list_git_repositories()
        except Exception as exc:
            logger.warning(f"Azure repository list failed collection={collection}: {exc}")
            errors.append(f"Azure {collection}: {exc}")
            continue
        if not rows and client.last_error:
            errors.append(f"Azure {collection}: {client.last_error}")
        for row in rows:
            url = str(row.get("url") or "").strip()
            key = url.rstrip("/").lower()
            if not url or key in seen:
                continue
            seen.add(key)
            found.append(row)
    return found, errors


def import_accessible_repositories(
    existing: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Merge token-visible repos into saved projects and persist them.

    Existing rows win on the same URL, so a hand-edited label or target
    branch stays. New repositories are appended.
    """
    current = parse_project_repositories(
        existing
        if existing is not None
        else getattr(settings, "project_repositories", "")
    )
    gitlab_rows, gitlab_errors = list_gitlab_repositories()
    azure_rows, azure_errors = list_azure_repositories()
    seen = {row["url"].rstrip("/").lower() for row in current}
    added = 0
    for row in gitlab_rows + azure_rows:
        key = str(row.get("url") or "").rstrip("/").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        current.append(
            {
                "label": str(row.get("label") or "")[:80],
                "url": str(row.get("url") or ""),
                "target_branch": _branch_name(row.get("target_branch")),
                "source_branch": "",
            }
        )
        added += 1
    saved = parse_project_repositories(current)
    encoded = project_repositories_to_json(saved)
    settings.project_repositories = encoded
    save_runtime_settings({"project_repositories": encoded})
    return {
        "ok": not (gitlab_errors or azure_errors) or bool(saved),
        "added": added,
        "gitlab": len(gitlab_rows),
        "azure": len(azure_rows),
        "errors": [*gitlab_errors, *azure_errors],
        "project_repositories": saved,
    }
