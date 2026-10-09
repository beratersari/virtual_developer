"""Compact Analytics snapshot the release site reads from this install.

The numbers are the same totals the Analytics page builds from this
install's job history. The report has counts and labels only. It does not
include issue text, tokens, or a repository username. The version check
does not send this report.
"""

from __future__ import annotations

from typing import Any, Optional

from src.dashboard.analytics import build_analytics
from src.state.job_store import JobStore

_ROW_LIMIT = 12
_LABEL_LIMIT = 160


def release_usage_report(store: Optional[JobStore] = None) -> dict[str, Any]:
    """All-time jobs, merge requests, and the Analytics breakdowns."""
    report = build_analytics(period="all", store=store)
    totals = report.totals
    ours = report.reviews.ours
    contributed = report.reviews.contributed
    facets = report.facets or {}
    return {
        "period": "all",
        "jobs": int(totals.jobs or 0),
        "completed": int(totals.completed or 0),
        "error": int(totals.error or 0),
        "cancelled": int(totals.cancelled or 0),
        "plan_ready": int(totals.plan_ready or 0),
        "in_flight": int(totals.in_flight or 0),
        "merge_requests": int(ours.total or 0) + int(contributed.total or 0),
        "opened": int(ours.opened or 0) + int(contributed.opened or 0),
        "merged": int(ours.merged or 0) + int(contributed.merged or 0),
        "closed": int(ours.closed or 0) + int(contributed.closed or 0),
        "ours": _review_group(ours),
        "contributed": _review_group(contributed),
        "categories": _named_rows(report.categories),
        "sources": _named_rows(report.sources),
        "models": _named_rows(report.models),
        "backends": _named_rows(report.backends),
        "agents": _named_rows(report.agents),
        "repositories": _facet_rows(facets.get("repository") or []),
        "statuses": _facet_rows(facets.get("status") or []),
    }


def _review_group(group: Any) -> dict[str, int]:
    return {
        "opened": int(getattr(group, "opened", 0) or 0),
        "merged": int(getattr(group, "merged", 0) or 0),
        "closed": int(getattr(group, "closed", 0) or 0),
        "total": int(getattr(group, "total", 0) or 0),
    }


def _label(value: object) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    text = text.replace("<", "").replace(">", "").strip()
    if len(text) > _LABEL_LIMIT:
        text = text[:_LABEL_LIMIT].rstrip()
    return text


def _named_rows(items: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items or []:
        if len(rows) >= _ROW_LIMIT:
            break
        label = _label(getattr(item, "label", "") or getattr(item, "id", ""))
        if not label:
            continue
        rows.append(
            {
                "label": label,
                "jobs": int(getattr(item, "jobs", 0) or 0),
                "completed": int(getattr(item, "completed", 0) or 0),
                "error": int(getattr(item, "error", 0) or 0),
                "cancelled": int(getattr(item, "cancelled", 0) or 0),
                "plan_ready": int(getattr(item, "plan_ready", 0) or 0),
                "in_flight": int(getattr(item, "in_flight", 0) or 0),
            }
        )
    return rows


def _facet_rows(items: Any) -> list[dict[str, int | str]]:
    rows: list[dict[str, int | str]] = []
    for item in items or []:
        if len(rows) >= _ROW_LIMIT:
            break
        label = _label(getattr(item, "label", "") or getattr(item, "id", ""))
        if not label:
            continue
        rows.append({"label": label, "jobs": int(getattr(item, "jobs", 0) or 0)})
    return rows
