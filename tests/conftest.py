"""Shared pytest fixtures for JIRA Virtual Developer tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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

    def get_issue(self, issue_key: str):
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
