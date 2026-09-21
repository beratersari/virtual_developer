"""Job analytics for the ops dashboard (aggregation only; SPA renders DTOs)."""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from src.dashboard.schemas import (
    AnalyticsFacet,
    AnalyticsModelPoint,
    AnalyticsNamedCount,
    AnalyticsPoint,
    AnalyticsRange,
    AnalyticsResponse,
    AnalyticsReviewCounts,
    AnalyticsReviewItem,
    AnalyticsReviews,
    AnalyticsReviewsList,
)
from src.state.job_store import JobStore, job_store as default_job_store

_COMPLETED = frozenset({"completed"})
_ERROR = frozenset({"error", "unknown"})
_CANCELLED = frozenset({"cancelled", "canceled", "superseded"})
_PLAN_READY = frozenset({"plan_ready"})
_IN_FLIGHT = frozenset({"pending", "planning", "executing", "running"})
_UNSET = "(unset)"

_RANGE_PRESETS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "1y": timedelta(days=365),
}

_MAX_BUCKETS = 400
_TOP_MODELS = 8
# Slightly under the SPA Analytics GET budget (60s) so the handler can
# answer 504 instead of leaving the browser on a spinner until abort.
ANALYTICS_TIMEOUT_SECONDS = 55.0


class AnalyticsCancelled(Exception):
    """Client disconnected (or the operator aborted) mid-aggregation."""


def _throw_if_cancelled(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise AnalyticsCancelled()

_CATEGORY_LABELS = {
    "plan": "Plan",
    "build": "Build",
    "test": "Test",
    "review": "Review",
    "other": "Other",
}


def job_category(workflow_type: str) -> str:
    wt = (workflow_type or "").strip().lower().replace("_", "-")
    if wt in {"planning", "plan"}:
        return "plan"
    if wt in {"testing", "test"}:
        return "test"
    if wt in {"review", "gitlab-review", "azure-review"}:
        return "review"
    if wt in {"execution", "build", "direct", "gitlab-mr", "azure-pr"}:
        return "build"
    return "other"


def _csv_set(raw: Optional[str]) -> set[str]:
    if not raw:
        return set()
    return {p.strip().lower() for p in str(raw).split(",") if p.strip()}


def _parse_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _floor(dt: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    if bucket == "week":
        day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    if bucket == "month":
        return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _step(bucket: str) -> timedelta:
    if bucket == "hour":
        return timedelta(hours=1)
    if bucket == "week":
        return timedelta(days=7)
    if bucket == "month":
        return timedelta(days=32)
    return timedelta(days=1)


def _add_bucket(dt: datetime, bucket: str) -> datetime:
    if bucket == "month":
        year = dt.year + (1 if dt.month == 12 else 0)
        month = 1 if dt.month == 12 else dt.month + 1
        return dt.replace(year=year, month=month, day=1)
    return dt + _step(bucket)


def _label(dt: datetime, bucket: str) -> str:
    if bucket == "hour":
        return dt.strftime("%m-%d %H:%M")
    if bucket == "week":
        return dt.strftime("%Y-%m-%d")
    if bucket == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y-%m-%d")


def _auto_bucket(start: datetime, end: datetime) -> str:
    span = max(0.0, (end - start).total_seconds())
    if span <= 2 * 86400:
        return "hour"
    if span <= 90 * 86400:
        return "day"
    if span <= 730 * 86400:
        return "week"
    return "month"


def _coarsen(start: datetime, end: datetime, bucket: str) -> str:
    order = ["hour", "day", "week", "month"]
    idx = order.index(bucket) if bucket in order else 1
    while idx < len(order):
        cur = order[idx]
        n = 0
        t = _floor(start, cur)
        last = _floor(end, cur)
        while t <= last and n <= _MAX_BUCKETS:
            nxt = _add_bucket(t, cur)
            if nxt <= t:
                break
            t = nxt
            n += 1
        if n <= _MAX_BUCKETS:
            return cur
        idx += 1
    return "month"


def _bucket_keys(start: datetime, end: datetime, bucket: str) -> List[datetime]:
    origin = _floor(start, bucket)
    last = _floor(end, bucket)
    keys: List[datetime] = []
    t = origin
    while t <= last and len(keys) < _MAX_BUCKETS:
        keys.append(t)
        nxt = _add_bucket(t, bucket)
        if nxt <= t:
            break
        t = nxt
    return keys


def _outcome(status: str) -> str:
    st = (status or "").strip().lower()
    if st in _COMPLETED:
        return "completed"
    if st in _ERROR:
        return "error"
    if st in _CANCELLED:
        return "cancelled"
    if st in _PLAN_READY:
        return "plan_ready"
    if st in _IN_FLIGHT:
        return "in_flight"
    return "other"


def _job_when(job: Dict[str, Any]) -> Optional[datetime]:
    return _parse_ts(job.get("started_at") or job.get("created_at") or job.get("updated_at"))


def _normalize_repo(raw: str) -> str:
    """Same Git remote with or without ``.git`` (and trailing slash) is one repo."""
    text = (raw or "").strip().rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4].rstrip("/")
    if "://" not in text:
        return text
    try:
        parsed = urlparse(text)
    except Exception:
        return text
    host = (parsed.hostname or "").lower()
    if not host:
        return text
    path = (parsed.path or "").rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4].rstrip("/")
    netloc = host
    if parsed.port and parsed.port not in (80, 443):
        netloc = f"{host}:{parsed.port}"
    scheme = (parsed.scheme or "https").lower()
    return f"{scheme}://{netloc}{path}"


def _repo_key(job: Dict[str, Any]) -> str:
    return _normalize_repo(str(job.get("repository_url") or ""))


_MERGED_REVIEW = frozenset({"merged", "completed"})
_CLOSED_REVIEW = frozenset({"closed", "abandoned"})
_REVIEW_RANK = {"opened": 1, "closed": 2, "merged": 3}


def _review_identity(job: Dict[str, Any]) -> str:
    """Stable id for one GitLab MR or Azure PR (URL first, then host ids)."""
    url = str(job.get("merge_request_url") or "").strip().rstrip("/").lower()
    if url:
        return f"url:{url}"
    project = str(job.get("gitlab_project") or "").strip().lower()
    try:
        iid = int(job.get("gitlab_mr_iid") or 0)
    except (TypeError, ValueError):
        iid = 0
    if project and iid > 0:
        return f"gl:{project}:{iid}"
    azure_project = str(job.get("azure_project") or "").strip().lower()
    try:
        pr_id = int(job.get("azure_pr_id") or 0)
    except (TypeError, ValueError):
        pr_id = 0
    if azure_project and pr_id > 0:
        return f"az:{azure_project}:{pr_id}"
    return ""


def _review_bucket(state: Any) -> str:
    raw = str(state or "").strip().lower()
    if raw in _MERGED_REVIEW:
        return "merged"
    if raw in _CLOSED_REVIEW:
        return "closed"
    return "opened"


def _mr_origin_from_job(job: Dict[str, Any]) -> str:
    """ours = Yaver opened the MR (Jira / Azure Boards). contributed = comment on an existing MR/PR."""
    raw = str(job.get("source") or "jira").strip().lower().replace("_", "-") or "jira"
    if raw in {"gitlab", "gitlab-mr"}:
        return "contributed"
    if raw in {"azure", "azure-pr"}:
        return "contributed"
    return "ours"


def _unique_review_rows(
    jobs: Iterable[Tuple[datetime, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """One row per MR/PR. Review + build on the same URL stay one row."""
    best: Dict[str, Dict[str, Any]] = {}
    for when, job in jobs:
        ident = _review_identity(job)
        if not ident:
            continue
        bucket = _review_bucket(job.get("merge_request_state"))
        url = str(job.get("merge_request_url") or "").strip()
        issue_key = str(job.get("issue_key") or "").strip()
        title = str(job.get("summary") or job.get("title") or "").strip()
        project = str(job.get("gitlab_project") or "").strip()
        try:
            iid = int(job.get("gitlab_mr_iid") or 0) or None
        except (TypeError, ValueError):
            iid = None
        azure_project = str(job.get("azure_project") or "").strip()
        try:
            pr_id = int(job.get("azure_pr_id") or 0) or None
        except (TypeError, ValueError):
            pr_id = None
        origin = _mr_origin_from_job(job)
        row = best.get(ident)
        if row is None:
            best[ident] = {
                "url": url,
                "state": bucket,
                "origin": origin,
                "issue_key": issue_key,
                "title": title,
                "jobs": 1,
                "gitlab_project": project,
                "gitlab_mr_iid": iid,
                "azure_project": azure_project,
                "azure_pr_id": pr_id,
                "when": when,
            }
            continue
        row["jobs"] = int(row.get("jobs") or 0) + 1
        if origin == "ours":
            row["origin"] = "ours"
        if url and not row.get("url"):
            row["url"] = url
        if project and not row.get("gitlab_project"):
            row["gitlab_project"] = project
        if iid and not row.get("gitlab_mr_iid"):
            row["gitlab_mr_iid"] = iid
        if azure_project and not row.get("azure_project"):
            row["azure_project"] = azure_project
        if pr_id and not row.get("azure_pr_id"):
            row["azure_pr_id"] = pr_id
        if _REVIEW_RANK[bucket] >= _REVIEW_RANK[str(row.get("state") or "opened")]:
            row["state"] = bucket
        if when >= row.get("when"):
            row["when"] = when
            if issue_key:
                row["issue_key"] = issue_key
            if title:
                row["title"] = title
    rows = list(best.values())
    rows.sort(key=lambda r: r.get("when") or datetime.min, reverse=True)
    return rows


def _counts_for_origin(rows: List[Dict[str, Any]], origin: str) -> AnalyticsReviewCounts:
    picked = [r for r in rows if r.get("origin") == origin]
    return AnalyticsReviewCounts(
        opened=sum(1 for r in picked if r.get("state") == "opened"),
        merged=sum(1 for r in picked if r.get("state") == "merged"),
        closed=sum(1 for r in picked if r.get("state") == "closed"),
        total=len(picked),
    )


def _review_counts(jobs: Iterable[Dict[str, Any]]) -> AnalyticsReviews:
    """Unique MRs/PRs. Same URL is one row; a Jira/work-item job marks it ours."""
    rows = _unique_review_rows((datetime.min, job) for job in jobs)
    return AnalyticsReviews(
        ours=_counts_for_origin(rows, "ours"),
        contributed=_counts_for_origin(rows, "contributed"),
    )


def list_analytics_reviews(
    *,
    state: str = "",
    origin: str = "",
    page: int = 1,
    page_size: int = 25,
    period: str = "30d",
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    category: str = "",
    source: str = "",
    model: str = "",
    backend: str = "",
    agent: str = "",
    repository: str = "",
    issue_key: str = "",
    q: str = "",
    store: Optional[JobStore] = None,
    cancel: Optional[threading.Event] = None,
) -> AnalyticsReviewsList:
    """Unique MR/PR rows for the Analytics cards (same filters and identity)."""
    want = str(state or "").strip().lower()
    if want in {"", "all"}:
        want = "all"
    elif want in {"open", "opened"}:
        want = "opened"
    elif want not in {"merged", "closed"}:
        raise ValueError("state must be opened, merged, closed, or all")
    want_origin = str(origin or "").strip().lower()
    if want_origin in {"", "all"}:
        want_origin = "all"
    elif want_origin in {"ours", "opened_by_us", "owned"}:
        want_origin = "ours"
    elif want_origin in {"contributed", "contrib"}:
        want_origin = "contributed"
    else:
        raise ValueError("origin must be ours, contributed, or all")
    matched, _dated, _in_range, _start, _end, _period_key, _now = _load_matched(
        period=period,
        date_from=date_from,
        date_to=date_to,
        status=status,
        category=category,
        source=source,
        model=model,
        backend=backend,
        agent=agent,
        repository=repository,
        issue_key=issue_key,
        q=q,
        store=store,
        cancel=cancel,
    )
    rows = _unique_review_rows(matched)
    if want != "all":
        rows = [r for r in rows if r.get("state") == want]
    if want_origin != "all":
        rows = [r for r in rows if r.get("origin") == want_origin]
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        size = 25
    size = max(1, min(size, 100))
    try:
        pg = int(page)
    except (TypeError, ValueError):
        pg = 1
    pg = max(1, pg)
    total = len(rows)
    start_i = (pg - 1) * size
    page_rows = rows[start_i : start_i + size]
    items = [
        AnalyticsReviewItem(
            url=str(r.get("url") or ""),
            state=str(r.get("state") or "opened"),
            origin=str(r.get("origin") or "contributed"),
            issue_key=str(r.get("issue_key") or ""),
            title=str(r.get("title") or ""),
            jobs=int(r.get("jobs") or 0),
            gitlab_project=str(r.get("gitlab_project") or ""),
            gitlab_mr_iid=r.get("gitlab_mr_iid"),
            azure_project=str(r.get("azure_project") or ""),
            azure_pr_id=r.get("azure_pr_id"),
        )
        for r in page_rows
    ]
    return AnalyticsReviewsList(
        state=want,
        origin=want_origin,
        items=items,
        total=total,
        page=pg,
        page_size=size,
    )


def _model_id(job: Dict[str, Any]) -> str:
    """Named model, or ``(unset)`` so breakdown shares still sum to 100%."""
    return str(job.get("model") or "").strip() or _UNSET


def _backend_id(job: Dict[str, Any]) -> str:
    raw = str(job.get("backend") or "").strip().lower()
    return raw or "(unset)"


def _source_id(job: Dict[str, Any]) -> str:
    raw = str(job.get("source") or "jira").strip().lower() or "jira"
    if raw in {"gitlab_mr", "gitlab-mr"}:
        return "gitlab"
    if raw in {"azure_pr", "azure-pr", "azure_workitem"}:
        return "azure"
    return raw


def _agent_id(job: Dict[str, Any]) -> str:
    return str(job.get("agent") or "").strip() or "(unset)"


def _in_set(value: str, allowed: set[str]) -> bool:
    if not allowed:
        return True
    return (value or "").strip().lower() in allowed


def _matches_text(job: Dict[str, Any], needle: str) -> bool:
    if not needle:
        return True
    n = needle.lower()
    blob = " ".join(
        [
            str(job.get("issue_key") or ""),
            str(job.get("summary") or ""),
            str(job.get("description") or ""),
            str(job.get("repository_url") or ""),
            str(job.get("agent") or ""),
            str(job.get("model") or ""),
        ]
    ).lower()
    return n in blob


def _empty_counts() -> Dict[str, int]:
    return {
        "total": 0,
        "completed": 0,
        "error": 0,
        "cancelled": 0,
        "plan_ready": 0,
        "in_flight": 0,
        "other": 0,
    }


def _bump(counts: Dict[str, int], outcome: str) -> None:
    counts["total"] += 1
    if outcome in counts:
        counts[outcome] += 1
    else:
        counts["other"] += 1


def _named(
    key: str,
    counts: Dict[str, int],
    *,
    label: Optional[str] = None,
    total_jobs: int = 0,
) -> AnalyticsNamedCount:
    jobs = int(counts.get("total") or 0)
    share = round((100.0 * jobs / total_jobs), 1) if total_jobs > 0 else 0.0
    return AnalyticsNamedCount(
        id=key,
        label=label or key,
        jobs=jobs,
        completed=int(counts.get("completed") or 0),
        error=int(counts.get("error") or 0),
        cancelled=int(counts.get("cancelled") or 0),
        plan_ready=int(counts.get("plan_ready") or 0),
        in_flight=int(counts.get("in_flight") or 0),
        share=share,
    )


def _facet(rows: Iterable[Tuple[str, str, int]]) -> List[AnalyticsFacet]:
    out = [
        AnalyticsFacet(id=i, label=lab or i, jobs=n)
        for i, lab, n in rows
        if i
    ]
    out.sort(key=lambda f: (-f.jobs, f.label.lower()))
    return out


def _load_matched(
    *,
    period: str = "30d",
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    category: str = "",
    source: str = "",
    model: str = "",
    backend: str = "",
    agent: str = "",
    repository: str = "",
    issue_key: str = "",
    q: str = "",
    store: Optional[JobStore] = None,
    cancel: Optional[threading.Event] = None,
) -> Tuple[
    List[Tuple[datetime, Dict[str, Any]]],
    List[Tuple[datetime, Dict[str, Any]]],
    List[Tuple[datetime, Dict[str, Any]]],
    datetime,
    datetime,
    str,
    datetime,
]:
    """dated, in_range, matched, start, end, period_key, now — shared by charts and MR list."""
    _throw_if_cancelled(cancel)
    js = store or default_job_store
    now = datetime.now().replace(microsecond=0)
    want_status = _csv_set(status)
    want_cat = _csv_set(category)
    want_src = _csv_set(source)
    want_model = _csv_set(model)
    want_backend = _csv_set(backend)
    want_agent = {p.strip().lower() for p in (agent or "").split(",") if p.strip()}
    want_repo = {
        _normalize_repo(p).lower()
        for p in str(repository or "").split(",")
        if p.strip()
    }
    want_keys = {p.strip().upper() for p in (issue_key or "").split(",") if p.strip()}
    search = (q or "").strip()

    jobs = js.iter_jobs() if hasattr(js, "iter_jobs") else js.list_jobs(limit=5000)
    _throw_if_cancelled(cancel)
    dated: List[Tuple[datetime, Dict[str, Any]]] = []
    for i, job in enumerate(jobs):
        if i % 32 == 0:
            _throw_if_cancelled(cancel)
        when = _job_when(job)
        if when is None:
            continue
        dated.append((when, job))

    period_key = (period or "30d").strip().lower() or "30d"
    start = _parse_ts(date_from)
    end = _parse_ts(date_to) or now
    if start is None:
        if period_key == "all":
            start = min((w for w, _ in dated), default=now - timedelta(days=30))
        else:
            delta = _RANGE_PRESETS.get(period_key, _RANGE_PRESETS["30d"])
            start = now - delta
    if end < start:
        start, end = end, start

    in_range = [(w, j) for w, j in dated if start <= w <= end]
    _throw_if_cancelled(cancel)
    matched: List[Tuple[datetime, Dict[str, Any]]] = []
    for i, (when, job) in enumerate(in_range):
        if i % 32 == 0:
            _throw_if_cancelled(cancel)
        st = str(job.get("status") or "").strip().lower()
        cat = job_category(str(job.get("workflow_type") or ""))
        src = _source_id(job)
        mid = _model_id(job)
        bid = _backend_id(job)
        ag = _agent_id(job)
        ik = str(job.get("issue_key") or "").strip().upper()
        if want_status and st not in want_status and _outcome(st) not in want_status:
            continue
        if not _in_set(cat, want_cat):
            continue
        if not _in_set(src, want_src):
            continue
        if want_model and mid.lower() not in want_model:
            continue
        if want_backend and bid.lower() not in want_backend:
            continue
        if want_agent and ag.lower() not in want_agent:
            continue
        if want_repo:
            rkl = _repo_key(job).lower()
            if rkl not in want_repo and not any(n in rkl for n in want_repo):
                continue
        if want_keys and ik not in want_keys:
            continue
        if not _matches_text(job, search):
            continue
        matched.append((when, job))
    return matched, dated, in_range, start, end, period_key, now


def build_analytics(
    *,
    period: str = "30d",
    bucket: str = "auto",
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    category: str = "",
    source: str = "",
    model: str = "",
    backend: str = "",
    agent: str = "",
    repository: str = "",
    issue_key: str = "",
    q: str = "",
    store: Optional[JobStore] = None,
    cancel: Optional[threading.Event] = None,
) -> AnalyticsResponse:
    """Aggregate stored jobs for the Analytics page.

    ``cancel`` is set when the HTTP client disconnects or the handler
    hits ``ANALYTICS_TIMEOUT_SECONDS``. The walk stops instead of stacking
    behind a superseded GET.
    """
    matched, dated, in_range, start, end, period_key, now = _load_matched(
        period=period,
        date_from=date_from,
        date_to=date_to,
        status=status,
        category=category,
        source=source,
        model=model,
        backend=backend,
        agent=agent,
        repository=repository,
        issue_key=issue_key,
        q=q,
        store=store,
        cancel=cancel,
    )

    bucket_key = (bucket or "auto").strip().lower() or "auto"
    if bucket_key not in {"auto", "hour", "day", "week", "month"}:
        bucket_key = "auto"
    if bucket_key == "auto":
        bucket_key = _auto_bucket(start, end)
    bucket_key = _coarsen(start, end, bucket_key)

    _throw_if_cancelled(cancel)
    facet_cat: Dict[str, int] = defaultdict(int)
    facet_src: Dict[str, int] = defaultdict(int)
    facet_model: Dict[str, int] = defaultdict(int)
    facet_backend: Dict[str, int] = defaultdict(int)
    facet_agent: Dict[str, int] = defaultdict(int)
    facet_status: Dict[str, int] = defaultdict(int)
    facet_repo: Dict[str, int] = defaultdict(int)
    for i, (_w, job) in enumerate(in_range):
        if i % 32 == 0:
            _throw_if_cancelled(cancel)
        facet_cat[job_category(str(job.get("workflow_type") or ""))] += 1
        facet_src[_source_id(job)] += 1
        mid = _model_id(job)
        facet_model[mid] += 1
        facet_backend[_backend_id(job)] += 1
        facet_agent[_agent_id(job)] += 1
        st = str(job.get("status") or "unknown").strip().lower() or "unknown"
        facet_status[st] += 1
        repo = _repo_key(job)
        if repo:
            facet_repo[repo] += 1

    keys = _bucket_keys(start, end, bucket_key)
    origin = keys[0] if keys else _floor(start, bucket_key)
    last = keys[-1] if keys else _floor(end, bucket_key)

    series_map: Dict[datetime, Dict[str, int]] = {k: _empty_counts() for k in keys}
    totals = _empty_counts()
    by_cat: Dict[str, Dict[str, int]] = defaultdict(_empty_counts)
    by_src: Dict[str, Dict[str, int]] = defaultdict(_empty_counts)
    by_model: Dict[str, Dict[str, int]] = defaultdict(_empty_counts)
    by_backend: Dict[str, Dict[str, int]] = defaultdict(_empty_counts)
    by_agent: Dict[str, Dict[str, int]] = defaultdict(_empty_counts)
    model_series: Dict[datetime, Dict[str, int]] = {k: {} for k in keys}

    for i, (when, job) in enumerate(matched):
        if i % 32 == 0:
            _throw_if_cancelled(cancel)
        out = _outcome(str(job.get("status") or ""))
        _bump(totals, out)
        cat = job_category(str(job.get("workflow_type") or ""))
        _bump(by_cat[cat], out)
        _bump(by_src[_source_id(job)], out)
        mid = _model_id(job)
        _bump(by_model[mid], out)
        _bump(by_backend[_backend_id(job)], out)
        _bump(by_agent[_agent_id(job)], out)
        b = _floor(when, bucket_key)
        if b < origin:
            b = origin
        if b > last:
            b = last
        if b not in series_map:
            series_map[b] = _empty_counts()
        _bump(series_map[b], out)
        if mid != _UNSET:
            if b not in model_series:
                model_series[b] = {}
            model_series[b][mid] = int(model_series[b].get(mid) or 0) + 1

    page_total = int(totals.get("total") or 0)
    named_models = [(k, v) for k, v in by_model.items() if k != _UNSET]
    top_models = sorted(
        named_models, key=lambda kv: (-int(kv[1].get("total") or 0), kv[0])
    )[:_TOP_MODELS]
    top_ids = [k for k, _ in top_models]

    points: List[AnalyticsPoint] = []
    model_points: List[AnalyticsModelPoint] = []
    for k in keys:
        c = series_map.get(k) or _empty_counts()
        points.append(
            AnalyticsPoint(
                t=k.isoformat(timespec="seconds"),
                label=_label(k, bucket_key),
                total=int(c.get("total") or 0),
                completed=int(c.get("completed") or 0),
                error=int(c.get("error") or 0),
                cancelled=int(c.get("cancelled") or 0),
                plan_ready=int(c.get("plan_ready") or 0),
                in_flight=int(c.get("in_flight") or 0),
            )
        )
        raw = model_series.get(k) or {}
        counts: Dict[str, int] = {}
        other = 0
        for mid, n in raw.items():
            if mid in top_ids:
                counts[mid] = n
            else:
                other += n
        if other:
            counts["(other)"] = other
        model_points.append(
            AnalyticsModelPoint(
                t=k.isoformat(timespec="seconds"),
                label=_label(k, bucket_key),
                counts=counts,
            )
        )

    def _sort_named(
        items: Dict[str, Dict[str, int]], labels: Optional[Dict[str, str]] = None
    ) -> List[AnalyticsNamedCount]:
        rows = [
            _named(k, v, label=(labels or {}).get(k) or k, total_jobs=page_total)
            for k, v in items.items()
        ]
        rows.sort(key=lambda r: (r.id == _UNSET, -r.jobs, r.label.lower()))
        return rows

    return AnalyticsResponse(
        range=AnalyticsRange(
            period=period_key,
            bucket=bucket_key,
            start=start.isoformat(timespec="seconds"),
            end=end.isoformat(timespec="seconds"),
        ),
        totals=_named("all", totals, label="All", total_jobs=page_total),
        reviews=_review_counts(j for _, j in matched),
        series=points,
        models=_sort_named(by_model),
        model_series=model_points,
        model_keys=top_ids
        + (["(other)"] if any("(other)" in p.counts for p in model_points) else []),
        categories=_sort_named(by_cat, _CATEGORY_LABELS),
        sources=_sort_named(by_src),
        backends=_sort_named(by_backend),
        agents=_sort_named(by_agent),
        facets={
            "status": _facet((k, k, n) for k, n in facet_status.items()),
            "category": _facet(
                (k, _CATEGORY_LABELS.get(k, k), n) for k, n in facet_cat.items()
            ),
            "source": _facet((k, k, n) for k, n in facet_src.items()),
            "model": _facet((k, k, n) for k, n in facet_model.items()),
            "backend": _facet((k, k, n) for k, n in facet_backend.items()),
            "agent": _facet((k, k, n) for k, n in facet_agent.items()),
            "repository": _facet((k, k, n) for k, n in facet_repo.items()),
        },
        matched=len(matched),
        scanned=len(dated),
        in_range=len(in_range),
        server_time=now.isoformat(timespec="seconds"),
    )
