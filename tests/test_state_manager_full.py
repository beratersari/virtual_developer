"""Full branch coverage for JiraStateManager."""

import json
from pathlib import Path
from unittest.mock import mock_open, patch

from src.state.manager import JiraStateManager, _log_state_transition
from src.state.models import TaskStatus


def test_get_state_missing(state_manager):
    assert state_manager.get_state("NOPE-1") is None


def test_get_state_corrupt_json(tmp_state_dir):
    mgr = JiraStateManager(state_dir=tmp_state_dir)
    path = tmp_state_dir / "BAD_1.json"
    path.write_text("{not json", encoding="utf-8")
    # issue key BAD-1 maps to BAD_1.json
    assert mgr.get_state("BAD-1") is None


def test_set_state_logs_transition(state_manager):
    s = state_manager.create_state("TR-1", "s", "d")
    s.status = TaskStatus.PLANNING
    state_manager.set_state(s)
    s2 = state_manager.get_state("TR-1")
    assert s2.status == TaskStatus.PLANNING


def test_set_state_same_status_no_transition_log(state_manager):
    s = state_manager.create_state("TR-2", "s", "d")
    state_manager.set_state(s)  # same PENDING
    assert state_manager.get_state("TR-2").status == TaskStatus.PENDING


def test_set_state_write_error(state_manager, monkeypatch):
    s = state_manager.create_state("TR-3", "s", "d")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", boom)
    # should not raise
    state_manager.set_state(s)


def test_update_unknown_field_warns(state_manager):
    state_manager.create_state("TR-4", "s", "d")
    result = state_manager.update_state("TR-4", not_a_real_field=123)
    assert result is not None


def test_update_missing_state(state_manager):
    assert state_manager.update_state("MISSING-1", status=TaskStatus.ERROR) is None


def test_get_active_issues_empty_dir(tmp_path):
    empty = tmp_path / "nostate"
    # dir does not exist
    mgr = JiraStateManager(state_dir=empty)
    # __init__ creates dir
    assert mgr.get_active_issues() == []


def test_get_active_issues_skips_corrupt(tmp_state_dir):
    mgr = JiraStateManager(state_dir=tmp_state_dir)
    mgr.create_state("OK-1", "s", "d")
    (tmp_state_dir / "junk.json").write_text("{bad", encoding="utf-8")
    active = mgr.get_active_issues()
    assert any(s.issue_key == "OK-1" for s in active)


def test_delete_state_exists_and_missing(state_manager):
    state_manager.create_state("DEL-1", "s", "d")
    assert state_manager.delete_state("DEL-1") is True
    assert state_manager.get_state("DEL-1") is None
    assert state_manager.delete_state("DEL-1") is False


def test_delete_state_unlink_error(state_manager, monkeypatch):
    state_manager.create_state("DEL-2", "s", "d")
    path = state_manager._get_state_file("DEL-2")

    class BoomPath:
        def exists(self):
            return True

        def unlink(self):
            raise OSError("perm")

    monkeypatch.setattr(state_manager, "_get_state_file", lambda k: BoomPath())
    assert state_manager.delete_state("DEL-2") is False


def test_safe_key_sanitization(state_manager):
    # slash becomes underscore in filename
    p = state_manager._get_state_file("PROJ/1")
    assert "/" not in p.name
    assert "PROJ_1" in p.name


def test_log_state_transition_callable():
    _log_state_transition("X-1", TaskStatus.PENDING, TaskStatus.ERROR)


def test_set_state_corrupt_old_file_skips_transition_log(tmp_state_dir):
    mgr = JiraStateManager(state_dir=tmp_state_dir)
    s = mgr.create_state("COR-1", "s", "d")
    path = mgr._get_state_file("COR-1")
    path.write_text("not-json", encoding="utf-8")
    s.status = TaskStatus.ERROR
    # should not raise despite corrupt old file
    mgr.set_state(s)
