"""JIRA integration module."""

from typing import Any

__all__ = ["JiraClient", "JiraPoller", "decide_jira_webhook", "normalize_intake_mode"]


def __getattr__(name: str) -> Any:
    # Lazy: Settings bootstrap may import normalize_intake_mode before
    # ``src.config.settings`` exists. Eager client/poller imports cycle.
    if name == "JiraClient":
        from .client import JiraClient

        return JiraClient
    if name == "JiraPoller":
        from .poller import JiraPoller

        return JiraPoller
    if name == "decide_jira_webhook":
        from .webhook import decide_jira_webhook

        return decide_jira_webhook
    if name == "normalize_intake_mode":
        from .webhook import normalize_intake_mode

        return normalize_intake_mode
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
