"""In-memory ring buffer of log lines keyed for dashboard task detail."""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Tuple


class IssueLogRing:
    """Keeps recent log lines; filter by issue key substring for task detail."""

    def __init__(self, maxlen: int = 5000) -> None:
        self._lock = threading.Lock()
        self._lines: Deque[Tuple[str, str]] = deque(maxlen=maxlen)  # (iso_ts, message)

    def append(self, message: str) -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._lines.append((ts, message))

    def for_issue(self, issue_key: str, *, limit: int = 500) -> List[Dict[str, str]]:
        key = (issue_key or "").strip()
        if not key:
            return []
        with self._lock:
            matched = [
                {"timestamp": ts, "message": msg}
                for ts, msg in self._lines
                if key in msg
            ]
        return matched[-limit:]


# Process-wide buffer (logger + dashboard share this)
issue_log_ring = IssueLogRing()
