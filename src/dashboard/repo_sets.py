"""Named groups of git remotes for a dashboard multi-repo job.

A Jira ticket still names one Repository line. Extra repositories are
chosen on the dashboard and stored on the schedule, not in that line.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from src.issue_git_spec import _looks_like_git_url, _normalize_repo_url

MAX_REPOS_IN_JOB = 12
MAX_REPOSITORY_SETS = 20


def _url_key(url: str) -> str:
    """Same identity as a git remote: host/path, no scheme, no .git."""
    raw = (url or "").strip().strip("<>").strip("`").rstrip("/")
    if raw.lower().endswith(".git"):
        raw = raw[:-4]
    if raw.startswith("git@"):
        rest = raw[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            return f"{host.lower()}/{path.strip('/').lower()}"
        return rest.lower()
    if "://" in raw:
        rest = raw.split("://", 1)[1]
        rest = rest.split("@")[-1]
        host, _, path = rest.partition("/")
        return f"{host.split(':')[0].lower()}/{path.strip('/').lower()}".rstrip("/")
    return raw.lower().replace("\\", "/")


def normalize_repository_urls(primary: str, extras: Any = None) -> List[str]:
    """Primary first, then extras, de-duplicated. Invalid URLs are dropped."""
    raws: List[Any] = [primary]
    if isinstance(extras, str):
        raws.append(extras)
    elif isinstance(extras, list):
        raws.extend(extras)
    out: List[str] = []
    seen: set[str] = set()
    for raw in raws:
        url = _normalize_repo_url(str(raw or ""))
        if not url or not _looks_like_git_url(url):
            continue
        if len(url) > 500:
            url = url[:500]
        key = _url_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= MAX_REPOS_IN_JOB:
            break
    return out


def parse_repository_sets(raw: Any) -> List[Dict[str, Any]]:
    """Normalize JSON into ``{name, repositories}`` with at least two URLs."""
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
            return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    seen_names: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:80]
        if not name:
            continue
        key = name.lower()
        if key in seen_names:
            continue
        urls = normalize_repository_urls("", entry.get("repositories") or [])
        if len(urls) < 2:
            continue
        seen_names.add(key)
        out.append({"name": name, "repositories": urls})
        if len(out) >= MAX_REPOSITORY_SETS:
            break
    return out


def repository_sets_to_json(raw: Any) -> str:
    items = parse_repository_sets(raw)
    if not items:
        return "[]"
    return json.dumps(items, ensure_ascii=False)
