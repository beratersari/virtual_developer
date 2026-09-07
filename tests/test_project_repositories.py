"""Saved project remotes for the schedule New-issue picker."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.dashboard.project_repos import (
    label_from_repo_url,
    parse_project_repositories,
    project_repositories_to_json,
)
from src.dashboard.schemas import ProjectRepositoryItem, SettingsUpdate
from src.dashboard.service import apply_settings_update, build_settings_view


def test_label_from_https_and_ssh():
    assert (
        label_from_repo_url("https://gitlab.com/acme/demo.git") == "acme/demo"
    )
    assert label_from_repo_url("git@gitlab.com:acme/demo.git") == "acme/demo"
    assert label_from_repo_url("") == ""


def test_parse_dedupes_and_skips_junk():
    rows = parse_project_repositories(
        [
            {"url": "https://gitlab.com/g/r.git", "label": "demo"},
            {"url": "https://gitlab.com/g/r.git"},
            {"url": "not-a-url"},
            "https://gitlab.com/other/app.git",
        ]
    )
    assert [r["url"] for r in rows] == [
        "https://gitlab.com/g/r.git",
        "https://gitlab.com/other/app.git",
    ]
    assert rows[0]["label"] == "demo"
    assert rows[1]["label"] == "other/app"


def test_parse_json_string_and_single_url():
    encoded = project_repositories_to_json(
        [{"label": "x", "url": "https://gitlab.com/a/b.git", "target_branch": "develop"}]
    )
    assert json.loads(encoded)[0]["url"].endswith("/a/b.git")
    assert parse_project_repositories(encoded)[0]["target_branch"] == "develop"
    assert parse_project_repositories("https://gitlab.com/solo/r.git")[0]["url"].endswith(
        "/solo/r.git"
    )
    assert parse_project_repositories("") == []
    assert parse_project_repositories("not-json") == []


def test_schema_rejects_invalid_url():
    with pytest.raises(ValidationError):
        ProjectRepositoryItem(url="nope")
    item = ProjectRepositoryItem(url="https://gitlab.com/g/r.git", label="")
    assert item.url.endswith("/g/r.git")


def test_settings_update_persists_project_repositories(tmp_path, monkeypatch):
    from src import config as config_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod.settings, "project_repositories", "")

    view = apply_settings_update(
        SettingsUpdate(
            project_repositories=[
                ProjectRepositoryItem(
                    label="demo",
                    url="https://gitlab.com/acme/demo.git",
                    target_branch="develop",
                )
            ]
        )
    )
    assert len(view.project_repositories) == 1
    assert view.project_repositories[0].label == "demo"
    assert view.project_repositories[0].url.endswith("/acme/demo.git")
    assert "acme/demo" in config_mod.settings.project_repositories

    data = json.loads((tmp_path / ".jira-agent" / "runtime_settings.json").read_text())
    assert "project_repositories" in data

    config_mod.settings.project_repositories = ""
    from src.config import apply_runtime_settings_to

    apply_runtime_settings_to(config_mod.settings)
    reloaded = build_settings_view()
    assert len(reloaded.project_repositories) == 1
    assert reloaded.project_repositories[0].label == "demo"

    cleared = apply_settings_update(SettingsUpdate(project_repositories=[]))
    assert cleared.project_repositories == []
    assert json.loads(config_mod.settings.project_repositories) == []
