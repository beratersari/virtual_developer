"""Agent worker backends (OpenCode, Codex, …)."""

from src.backends.base import (
    BACKEND_CODEX,
    BACKEND_OPENCODE,
    SUPPORTED_BACKENDS,
    AgentBackend,
    AgentRunRequest,
    AgentRunResult,
    normalize_backend_name,
)
from src.backends.registry import get_agent_backend, resolve_backend_name

__all__ = [
    "BACKEND_CODEX",
    "BACKEND_OPENCODE",
    "SUPPORTED_BACKENDS",
    "AgentBackend",
    "AgentRunRequest",
    "AgentRunResult",
    "get_agent_backend",
    "normalize_backend_name",
    "resolve_backend_name",
]
