"""State manager for persisting JIRA agent state."""

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.logger import logger
from src.state.models import JiraAgentState, TaskStatus
from src.state.record_db import (
    connect,
    database_path,
    dumps,
    ensure_column,
    import_done,
    loads,
    mark_imported,
)

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_states (
    issue_key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    repository_url TEXT NOT NULL DEFAULT '',
    merge_request_url TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    document TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_states_status ON issue_states(status);
CREATE INDEX IF NOT EXISTS idx_states_updated ON issue_states(updated_at);
"""

_STATE_EXTRA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_states_repo ON issue_states(repository_url);
CREATE INDEX IF NOT EXISTS idx_states_mr ON issue_states(merge_request_url);
CREATE INDEX IF NOT EXISTS idx_states_workdir ON issue_states(working_directory);
"""

_STATE_UPSERT = """
INSERT INTO issue_states (
    issue_key, status, updated_at, repository_url, merge_request_url,
    working_directory, document
) VALUES (
    :issue_key, :status, :updated_at, :repository_url, :merge_request_url,
    :working_directory, :document
)
ON CONFLICT(issue_key) DO UPDATE SET
    status=excluded.status,
    updated_at=excluded.updated_at,
    repository_url=excluded.repository_url,
    merge_request_url=excluded.merge_request_url,
    working_directory=excluded.working_directory,
    document=excluded.document
"""

_STATE_FLAG = "state_json_imported"

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


def _index_fields(data: Dict[str, Any]) -> Dict[str, str]:
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

    def pick(name: str) -> str:
        raw = data.get(name)
        if raw is None or raw == "":
            raw = meta.get(name)
        return str(raw or "").strip()

    updated = ""
    for key in ("completed_at", "started_at", "updated_at"):
        raw = data.get(key)
        if raw:
            updated = str(raw).strip()
            break
    status = data.get("status", "")
    if hasattr(status, "value"):
        status = status.value
    return {
        "issue_key": str(data.get("issue_key") or "").strip().upper(),
        "status": str(status or "").strip(),
        "updated_at": updated,
        "repository_url": pick("repository_url"),
        "merge_request_url": pick("merge_request_url"),
        "working_directory": pick("working_directory"),
    }


class JiraStateManager:
    """Issue state rows in yaver.sqlite.

    Leftover ``*.json`` in the state folder is imported once when this
    manager is constructed, then deleted. Later reads do not scan the folder.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = Path(state_dir or settings.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # Shared across all instances for this directory (not per-instance)
        self._lock = _lock_for_state_dir(self.state_dir)
        self._conn = None
        try:
            conn = connect(database_path(self.state_dir))
            conn.executescript(_STATE_SCHEMA)
            ensure_column(conn, "issue_states", "repository_url", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "issue_states", "merge_request_url", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "issue_states", "working_directory", "TEXT NOT NULL DEFAULT ''")
            ensure_column(conn, "issue_states", "document", "TEXT NOT NULL DEFAULT ''")
            conn.executescript(_STATE_EXTRA_INDEXES)
            conn.commit()
            self._conn = conn
        except Exception as exc:
            logger.warning(f"Issue-state database unavailable: {exc}")
            return
        with self._lock:
            try:
                self._import_locked()
            except OSError as exc:
                logger.warning(f"Issue-state import failed: {exc}")

    @staticmethod
    def _sanitize_issue_key(issue_key: str, *, fold_hyphen: bool) -> str:
        text = issue_key or ""
        if fold_hyphen:
            text = text.replace("-", "_")
        text = text.replace("/", "_").replace("\\", "_")
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in text)

    def _get_state_file(self, issue_key: str) -> Path:
        """Preferred path for this key. Hyphens are kept (KAN-12.json)."""
        return self.state_dir / f"{self._sanitize_issue_key(issue_key, fold_hyphen=False)}.json"

    def _legacy_state_file(self, issue_key: str) -> Path:
        """Pre-C5 path that folded '-' to '_' (KAN-12 → KAN_12.json)."""
        return self.state_dir / f"{self._sanitize_issue_key(issue_key, fold_hyphen=True)}.json"

    def _legacy_file_belongs_to(self, path: Path, issue_key: str) -> bool:
        want = (issue_key or "").strip()
        if not want or not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            stored = str(data.get("issue_key") or "").strip()
        except Exception:
            return False
        if not stored:
            return True
        return stored.upper() == want.upper()

    def _resolve_state_file(self, issue_key: str) -> Optional[Path]:
        """Existing file for this key: new name first, then hyphen-folded legacy."""
        primary = self._get_state_file(issue_key)
        if primary.exists():
            return primary
        legacy = self._legacy_state_file(issue_key)
        if legacy == primary:
            return None
        if self._legacy_file_belongs_to(legacy, issue_key):
            return legacy
        return None

    def _import_locked(self) -> None:
        """Import leftover state JSON once. Caller holds ``_lock``.

        ``KAN-12.json`` wins over a hyphen-folded ``KAN_12.json`` for the
        same issue. Parsed files in that group are deleted, including the
        loser. Corrupt files and files with no issue key stay on disk.
        A glob error leaves the flag unset.
        """
        conn = self._conn
        if conn is None or import_done(conn, _STATE_FLAG):
            return
        if not self.state_dir.is_dir():
            mark_imported(conn, _STATE_FLAG)
            return
        paths = [
            path
            for path in self.state_dir.glob("*.json")
            if not path.name.endswith(".tmp")
        ]
        groups: Dict[str, List[tuple]] = {}
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                logger.warning(f"Issue-state import skip {path.name}: {exc}")
                continue
            if not isinstance(raw, dict):
                logger.warning(f"Issue-state import skip {path.name}: not an object")
                continue
            key = str(raw.get("issue_key") or "").strip()
            if not key:
                continue
            primary = path.name == self._get_state_file(key).name
            groups.setdefault(key.upper(), []).append((path, raw, primary))
        for items in groups.values():
            winner = items[0][1]
            for _path, raw, primary in items:
                if primary:
                    winner = raw
                    break
            self._upsert_dict_locked(winner)
            for path, _raw, _primary in items:
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning(
                        f"Imported {path.name} but could not remove it: {exc}"
                    )
        mark_imported(conn, _STATE_FLAG)

    def _upsert_dict_locked(self, data: Dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            raise RuntimeError("issue-state database unavailable")
        fields = _index_fields(data)
        if not fields["issue_key"]:
            raise RuntimeError("issue state is missing issue_key")
        fields["document"] = dumps(data)
        conn.execute(_STATE_UPSERT, fields)
        conn.commit()

    def _load_locked(self, issue_key: str) -> Optional[JiraAgentState]:
        key = (issue_key or "").strip().upper()
        conn = self._conn
        if not key or conn is None:
            return None
        row = conn.execute(
            "SELECT document FROM issue_states WHERE issue_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        data = loads(row["document"])
        if data is None:
            return None
        return JiraAgentState.from_dict(data)

    def get_state(self, issue_key: str) -> Optional[JiraAgentState]:
        """Load state for an issue from yaver.sqlite."""
        with self._lock:
            try:
                return self._load_locked(issue_key)
            except Exception as e:
                logger.error(f"Error loading state for {issue_key}: {e}")
                return None

    def set_state(self, state: JiraAgentState, *, force: bool = False) -> bool:
        """Save state. Returns False when the write fails or would clobber a terminal status.

        A terminal status (completed, error, cancelled) is kept unless
        ``force=True`` (intentional reprocess back to pending).
        """
        with self._lock:
            if self._conn is None:
                logger.error(
                    f"Error saving state for {state.issue_key}: database unavailable"
                )
                return False
            try:
                existing = self._load_locked(state.issue_key)
                if existing is not None:
                    old_status = existing.status
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
                self._upsert_dict_locked(state.to_dict())
                return True
            except Exception as e:
                logger.error(f"Error saving state for {state.issue_key}: {e}")
                return False

    def create_state(
        self,
        issue_key: str,
        issue_summary: str,
        description: str = "",
        triggered_by: Optional[str] = None,
        jira_assignee: Optional[str] = None,
    ) -> Optional[JiraAgentState]:
        """Create a new state for an issue.

        Returns None when the first disk write fails and nothing is stored.
        Callers must not treat that as an owned pending issue.
        """
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
            existing = self.get_state(issue_key)
            if existing is not None:
                return existing
            return None
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
        expected_current_task_id: Optional[str] = None,
        expected_job_id: Optional[str] = None,
        force: bool = False,
        **kwargs: Any,
    ) -> Optional[JiraAgentState]:
        """Compare-and-swap style update under the RLock.

        * If ``expected_statuses`` is set, current status must be in that set.
        * If ``reject_statuses`` is set, current status must *not* be in that set.
        * If ``expected_current_task_id`` / ``expected_job_id`` are set, the
          live run must still be that generation (stale complete after a
          new GitLab/Azure begin must not stamp the new job).
        * On mismatch, returns None without writing (caller treats as aborted/stale).
        * On disk write failure or terminal-clobber refuse, returns None.
        * ``force=True`` allows an intentional terminal → in-flight write
          (same-ticket ``plan_execute`` retry after ERROR/CANCELLED).

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
            want_task = str(expected_current_task_id or "").strip()
            if want_task:
                live_task = str(state.current_task_id or "").strip()
                if live_task and live_task != want_task:
                    logger.info(
                        f"update_state_if skip {issue_key}: "
                        f"task_id={live_task} != expected {want_task}"
                    )
                    return None
            want_job = str(expected_job_id or "").strip()
            if want_job:
                live_job = str(
                    (state.metadata or {}).get("current_job_id") or ""
                ).strip()
                if live_job and live_job != want_job:
                    logger.info(
                        f"update_state_if skip {issue_key}: "
                        f"job_id={live_job} != expected {want_job}"
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

            if not self.set_state(state, force=force):
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
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                rows = conn.execute("SELECT document FROM issue_states").fetchall()
            except Exception as e:
                logger.error(f"Error loading issue states: {e}")
                return []
        found: List[JiraAgentState] = []
        for row in rows:
            data = loads(row["document"])
            if data is None:
                continue
            try:
                found.append(JiraAgentState.from_dict(data))
            except Exception as e:
                logger.error(f"Error loading issue state: {e}")
        return found

    def get_active_issues(self) -> List[JiraAgentState]:
        """Get all issues that are not in a terminal state."""
        terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
        return [s for s in self.get_all_states() if s.status not in terminal_states]

    def delete_state(self, issue_key: str) -> bool:
        """Delete the issue-state row. Returns True when a row was removed."""
        key = (issue_key or "").strip().upper()
        if not key:
            return False
        with self._lock:
            conn = self._conn
            if conn is None:
                return False
            try:
                conn.execute(
                    "DELETE FROM issue_states WHERE issue_key = ?",
                    (key,),
                )
                changed = conn.execute("SELECT changes()").fetchone()
                conn.commit()
            except Exception as e:
                logger.error(f"Error deleting state for {issue_key}: {e}")
                return False
            return bool(changed and int(changed[0]) > 0)
