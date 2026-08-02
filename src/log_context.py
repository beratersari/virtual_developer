"""Per-request / per-job log context for dashboard filtering.

Uses contextvars so concurrent asyncio jobs tag their own log lines with
``job_id`` / ``issue_key`` without shared mutable globals.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Optional

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


@contextmanager
def log_context(
    *,
    issue_key: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Iterator[None]:
    """Temporarily bind issue/job for logging (nested-safe)."""
    tokens: list[Token] = []
    if issue_key is not None:
        tokens.append(set_issue_key(issue_key))
    if job_id is not None:
        tokens.append(set_job_id(job_id))
    try:
        yield
    finally:
        # Reset in reverse order
        if job_id is not None:
            _job_id.set(None)
        if issue_key is not None:
            _issue_key.set(None)
