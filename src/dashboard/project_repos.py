"""Saved git remotes for the schedule New-issue form (dashboard runtime)."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import urlparse

from src.issue_git_spec import _looks_like_git_url, _normalize_branch, _normalize_repo_url

MAX_PROJECT_REPOS = 40


def label_from_repo_url(url: str) -> str:
    """Short label: group/repo from https://host/group/repo.git or git@host:group/repo.git."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("git@"):
        path = raw.split(":", 1)[-1]
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        path = parsed.path or raw
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path[:80].strip()
    return path or raw[:80]


def _as_item(raw: Any) -> Dict[str, str] | None:
    if isinstance(raw, str):
        url = _normalize_repo_url(raw)
        label = ""
        target = ""
        source = ""
    elif isinstance(raw, dict):
        url = _normalize_repo_url(str(raw.get("url") or raw.get("repository_url") or ""))
        label = str(raw.get("label") or "").strip()[:80]
        target = _normalize_branch(str(raw.get("target_branch") or ""))
        source = _normalize_branch(str(raw.get("source_branch") or ""))
    else:
        return None
    if not url or not _looks_like_git_url(url):
        return None
    if len(url) > 500:
        url = url[:500]
    return {
        "label": label or label_from_repo_url(url),
        "url": url,
        "target_branch": target[:255],
        "source_branch": source[:255],
    }


def parse_project_repositories(raw: Any) -> List[Dict[str, str]]:
    """Normalize JSON string / list into unique {label,url,target_branch,source_branch}."""
    if raw is None or raw == "":
        return []
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Single URL pasted into the env/runtime field
            item = _as_item(text)
            return [item] if item else []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for entry in data:
        item = _as_item(entry)
        if not item:
            continue
        key = item["url"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= MAX_PROJECT_REPOS:
            break
    return out


def project_repositories_to_json(raw: Any) -> str:
    items = parse_project_repositories(raw)
    if not items:
        return "[]"
    return json.dumps(items, ensure_ascii=False)
