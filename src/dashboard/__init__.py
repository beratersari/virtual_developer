"""Ops dashboard: API + live poll snapshot (UI is display-only)."""

from typing import Any

__all__ = [
    "create_dashboard_app",
    "PollSnapshotStore",
    "poll_snapshot_store",
]


def __getattr__(name: str) -> Any:
    # Lazy: config imports project_repos at startup; do not pull FastAPI then.
    if name == "create_dashboard_app":
        from src.dashboard.api import create_dashboard_app

        return create_dashboard_app
    if name in {"PollSnapshotStore", "poll_snapshot_store"}:
        from src.dashboard import snapshot

        return getattr(snapshot, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
