"""Shared pytest fixtures for JIRA Virtual Developer tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock

import pytest


def _snapshot_paths(root: Path) -> Set[str]:
    if not root.is_dir():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route job/session writes into tmp and scrub any real-tree leaks.

    Production uses ``Path.cwd()/.jira-agent/...`` and a module-level
    ``job_store`` singleton. Without isolation, tests leave files in the repo
    after assertions. Isolated paths live under ``tmp_path/_vd_runtime`` (auto-
    deleted by pytest); any *new* files under the real project ``.jira-agent``
    are removed on teardown as a safety net.
    """
    project_root = Path.cwd()
    real_agent = (project_root / ".jira-agent").resolve()
    before = _snapshot_paths(real_agent)

    # Separate from tests that mkdir tmp_path/.jira-agent themselves
    runtime = tmp_path / "_vd_runtime"
    jobs_dir = runtime / "jobs"
    sessions_dir = runtime / "sessions"
    binds_dir = runtime / "opencode-binds"
    jobs_dir.mkdir(parents=True)
    sessions_dir.mkdir(parents=True)
    binds_dir.mkdir(parents=True)

    from src.state.job_store import JobStore
    from src.state.session_bind_store import SessionBindStore
    import src.processor as processor_mod
    import src.state.job_store as job_store_mod
    import src.state.session_bind_store as bind_store_mod

    isolated_store = JobStore(jobs_dir=jobs_dir)
    isolated_binds = SessionBindStore(binds_dir=binds_dir)
    monkeypatch.setattr(job_store_mod, "job_store", isolated_store)
    monkeypatch.setattr(job_store_mod, "_default_jobs_dir", lambda: jobs_dir)
    monkeypatch.setattr(processor_mod, "job_store", isolated_store)
    monkeypatch.setattr(bind_store_mod, "session_bind_store", isolated_binds)
    monkeypatch.setattr(bind_store_mod, "_default_binds_dir", lambda: binds_dir)

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
    # Never let settings PATCH rewrite the developer's real .env
    fake_dotenv = runtime / ".env"
    fake_dotenv.write_text("JIRA_BOARD_ID=1\n", encoding="utf-8")
    monkeypatch.setenv("VD_DOTENV_PATH", str(fake_dotenv))
    import src.config as config_mod

    monkeypatch.setattr(config_mod, "dotenv_file_path", lambda: fake_dotenv)
    # Dashboard settings tests must not rewrite live runtime_settings.json
    fake_runtime_settings = runtime / "runtime_settings.json"
    monkeypatch.setattr(
        config_mod, "runtime_settings_path", lambda: fake_runtime_settings
    )

    yield {
        "runtime": runtime,
        "jobs_dir": jobs_dir,
        "sessions_dir": sessions_dir,
        "job_store": isolated_store,
        "session_bind_store": isolated_binds,
        "dotenv_path": fake_dotenv,
        "runtime_settings_path": fake_runtime_settings,
        "binds_dir": binds_dir,
    }

    shutil.rmtree(runtime, ignore_errors=True)

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
