"""Stale oh-my-openagent run-continuation files must be wiped at job start."""

from unittest.mock import patch

import pytest


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


def test_clear_stale_omo_continuations_removes_json(processor, tmp_path):
    folder = tmp_path / ".omo" / "run-continuation"
    folder.mkdir(parents=True)
    leftover = folder / "ses_old.json"
    leftover.write_text(
        '{"sessionID":"ses_old","sources":{"background-task":{"state":"idle"}}}',
        encoding="utf-8",
    )
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    processor._clear_stale_omo_continuations(tmp_path)
    assert leftover.exists() is False
    assert (folder / "notes.txt").is_file()


def test_clear_stale_omo_continuations_missing_dir_ok(processor, tmp_path):
    processor._clear_stale_omo_continuations(tmp_path)
    processor._clear_stale_omo_continuations(None)
