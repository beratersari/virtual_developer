"""Named groups of git remotes for a dashboard multi-repo job.

A Jira ticket still names one Repository line. Extra repositories are
chosen on the dashboard and stored on the schedule, not in that line.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence, Tuple

from src.issue_git_spec import _looks_like_git_url, _normalize_repo_url

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


def normalize_repository_refs(
    primary: str,
    source_branch: str = "",
    target_branch: str = "",
    extras: Any = None,
) -> List[Dict[str, str]]:
    """Primary first, then extras. Each row has its own source and target.

    A string extra copies the primary branches. A dict may set
    ``source_branch`` and ``target_branch``. A later dict of a URL that
    is already listed fills only branches that are still blank, so an
    empty synthetic primary does not hide the real row. A branch that
    is already set stays. Fewer than two URLs returns an empty list so
    a one-repo job stays the normal path.
    """
    source = (source_branch or "").strip()
    target = (target_branch or "").strip()
    raws: List[Any] = [{"url": primary, "source_branch": source, "target_branch": target}]
    if isinstance(extras, str):
        raws.append(extras)
    elif isinstance(extras, list):
        raws.extend(extras)
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw in raws:
        if isinstance(raw, dict):
            url = _normalize_repo_url(
                str(raw.get("url") or raw.get("repository_url") or "")
            )
            row_source = str(raw.get("source_branch") or source).strip()
            row_target = str(raw.get("target_branch") or target).strip()
            own_source = str(raw.get("source_branch") or "").strip()
            own_target = str(raw.get("target_branch") or "").strip()
        else:
            url = _normalize_repo_url(str(raw or ""))
            row_source = source
            row_target = target
            own_source = ""
            own_target = ""
        if not url or not _looks_like_git_url(url):
            continue
        if len(url) > 500:
            url = url[:500]
        key = _url_key(url)
        if key in seen:
            if isinstance(raw, dict):
                for existing in out:
                    if _url_key(existing["url"]) != key:
                        continue
                    if not existing["source_branch"] and own_source:
                        existing["source_branch"] = own_source[:255]
                    if not existing["target_branch"] and own_target:
                        existing["target_branch"] = own_target[:255]
                    break
            continue
        seen.add(key)
        out.append(
            {
                "url": url,
                "source_branch": row_source[:255],
                "target_branch": row_target[:255],
            }
        )
    if len(out) < 2:
        return []
    return out


def repository_count(meta: Any) -> int:
    """How many repositories a job will clone. One when the list is empty."""
    if not isinstance(meta, dict):
        return 1
    count = 0
    refs = meta.get("repository_refs")
    if isinstance(refs, list):
        refs_n = 0
        for row in refs:
            if isinstance(row, dict) and str(
                row.get("url") or row.get("repository_url") or ""
            ).strip():
                refs_n += 1
            elif isinstance(row, str) and row.strip():
                refs_n += 1
        count = max(count, refs_n)
    urls = meta.get("repository_urls")
    if isinstance(urls, list):
        count = max(count, sum(1 for url in urls if str(url or "").strip()))
    return max(1, count)


def same_repository(left: str, right: str) -> bool:
    """True when both strings are the same git remote."""
    a = _url_key(left)
    b = _url_key(right)
    return bool(a and b and a == b)


def merge_missing_ref_branches(
    stored: Sequence[Dict[str, Any]],
    described: Any,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Fill blank source or target from another row of the same repository.

    A branch that is already set is kept. ``described`` may be dicts
    (``url`` / ``source_branch`` / ``target_branch``) or
    ``(url, source, target)`` tuples. Returns the rows and whether any
    blank field was filled.
    """
    donors: Dict[str, Tuple[str, str]] = {}
    items = described if isinstance(described, (list, tuple)) else []
    for item in items:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("repository_url") or "")
            donor_source = str(item.get("source_branch") or "").strip()
            donor_target = str(item.get("target_branch") or "").strip()
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            url = str(item[0] or "")
            donor_source = str(item[1] or "").strip()
            donor_target = str(item[2] or "").strip()
        else:
            continue
        key = _url_key(url)
        if not key:
            continue
        prev = donors.get(key)
        if prev is None:
            donors[key] = (donor_source, donor_target)
        else:
            donors[key] = (prev[0] or donor_source, prev[1] or donor_target)
    out: List[Dict[str, Any]] = []
    changed = False
    for row in stored:
        if not isinstance(row, dict):
            continue
        copied = dict(row)
        url = str(copied.get("url") or copied.get("repository_url") or "").strip()
        source = str(copied.get("source_branch") or "").strip()
        target = str(copied.get("target_branch") or "").strip()
        donor = donors.get(_url_key(url))
        if donor:
            if not source and donor[0]:
                source = donor[0][:255]
                changed = True
            if not target and donor[1]:
                target = donor[1][:255]
                changed = True
        copied["url"] = url
        copied["source_branch"] = source
        copied["target_branch"] = target
        out.append(copied)
    return out, changed


def repository_sets_to_json(raw: Any) -> str:
    items = parse_repository_sets(raw)
    if not items:
        return "[]"
    return json.dumps(items, ensure_ascii=False)
