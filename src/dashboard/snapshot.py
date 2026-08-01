"""Thread-safe poll cycle snapshot for the ops dashboard."""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


class PollSnapshotStore:
    """Holds the latest poll observation and countdown fields.

    Updated by the poller thread; read by the dashboard API (any thread).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {
            "phase": "idle",  # idle | polling | waiting
            "last_poll_at": None,
            "next_poll_at": None,
            "poll_interval_seconds": 30,
            "source": None,
            "board_id": None,
            "issues": [],
            "matched_count": 0,
            "will_process_count": 0,
            "error": None,
            "cycle": 0,
        }
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        """Register a listener for snapshot updates. Returns unsubscribe."""
        with self._lock:
            self._listeners.append(callback)

        def _unsub() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return _unsub

    def _notify(self) -> None:
        snapshot = self.snapshot()
        for cb in list(self._listeners):
            try:
                cb(snapshot)
            except Exception:
                pass

    def begin_poll(
        self,
        *,
        board_id: Optional[str],
        interval_seconds: int,
    ) -> None:
        with self._lock:
            self._data["phase"] = "polling"
            self._data["board_id"] = board_id
            self._data["poll_interval_seconds"] = interval_seconds
            self._data["error"] = None
            self._data["cycle"] = int(self._data.get("cycle") or 0) + 1
        self._notify()

    def end_poll(
        self,
        *,
        source: str,
        issues: List[Dict[str, Any]],
        interval_seconds: int,
        error: Optional[str] = None,
    ) -> None:
        now = datetime.now()
        next_at = now + timedelta(seconds=max(1, interval_seconds))
        matched = sum(1 for i in issues if i.get("matched_label") or i.get("matched_assignee"))
        will = sum(1 for i in issues if i.get("will_process"))
        with self._lock:
            self._data.update(
                {
                    "phase": "waiting",
                    "last_poll_at": now.isoformat(timespec="seconds"),
                    "next_poll_at": next_at.isoformat(timespec="seconds"),
                    "poll_interval_seconds": interval_seconds,
                    "source": source,
                    "issues": issues,
                    "matched_count": matched,
                    "will_process_count": will,
                    "error": error,
                }
            )
        self._notify()

    def set_idle(self) -> None:
        with self._lock:
            self._data["phase"] = "idle"
        self._notify()

    def snapshot(self) -> Dict[str, Any]:
        """Return a copy including computed seconds_until_next_poll."""
        with self._lock:
            data = deepcopy(self._data)
        now = datetime.now()
        next_raw = data.get("next_poll_at")
        seconds: Optional[int] = None
        if next_raw:
            try:
                nxt = datetime.fromisoformat(next_raw)
                seconds = max(0, int((nxt - now).total_seconds()))
            except ValueError:
                seconds = None
        data["seconds_until_next_poll"] = seconds
        data["server_time"] = now.isoformat(timespec="seconds")
        return data


# Process-wide store (daemon + API share this instance)
poll_snapshot_store = PollSnapshotStore()
