"""Dashboard document titles.

Copying the address bar uses ``<title>`` and ``og:title``. The names match
``web/src/util/pageTitle.ts``.
"""

from __future__ import annotations

import html
import re
from typing import Optional
from urllib.parse import parse_qs, unquote

_TITLE_RE = re.compile(r"<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
_OG_RE = re.compile(
    r"<meta\b[^>]*property=[\"']og:title[\"'][^>]*>",
    re.IGNORECASE,
)
_PAGE_RE = re.compile(r"^[1-9]\d*$")

_JOB_LIST = {
    "in-flight": "Jobs - In flight",
    "active": "Jobs - In flight",
    "queue": "Jobs - Queue",
    "error": "Jobs - Error",
    "completed": "Jobs - Completed",
    "cancelled": "Jobs - Cancelled",
    "plan-ready": "Jobs - Plan ready",
}
_JOB_TABS = {
    "plan": "Plan",
    "prompt": "Prompt",
    "transcript": "Transcript",
    "output": "Output",
    "daemon": "Daemon",
}
_SETTINGS = {
    "jira": "Settings - Jira",
    "gitlab": "Settings - GitLab",
    "azure": "Settings - Azure",
    "projects": "Settings - Projects",
    "agent": "Settings - Agent",
    "model": "Settings - Agent",
    "runtime": "Settings - Runtime",
}
_PERIODS = {
    "24h": "24 hours",
    "7d": "7 days",
    "30d": "30 days",
    "90d": "90 days",
    "1y": "1 year",
    "all": "All",
    "custom": "Custom",
}
_SCHEDULE_MODES = {"existing", "new", "mr", "pr"}


def collapse(value: Optional[str]) -> str:
    return " ".join(str(value or "").split())


def format_page_title(name: str) -> str:
    clean = collapse(name)
    if not clean or clean == "Yaver":
        return "Yaver"
    if clean.startswith("Yaver - "):
        return clean
    return f"Yaver - {clean}"


def _parts(path: str) -> list[str]:
    out: list[str] = []
    for part in (path or "").split("/"):
        if not part:
            continue
        out.append(unquote(part))
    return out


def _page_number(raw: str) -> bool:
    return bool(_PAGE_RE.match(raw or ""))


def _query_value(query: str, key: str) -> str:
    raw = (query or "").lstrip("?")
    values = parse_qs(raw, keep_blank_values=False).get(key) or []
    return values[0] if values else ""


def reviews_page_name(origin: Optional[str], state: Optional[str]) -> str:
    who = (
        "Opened by us"
        if origin == "ours"
        else "Contributed"
        if origin == "contributed"
        else "Merge requests"
    )
    if state == "opened":
        return f"{who} · open"
    if state == "merged":
        return f"{who} · merged"
    if state == "closed":
        return f"{who} · closed"
    return who


def job_page_name(
    summary: Optional[str],
    section: Optional[str] = None,
    issue_key: Optional[str] = None,
) -> str:
    base = collapse(summary) or collapse(issue_key) or "Job"
    tab = _JOB_TABS.get((section or "").strip().lower(), "")
    return f"{base} - {tab}" if tab else base


def issue_page_name(
    summary: Optional[str],
    issue_key: Optional[str] = None,
    section: Optional[str] = None,
) -> str:
    base = collapse(summary) or collapse(issue_key) or "Issue"
    tab = "System logs" if (section or "").strip().lower() == "logs" else ""
    return f"{base} - {tab}" if tab else base


def _parse_schedule(parts: list[str]) -> Optional[tuple[str, str]]:
    bits = [part.strip() for part in parts if part.strip()]
    if bits and _page_number(bits[-1]):
        bits = bits[:-1]
    if len(bits) > 2:
        return None
    mode = bits[0].lower() if bits else ""
    tracker = bits[1].lower() if len(bits) > 1 else ""
    if not mode and not tracker:
        return ("existing", "jira")
    if mode in {"azure", "jira"} and not tracker:
        return ("existing", mode)
    if mode not in _SCHEDULE_MODES:
        return None
    if not tracker or tracker == "jira":
        return (mode, "jira")
    if tracker == "azure" and mode in {"existing", "new"}:
        return (mode, "azure")
    return None


def _schedule_name(parts: list[str]) -> str:
    parsed = _parse_schedule(parts[1:])
    if parsed is None:
        return "Existing issue"
    mode, tracker = parsed
    if mode == "mr":
        return "Existing MR"
    if mode == "pr":
        return "Existing PR"
    base = "New issue" if mode == "new" else "Existing issue"
    if tracker == "azure":
        return f"{base} - Azure work item"
    return base


def page_name(path: str, query: str = "", record_name: Optional[str] = None) -> str:
    """Name after ``Yaver - ``."""
    parts = _parts(path)
    head = parts[0].lower() if parts else ""
    if not head or head == "queue":
        return "Jobs"

    if head == "jobs":
        if len(parts) == 1:
            return "Jobs"
        section = parts[1].lower()
        if section in _JOB_LIST:
            return "Jobs" if len(parts) > 3 else _JOB_LIST[section]
        if len(parts) == 2 and _page_number(parts[1]):
            return "Jobs"
        if len(parts) > 3:
            return "Jobs"
        return job_page_name(record_name, parts[2] if len(parts) > 2 else "")

    if head == "tasks":
        if len(parts) < 2 or len(parts) > 3:
            return "Jobs"
        return issue_page_name(record_name, parts[1], parts[2] if len(parts) > 2 else "")

    if head == "analytics":
        if len(parts) > 1 and parts[1].lower() == "reviews":
            if len(parts) > 3:
                return "Jobs"
            return reviews_page_name(
                _query_value(query, "origin") or None,
                _query_value(query, "state") or None,
            )
        if len(parts) > 2:
            return "Jobs"
        period = parts[1].lower() if len(parts) > 1 else "30d"
        if period not in _PERIODS or period == "30d":
            return "Analytics"
        return f"Analytics - {_PERIODS[period]}"

    if head == "scheduled":
        if len(parts) > 4:
            return "Jobs"
        return _schedule_name(parts)

    if head == "schedules":
        return "Existing issue"

    if head == "sessions":
        if len(parts) == 1 or (len(parts) == 2 and _page_number(parts[1])):
            return "Sessions"
        if len(parts) == 2:
            return collapse(record_name) or "Workspace"
        return "Jobs"

    if head == "storage" and len(parts) == 1:
        return "Storage"
    if head == "poll" and len(parts) == 1:
        return "Board"

    if head == "settings":
        if len(parts) > 2:
            return "Jobs"
        section = parts[1].lower() if len(parts) > 1 else "jira"
        return _SETTINGS.get(section, "Settings - Jira")

    return "Jobs"


def document_title(
    path: str,
    query: str = "",
    record_name: Optional[str] = None,
) -> str:
    return format_page_title(page_name(path, query, record_name=record_name))


def spa_record_ref(path: str) -> Optional[tuple[str, str]]:
    """``('job', id)`` or ``('issue', key)`` when the URL is one record."""
    parts = _parts(path)
    if len(parts) < 2:
        return None
    head = parts[0].lower()
    if head == "jobs":
        section = parts[1].lower()
        if section in _JOB_LIST or (len(parts) == 2 and _page_number(parts[1])):
            return None
        if len(parts) > 3:
            return None
        return ("job", parts[1])
    if head == "tasks" and 2 <= len(parts) <= 3:
        return ("issue", parts[1])
    return None


def apply_document_title(html_text: str, title: str) -> str:
    """Replace ``<title>`` and ``og:title``. Leave a file with no title tag alone."""
    if "<title" not in (html_text or "").lower():
        return html_text
    safe = html.escape(title, quote=True)
    title_tag = f"<title>{safe}</title>"
    updated = _TITLE_RE.sub(title_tag, html_text, count=1)
    og = f'<meta property="og:title" content="{safe}" />'
    if _OG_RE.search(updated):
        return _OG_RE.sub(og, updated, count=1)
    return updated.replace(title_tag, title_tag + og, 1)
