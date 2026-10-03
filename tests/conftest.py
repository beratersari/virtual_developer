"""Shared pytest fixtures for JIRA Virtual Developer tests."""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock

import pytest


def _snapshot_paths(root: Path) -> Set[str]:
    if not root.is_dir():
        return set()
    # Full rglob of a production .jira-agent on WSL/NTFS can hang the
    # autouse fixture for minutes. Top-level files still catch leaks.
    try:
        return {p.name for p in root.iterdir() if p.is_file()}
    except OSError:
        return set()


@pytest.fixture(autouse=True)
def disable_dashboard_auth(monkeypatch: pytest.MonkeyPatch):
    """Keep dashboard APIs open unless a test sets login credentials."""
    from src.config import settings

    monkeypatch.setattr(settings, "dashboard_username", "")
    monkeypatch.setattr(settings, "dashboard_password", "")


@pytest.fixture(autouse=True)
def leave_opencode_serve_alone(monkeypatch: pytest.MonkeyPatch):
    """Stub the serve supervisor so the suite never starts or kills a live serve."""
    from src.opencode_serve_supervisor import supervisor

    async def _watch(_running):
        return None

    monkeypatch.setattr(
        supervisor,
        "request_reload",
        lambda: {
            "status": "reloaded",
            "message": "OpenCode reloaded and is using the saved agents.",
        },
    )
    monkeypatch.setattr(
        supervisor,
        "ensure_started",
        lambda: {
            "status": "ready",
            "message": "OpenCode serve is already running.",
        },
    )
    monkeypatch.setattr(
        supervisor,
        "status",
        lambda: {"status": "ready", "message": "OpenCode serve is running."},
    )
    monkeypatch.setattr(supervisor, "stop_owned", lambda: None)
    monkeypatch.setattr(supervisor, "wait_until_ready", lambda timeout=45.0: True)
    monkeypatch.setattr(supervisor, "watch", _watch)
    monkeypatch.setattr(supervisor, "bind", lambda **_kwargs: None)
    monkeypatch.setattr(supervisor, "reload_outstanding", lambda: False)
    monkeypatch.setattr(
        supervisor,
        "apply_pending_reload",
        lambda timeout=90.0: {
            "status": "ready",
            "message": "OpenCode serve is running.",
        },
    )


def _rebind_store_aliases(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: Path,
    replacements: Dict[int, object],
    folders: Dict[str, tuple],
) -> None:
    """Point import-time store aliases at this test's stores.

    ``from src.state.job_store import job_store`` captures the object.
    A later patch of the defining module does not update api.py, the
    scheduler, or the dashboard service. A module imported after a
    previous test can also keep that test's store. Both would miss the
    operator data directory or read a deleted temp directory.
    """
    from src.state.job_store import JobStore
    from src.state.queue_store import WorkQueueStore
    from src.state.schedule_store import ScheduleStore
    from src.state.session_bind_store import SessionBindStore

    store_types = (JobStore, ScheduleStore, SessionBindStore, WorkQueueStore)
    runtime_resolved = runtime.resolve()

    def _replacement(value: object) -> Optional[object]:
        found = replacements.get(id(value))
        if found is not None:
            return found
        if not isinstance(value, store_types):
            return None
        for attr, (live_dir, repl) in folders.items():
            folder = getattr(value, attr, None)
            if not isinstance(folder, Path):
                continue
            try:
                resolved = folder.resolve()
            except OSError:
                continue
            if resolved == live_dir:
                return repl
            if (
                "_vd_runtime" in resolved.parts
                and runtime_resolved not in resolved.parents
            ):
                return repl
        return None

    for mod in list(sys.modules.values()):
        if not isinstance(mod, types.ModuleType):
            continue
        namespace = getattr(mod, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, value in list(namespace.items()):
            replacement = _replacement(value)
            if replacement is None or replacement is value:
                continue
            try:
                monkeypatch.setattr(mod, name, replacement, raising=False)
            except (AttributeError, TypeError):
                continue


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route job/session writes into tmp and scrub any real-tree leaks.

    Production uses ``YAVER_DATA_DIR`` (``C:\\vd\\yaver``, ``/vd/yaver``, …)
    and a module-level ``job_store`` singleton. Without isolation, tests
    leave files in the durable dir or leftover ``.jira-agent``. Isolated
    paths live under ``tmp_path/_vd_runtime`` (auto-deleted by pytest);
    any *new* files under the real project ``.jira-agent`` are removed on
    teardown as a safety net.
    """
    project_root = Path.cwd()
    real_agent = (project_root / ".jira-agent").resolve()
    before = _snapshot_paths(real_agent)
    real_env = project_root / ".env"
    env_before = real_env.read_text(encoding="utf-8") if real_env.is_file() else None

    # Separate from tests that mkdir tmp_path/.jira-agent themselves
    runtime = tmp_path / "_vd_runtime"
    jobs_dir = runtime / "jobs"
    sessions_dir = runtime / "sessions"
    binds_dir = runtime / "opencode-binds"
    schedules_dir = runtime / "schedules"
    queue_dir = runtime / "queue"
    jobs_dir.mkdir(parents=True)
    sessions_dir.mkdir(parents=True)
    binds_dir.mkdir(parents=True)
    schedules_dir.mkdir(parents=True)
    queue_dir.mkdir(parents=True)

    from src.state.job_store import JobStore
    from src.state.schedule_store import ScheduleStore
    from src.state.session_bind_store import SessionBindStore
    from src.state.queue_store import WorkQueueStore
    import src.state.job_store as job_store_mod
    import src.state.schedule_store as schedule_store_mod
    import src.state.session_bind_store as bind_store_mod
    import src.state.queue_store as queue_store_mod

    live_job = job_store_mod.job_store
    live_schedules = schedule_store_mod.schedule_store
    live_binds = bind_store_mod.session_bind_store
    live_queue = queue_store_mod.work_queue_store
    isolated_store = JobStore(jobs_dir=jobs_dir)
    isolated_binds = SessionBindStore(binds_dir=binds_dir)
    isolated_schedules = ScheduleStore(schedules_dir=schedules_dir)
    isolated_queue = WorkQueueStore(queue_dir=queue_dir)
    _rebind_store_aliases(
        monkeypatch,
        runtime=runtime,
        replacements={
            id(live_job): isolated_store,
            id(live_schedules): isolated_schedules,
            id(live_binds): isolated_binds,
            id(live_queue): isolated_queue,
        },
        folders={
            "jobs_dir": (Path(live_job.jobs_dir).resolve(), isolated_store),
            "schedules_dir": (
                Path(live_schedules.schedules_dir).resolve(),
                isolated_schedules,
            ),
            "binds_dir": (Path(live_binds.binds_dir).resolve(), isolated_binds),
            "queue_dir": (Path(live_queue.queue_dir).resolve(), isolated_queue),
        },
    )
    monkeypatch.setattr(job_store_mod, "_default_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(bind_store_mod, "_default_binds_dir", lambda: binds_dir)
    monkeypatch.setattr(
        schedule_store_mod, "_default_schedules_dir", lambda: schedules_dir
    )
    monkeypatch.setattr(queue_store_mod, "_default_queue_dir", lambda: queue_dir)
    state_dir = runtime / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    from src.config import settings as app_settings

    def _isolated_state_dir(self, _path=state_dir):
        return _path

    monkeypatch.setattr(
        type(app_settings), "state_dir", property(_isolated_state_dir)
    )

    monkeypatch.setattr(
        "src.orchestrator.agent_runner._default_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "src.dashboard.service._sessions_dir",
        lambda: sessions_dir,
        raising=False,
    )
    # Never let tests read/write the developer's real OpenCode SQLite DB
    # (rename relocate would otherwise UPDATE session.directory there).
    fake_opencode_db = runtime / "opencode.db"
    monkeypatch.setattr(
        "src.opencode_sessions._default_db_path",
        lambda: fake_opencode_db,
    )
    try:
        from src.dashboard.temp_storage import reset_size_cache

        reset_size_cache()
    except Exception:
        pass

    yield {
        "runtime": runtime,
        "jobs_dir": jobs_dir,
        "sessions_dir": sessions_dir,
        "job_store": isolated_store,
        "session_bind_store": isolated_binds,
        "binds_dir": binds_dir,
        "schedule_store": isolated_schedules,
        "schedules_dir": schedules_dir,
        "queue_store": isolated_queue,
        "queue_dir": queue_dir,
    }

    shutil.rmtree(runtime, ignore_errors=True)

    # Tests that call apply_settings_update without chdir must not keep
    # JIRA_EMAIL= (or other dotenv writes) in the developer's real .env.
    if env_before is not None and real_env.is_file():
        try:
            if real_env.read_text(encoding="utf-8") != env_before:
                real_env.write_text(env_before, encoding="utf-8")
        except OSError:
            pass

    # Safety net: only remove files created under the real tree during this test
    if real_agent.is_dir():
        after = _snapshot_paths(real_agent)
        for rel in after - before:
            path = real_agent / rel
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                pass

@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


@pytest.fixture
def state_manager(tmp_state_dir: Path):
    from src.state.manager import JiraStateManager

    return JiraStateManager(state_dir=tmp_state_dir)


class FakeJiraClient:
    """Minimal Jira client stub capturing comments (on-prem API v2 style)."""

    def __init__(self):
        self.comments: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, Any]] = []
        self.transitions: List[str] = []

    def add_comment(self, issue_key: str, body: str) -> Dict[str, Any]:
        entry = {"id": str(len(self.comments) + 1), "issue_key": issue_key, "body": body}
        self.comments.append(entry)
        return entry

    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        return [c for c in self.comments if c.get("issue_key") == issue_key]

    def add_labels(self, issue_key: str, labels: List[str]) -> bool:
        return self.update_issue(issue_key, labels=list(labels or []))

    def remove_labels(self, issue_key: str, labels: List[str]) -> bool:
        _ = labels
        return self.update_issue(issue_key, labels=[])

    def replace_label(self, issue_key: str, old: str, new: str) -> bool:
        _ = old
        return self.update_issue(issue_key, labels=[new] if new else [])

    def update_issue(self, issue_key: str, fields=None, labels=None) -> bool:
        self.updated.append({"issue_key": issue_key, "fields": fields, "labels": labels})
        return True

    def transition_issue(self, issue_key: str, transition_name: str) -> bool:
        self.transitions.append(transition_name)
        return True

    def add_attachment(self, issue_key: str, file_path: str, filename=None):
        return {"id": "1"}

    def transition_to_in_progress(self, issue_key: str) -> bool:
        return True

    def get_issue(self, issue_key: str, fields=None, **kwargs):
        return None

    def get_active_sprint(self, board_id: str):
        return None

    def get_sprint_issues(self, sprint_id, fields=None, max_results=100):
        return []

    def get_myself(self):
        return {"name": "devbot", "displayName": "DevBot", "key": "devbot"}

    def assign_issue(self, issue_key: str, username: str) -> bool:
        self.updated.append(
            {"issue_key": issue_key, "fields": {"assignee": username}, "labels": None}
        )
        return True

@pytest.fixture
def fake_jira() -> FakeJiraClient:
    return FakeJiraClient()


@pytest.fixture
def reporter(fake_jira: FakeJiraClient):
    from src.reporter.jira_reporter import JiraReporter

    return JiraReporter(client=fake_jira)


def make_issue_event(
    key: str = "PROJ-1",
    summary: str = "Fix typo",
    description: str = "Fix a small typo",
    status: str = "To Do",
    event_type: str = "jira:issue_created",
    labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "webhookEvent": event_type,
        "issue": {
            "key": key,
            "fields": {
                "summary": summary,
                "description": description,
                "status": {"name": status},
                "labels": labels or ["ai-assist"],
                "assignee": {"displayName": "Jira AI Bot"},
            },
        },
    }
