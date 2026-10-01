"""Saved project remotes for the schedule New-issue picker."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.dashboard.project_repos import (
    label_from_repo_url,
    merge_project_repositories,
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

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / ".jira-agent"))
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


def test_parse_keeps_more_than_five_hundred_projects():
    rows = parse_project_repositories(
        [
            {
                "label": f"repo-{i}",
                "url": f"https://gitlab.example/acme/repo-{i}.git",
                "target_branch": "main",
            }
            for i in range(501)
        ]
    )
    assert len(rows) == 501
    assert rows[-1]["url"].endswith("/repo-500.git")


def test_append_keeps_projects_the_editor_has_not_loaded(tmp_path, monkeypatch):
    from src import config as config_mod

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / ".jira-agent"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_mod.settings,
        "project_repositories",
        json.dumps(
            [
                {
                    "label": "demo",
                    "url": "https://gitlab.com/acme/demo.git",
                    "target_branch": "main",
                    "source_branch": "",
                }
            ]
        ),
    )
    apply_settings_update(
        SettingsUpdate(
            project_repositories_append=[
                ProjectRepositoryItem(
                    label="other",
                    url="https://gitlab.com/acme/other.git",
                    target_branch="main",
                )
            ]
        )
    )
    stored = json.loads(config_mod.settings.project_repositories)
    assert [row["label"] for row in stored] == ["demo", "other"]
    renamed = merge_project_repositories(
        stored,
        [{"url": "https://gitlab.com/acme/demo.git", "label": "renamed"}],
    )
    assert renamed[0]["label"] == "renamed"
    assert renamed[1]["label"] == "other"


def test_settings_responses_include_saved_projects_without_token_import(monkeypatch):
    """Settings and the dashboard return the stored list. They do not call GitLab or Azure."""
    from fastapi.testclient import TestClient

    from src import config as config_mod
    from src.dashboard.api import create_dashboard_app

    monkeypatch.setattr(
        config_mod.settings,
        "project_repositories",
        json.dumps(
            [
                {
                    "label": "demo",
                    "url": "https://gitlab.com/acme/demo.git",
                    "target_branch": "main",
                    "source_branch": "",
                }
            ]
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.list_gitlab_repositories",
        lambda: calls.append("gitlab") or ([], []),
    )
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.list_azure_repositories",
        lambda: calls.append("azure") or ([], []),
    )
    monkeypatch.setattr(
        "src.dashboard.accessible_repos.save_runtime_settings",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.dashboard.service.save_runtime_settings",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.dashboard.service.upsert_dotenv_keys",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        config_mod.settings,
        "dashboard_username",
        "",
    )
    monkeypatch.setattr(
        config_mod.settings,
        "dashboard_password",
        "",
    )
    monkeypatch.setattr(
        config_mod.settings,
        "jira_trigger_label",
        getattr(config_mod.settings, "jira_trigger_label", "") or "",
    )
    client = TestClient(create_dashboard_app())
    boot = client.get("/api/settings")
    assert boot.status_code == 200
    assert boot.json()["project_repositories"][0]["label"] == "demo"
    assert "repository_sets" in boot.json()
    listed = client.get("/api/settings", params={"projects": "true"})
    assert listed.status_code == 200
    assert listed.json()["project_repositories"][0]["label"] == "demo"
    patched = client.patch("/api/settings", json={"jira_trigger_label": "bot"})
    assert patched.status_code == 200
    assert patched.json()["project_repositories"][0]["label"] == "demo"
    dashboard = client.get("/api/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["settings"]["project_repositories"][0]["label"] == "demo"
    assert calls == []
    imported = client.post("/api/settings/projects/import", json={})
    assert imported.status_code == 200
    assert imported.json()["project_repositories"][0]["label"] == "demo"
    assert calls == ["gitlab", "azure"]
