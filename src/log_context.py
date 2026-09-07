"""Per-request / per-job log context for dashboard filtering.

Uses contextvars so concurrent asyncio jobs tag their own log lines with
``job_id`` / ``issue_key`` without shared mutable globals.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

_job_id: ContextVar[Optional[str]] = ContextVar("vd_job_id", default=None)
_issue_key: ContextVar[Optional[str]] = ContextVar("vd_issue_key", default=None)


def get_job_id() -> Optional[str]:
    return _job_id.get()


def get_issue_key() -> Optional[str]:
    return _issue_key.get()


def set_job_id(job_id: Optional[str]) -> Token:
    """Bind active job id for subsequent log lines in this context."""
    return _job_id.set((job_id or "").strip() or None)


def set_issue_key(issue_key: Optional[str]) -> Token:
    return _issue_key.set((issue_key or "").strip() or None)


def clear_log_context() -> None:
    """Reset job and issue tags (call when a process_event finishes)."""
    _job_id.set(None)
    _issue_key.set(None)
