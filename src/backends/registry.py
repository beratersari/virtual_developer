"""Resolve which AgentBackend implementation should run a task."""

from __future__ import annotations

from typing import Optional

from src.backends.base import (
    BACKEND_CODEX,
    BACKEND_OPENCODE,
    AgentBackend,
    normalize_backend_name,
)
from src.config import settings


def resolve_backend_name(
    *,
    task_backend: Optional[str] = None,
    issue_backend: Optional[str] = None,
    default: Optional[str] = None,
) -> str:
    """Prefer per-task, then issue {params}, then settings, then OpenCode."""
    for raw in (task_backend, issue_backend, default, getattr(settings, "agent_backend", None)):
        name = normalize_backend_name(raw)
        if name:
            return name
    return BACKEND_OPENCODE


def get_agent_backend(name: Optional[str] = None) -> AgentBackend:
    """Return a worker instance. Unknown names fall back to OpenCode."""
    resolved = resolve_backend_name(task_backend=name)
    if resolved == BACKEND_CODEX:
        from src.backends.codex import CodexBackend

        return CodexBackend()
    from src.backends.opencode import OpenCodeBackend

    return OpenCodeBackend()
