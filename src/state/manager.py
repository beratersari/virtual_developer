"""State manager for persisting JIRA agent state."""

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.logger import logger
from src.state.models import JiraAgentState, TaskStatus

# Terminal statuses must not be overwritten by in-flight / pending writes
# (dashboard RMW or a second JiraStateManager instance on the same dir).
_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
        TaskStatus.CANCELLED,
    }
)

# Process-wide locks keyed by resolved state_dir so every manager instance
# sharing a directory serializes RMW (daemon + processor + poller + tests).
_DIR_LOCKS: Dict[str, threading.RLock] = {}
_DIR_LOCKS_GUARD = threading.Lock()


def _lock_for_state_dir(state_dir: Path) -> threading.RLock:
    key = str(Path(state_dir).resolve())
    with _DIR_LOCKS_GUARD:
        lock = _DIR_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DIR_LOCKS[key] = lock
        return lock


def _log_state_transition(issue_key: str, old_status: TaskStatus, new_status: TaskStatus) -> None:
    """Log a status change using the standard application logger format."""
    logger.info(
        f"state {issue_key}: {old_status.value} -> {new_status.value}"
    )


class JiraStateManager:
    """Manages JIRA agent state persistence to disk."""

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir or settings.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Shared across all instances for this directory (not per-instance)
        self._lock = _lock_for_state_dir(self.state_dir)

    def _get_state_file(self, issue_key: str) -> Path:
        """Get path to state file for an issue."""
        safe_key = issue_key.replace("-", "_").replace("/", "_").replace("\\", "_")
        safe_key = "".join(c if c.isalnum() or c in "._-" else "_" for c in safe_key)
        return self.state_dir / f"{safe_key}.json"

    def get_state(self, issue_key: str) -> Optional[JiraAgentState]:
        """Load state for an issue from disk."""
        state_file = self._get_state_file(issue_key)
        with self._lock:
            if not state_file.exists():
                return None
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return JiraAgentState.from_dict(data)
            except Exception as e:
                logger.error(f"Error loading state for {issue_key}: {e}")
                return None

    def set_state(self, state: JiraAgentState, *, force: bool = False) -> bool:
        """Save state to disk atomically.

        Returns True when the file was written. Returns False when:
        * disk write fails, or
        * the write would clobber a terminal status with a non-terminal one
          (protects COMPLETED/CANCELLED/ERROR from stale dashboard RMW).

        Pass ``force=True`` for intentional reprocess resets (terminal → PENDING).
        """
        state_file = self._get_state_file(state.issue_key)

        with self._lock:
            try:
                if state_file.exists():
                    with open(state_file, "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                    old_status = TaskStatus(old_data.get("status", "pending"))
                    if (
                        not force
                        and old_status in _TERMINAL_STATUSES
                        and state.status not in _TERMINAL_STATUSES
                    ):
                        logger.warning(
                            f"Refuse set_state {state.issue_key}: would clobber "
                            f"terminal {old_status.value} with {state.status.value}"
                        )
                        return False
                    if old_status != state.status:
                        _log_state_transition(state.issue_key, old_status, state.status)
            except Exception:
                pass

            try:
                payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
                tmp = state_file.with_suffix(state_file.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, state_file)
                return True
            except Exception as e:
                logger.error(f"Error saving state for {state.issue_key}: {e}")
                try:
                    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
                return False

    def create_state(
        self,
        issue_key: str,
        issue_summary: str,
        description: str = "",
        triggered_by: Optional[str] = None,
        jira_assignee: Optional[str] = None,
    ) -> JiraAgentState:
        """Create a new state for an issue."""
        logger.info(f"state {issue_key}: created -> {TaskStatus.PENDING.value}")

        state = JiraAgentState(
            issue_key=issue_key,
            issue_summary=issue_summary,
            description=description,
            status=TaskStatus.PENDING,
            triggered_by=triggered_by,
            jira_assignee=jira_assignee,
            metadata={},
        )
        if not self.set_state(state):
            logger.error(f"state {issue_key}: create failed to persist to disk")
        return state

    def update_state(
        self,
        issue_key: str,
        *,
        force: bool = False,
        **kwargs: Any,
    ) -> Optional[JiraAgentState]:
        """Update specific fields of an existing state (locked RMW).

        Returns None when the issue is missing, the write is refused
        (terminal clobber), or disk persistence fails.

        ``force=True`` allows intentional reprocess (terminal → PENDING).
        """
        with self._lock:
            state = self.get_state(issue_key)
            if not state:
                logger.warning(f"No state found for {issue_key}")
                return None

            for key, value in kwargs.items():
                if not hasattr(state, key):
                    logger.warning(f"Unknown field: {key}")
                    continue
                if key == "metadata" and isinstance(value, dict):
                    state.metadata = {**(state.metadata or {}), **value}
                else:
                    setattr(state, key, value)

            if not self.set_state(state, force=force):
                return None
            return state

    def update_state_if(
        self,
        issue_key: str,
        *,
        expected_statuses: Optional[set] = None,
        reject_statuses: Optional[set] = None,
        **kwargs: Any,
    ) -> Optional[JiraAgentState]:
        """Compare-and-swap style update under the RLock.

        * If ``expected_statuses`` is set, current status must be in that set.
        * If ``reject_statuses`` is set, current status must *not* be in that set.
        * On mismatch, returns None without writing (caller treats as aborted/stale).
        * On disk write failure or terminal-clobber refuse, returns None.

        Metadata patches still merge. Use this for progress→terminal transitions
        so cancel/watchdog ERROR/CANCELLED cannot be overwritten by late success.
        """
        with self._lock:
            state = self.get_state(issue_key)
            if not state:
                logger.warning(f"No state found for {issue_key} (update_state_if)")
                return None
            if expected_statuses is not None and state.status not in expected_statuses:
                logger.info(
                    f"update_state_if skip {issue_key}: status={state.status.value} "
                    f"not in expected {[s.value if hasattr(s, 'value') else s for s in expected_statuses]}"
                )
                return None
            if reject_statuses is not None and state.status in reject_statuses:
                logger.info(
                    f"update_state_if skip {issue_key}: status={state.status.value} "
                    f"is rejected"
                )
                return None

            for key, value in kwargs.items():
                if not hasattr(state, key):
                    logger.warning(f"Unknown field: {key}")
                    continue
                if key == "metadata" and isinstance(value, dict):
                    state.metadata = {**(state.metadata or {}), **value}
                else:
                    setattr(state, key, value)

            if not self.set_state(state):
                return None
            return state

    def record_retry_attempt(
        self,
        issue_key: str,
        attempt: Any,
        *,
        abort_statuses: Optional[set] = None,
        current_task_id: Optional[str] = None,
        current_opencode_session_id: Optional[str] = None,
    ) -> Optional[JiraAgentState]:
        """Append a retry attempt under the lock without clobbering terminal status.

        Re-reads state under the RLock, skips if status is already aborted
        (cancelled/error), then appends the attempt and optional live ids.
        Returns the updated state, the unchanged aborted state, or None.
        """
        aborted = abort_statuses or {TaskStatus.CANCELLED, TaskStatus.ERROR}
        with self._lock:
            state = self.get_state(issue_key)
            if not state:
                logger.warning(f"No state found for {issue_key} (record_retry)")
                return None
            if state.status in aborted:
                logger.info(
                    f"Skipping retry record for {issue_key}: "
                    f"already {state.status.value}"
                )
                return state
            state.add_retry_attempt(attempt)
            if current_task_id is not None:
                state.current_task_id = current_task_id
            if current_opencode_session_id is not None:
                state.current_opencode_session_id = current_opencode_session_id
            if not self.set_state(state):
                return None
            return state

    def get_all_states(self) -> List[JiraAgentState]:
        """Load every persisted issue state (all statuses)."""
        states: List[JiraAgentState] = []
        with self._lock:
            if not self.state_dir.exists():
                return states
            for state_file in self.state_dir.glob("*.json"):
                if state_file.name.endswith(".tmp"):
                    continue
                try:
                    with open(state_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    states.append(JiraAgentState.from_dict(data))
                except Exception as e:
                    logger.error(f"Error loading {state_file}: {e}")
        return states

    def get_active_issues(self) -> List[JiraAgentState]:
        """Get all issues that are not in a terminal state."""
        terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
        return [s for s in self.get_all_states() if s.status not in terminal_states]

    def delete_state(self, issue_key: str) -> bool:
        """Delete state file for an issue."""
        state_file = self._get_state_file(issue_key)
        with self._lock:
            if state_file.exists():
                try:
                    state_file.unlink()
                    return True
                except Exception as e:
                    logger.error(f"Error deleting state for {issue_key}: {e}")
                    return False
        return False
