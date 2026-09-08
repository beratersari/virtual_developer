"""JIRA integration module."""

from typing import Any

__all__ = ["JiraClient", "JiraPoller"]


def __getattr__(name: str) -> Any:
    if name == "JiraClient":
        from .client import JiraClient

        return JiraClient
    if name == "JiraPoller":
        from .poller import JiraPoller

        return JiraPoller
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
