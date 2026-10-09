"""Comma-separated Jira board ids on the existing Board ID setting."""

from pathlib import Path

import pytest

from src.dashboard.schemas import SettingsUpdate
from src.jira.poller import JiraPoller


def _todo(key: str, summary: str | None = None) -> dict:
    return {
        "key": key,
        "fields": {
            "status": {"name": "To Do"},
            "labels": [],
            "summary": summary or key,
            "assignee": {"displayName": "DevBot"},
        },
    }


class _Boards:
    """One client whose sprint_lookup and last_error move with each board."""

    def __init__(self, sprints: dict, issues: dict):
        self.sprints = sprints
        self.issues = issues
        self.sprint_lookup = None
        self.last_error = None
        self.sprint_calls: list[str] = []
        self.board_calls: list[str] = []
        self.sprint_issue_calls: list = []

    def get_active_sprint(self, board_id: str):
        self.sprint_calls.append(str(board_id))
        spec = self.sprints.get(str(board_id))
        if spec is None:
            self.sprint_lookup = "error"
            self.last_error = f"unknown board {board_id}"
            return None
        kind = spec[0]
        self.sprint_lookup = kind
        if kind == "error":
            self.last_error = spec[1]
            return None
        self.last_error = None
        if kind == "ok":
            return spec[1]
        return None

    def get_board_issues(self, board_id: str, fields=None, max_results=100):
        self.board_calls.append(str(board_id))
        self.last_error = None
        return list(self.issues.get(("board", str(board_id)), []))

    def get_sprint_issues(self, sprint_id, fields=None, max_results=100):
        self.sprint_issue_calls.append(sprint_id)
        self.last_error = None
        return list(self.issues.get(("sprint", sprint_id), []))


def _poll(board_id: str, client: _Boards, state_manager):
    from unittest.mock import patch

    poller = JiraPoller(client=client, interval_seconds=30, board_id=board_id, state_manager=state_manager)
    with patch("src.jira.poller.settings") as s:
        s.jira_enabled = True
        s.trigger_assignee_names_list = ["devbot"]
        s.jira_trigger_label_list = []
        return poller.poll_board()


def test_settings_update_accepts_comma_separated_board_ids():
    assert SettingsUpdate(jira_board_id="2, 5").jira_board_id == "2,5"
    assert SettingsUpdate(jira_board_id="5, 2, 5").jira_board_id == "5,2"
    assert SettingsUpdate(jira_board_id="`2, 5`").jira_board_id == "2,5"
    assert SettingsUpdate(jira_board_id="2,,5").jira_board_id == "2,5"
    many = ", ".join(str(i) for i in range(1, 30))
    assert SettingsUpdate(jira_board_id=many).jira_board_id == ",".join(
        str(i) for i in range(1, 30)
    )


def test_settings_update_rejects_a_bad_token_in_the_board_list():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SettingsUpdate(jira_board_id="2, board")
    with pytest.raises(ValidationError):
        SettingsUpdate(jira_board_id="2,5a")


def test_runtime_settings_store_comma_separated_board_ids(tmp_path, monkeypatch):
    from src.config import apply_runtime_settings_to, save_runtime_settings, settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    monkeypatch.setattr(settings, "jira_board_id", "1")
    save_runtime_settings({"jira_board_id": "2, 5"})
    apply_runtime_settings_to(settings)
    assert settings.jira_board_id == "2,5"

    save_runtime_settings({"jira_board_id": "2,x"})
    apply_runtime_settings_to(settings)
    assert settings.jira_board_id == "2,5"


def test_poll_board_reads_each_configured_board(state_manager):
    client = _Boards(
        sprints={
            "2": ("kanban", None),
            "5": ("ok", {"id": 9, "name": "Alpha"}),
        },
        issues={
            ("board", "2"): [_todo("KAN-1")],
            ("sprint", 9): [_todo("SAM-1")],
        },
    )
    out = _poll("2, 5", client, state_manager)
    assert client.sprint_calls == ["2", "5"]
    assert [row["key"] for row in out] == ["KAN-1", "SAM-1"]
    assert client.sprint_issue_calls == [9]
    assert client.board_calls == ["2"]


def test_poll_board_takes_an_issue_on_two_boards_once(state_manager):
    shared = _todo("KAN-1", summary="from board 2")
    later = _todo("KAN-1", summary="from board 5")
    client = _Boards(
        sprints={"2": ("kanban", None), "5": ("kanban", None)},
        issues={
            ("board", "2"): [shared],
            ("board", "5"): [later, _todo("SAM-2")],
        },
    )
    out = _poll("2,5", client, state_manager)
    assert [row["key"] for row in out] == ["KAN-1", "SAM-2"]
    assert out[0]["fields"]["summary"] == "from board 2"


def test_poll_board_keeps_later_boards_when_one_sprint_lookup_fails(state_manager):
    from src.dashboard.snapshot import poll_snapshot_store

    client = _Boards(
        sprints={
            "2": ("error", "sprint down"),
            "5": ("kanban", None),
        },
        issues={("board", "5"): [_todo("SAM-1")]},
    )
    out = _poll("2,5", client, state_manager)
    assert client.sprint_calls == ["2", "5"]
    assert "2" not in client.board_calls
    assert [row["key"] for row in out] == ["SAM-1"]
    error = poll_snapshot_store.snapshot().get("error") or ""
    assert "2" in error
    assert "sprint down" in error


def test_poll_board_skips_an_empty_sprint_and_still_reads_the_next_board(state_manager):
    from src.dashboard.snapshot import poll_snapshot_store

    client = _Boards(
        sprints={
            "2": ("empty", None),
            "5": ("ok", {"id": 3, "name": "Beta"}),
        },
        issues={("sprint", 3): [_todo("SAM-3")]},
    )
    out = _poll("2, 5", client, state_manager)
    assert "2" not in client.board_calls
    assert [row["key"] for row in out] == ["SAM-3"]
    assert not (poll_snapshot_store.snapshot().get("error") or "").strip()


def test_poll_board_reports_a_failed_board_when_another_sprint_is_empty(state_manager):
    from src.dashboard.snapshot import poll_snapshot_store

    client = _Boards(
        sprints={"2": ("error", "sprint down"), "5": ("empty", None)},
        issues={},
    )
    out = _poll("2,5", client, state_manager)
    assert out == []
    assert client.sprint_calls == ["2", "5"]
    assert client.board_calls == []
    error = poll_snapshot_store.snapshot().get("error") or ""
    assert "sprint down" in error


def test_poll_board_reports_an_error_when_every_board_fails(state_manager):
    from src.dashboard.snapshot import poll_snapshot_store

    client = _Boards(
        sprints={"2": ("error", "down"), "5": ("error", "also down")},
        issues={},
    )
    out = _poll("2,5", client, state_manager)
    assert out == []
    assert client.sprint_calls == ["2", "5"]
    assert client.board_calls == []
    error = poll_snapshot_store.snapshot().get("error") or ""
    assert "down" in error
    assert "also down" in error


def test_poll_board_does_not_call_jira_when_a_board_id_is_invalid(state_manager):
    from src.dashboard.snapshot import poll_snapshot_store

    client = _Boards(sprints={}, issues={})
    out = _poll("2, board", client, state_manager)
    assert out == []
    assert client.sprint_calls == []
    assert "board" in (poll_snapshot_store.snapshot().get("error") or "").lower()


def test_settings_board_field_allows_a_comma():
    text = Path("web/src/pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    assert 'inputMode="numeric"' not in text
    assert "separated by commas" in text
    assert "first active sprint" in text


def test_settings_save_does_not_require_a_board_id():
    text = Path("web/src/pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    save = text.split("const onSave = async () => {", 1)[1].split(
        "const body: Parameters<typeof patchSettings>[0] = {}", 1
    )[0]
    assert "Board ID is required" not in save
    assert 'needs a PAT' in save
    assert "Azure DevOps host" in save
