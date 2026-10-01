"""Saved projects and repo sets survive a rewrite of runtime_settings.json."""

from __future__ import annotations

import json

from src.dashboard.schemas import ProjectRepositoryItem, RepositorySetItem, SettingsUpdate
from src.dashboard.service import apply_settings_update


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.chdir(tmp_path)


def test_catalog_keeps_projects_and_sets_when_runtime_settings_drops_them(
    tmp_path, monkeypatch
):
    from src import config as config_mod
    from src.config import apply_runtime_settings_to, runtime_settings_path
    from src.dashboard.saved_catalog import saved_catalog_path

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(config_mod.settings, "project_repositories", "")
    monkeypatch.setattr(config_mod.settings, "repository_sets", "")

    apply_settings_update(
        SettingsUpdate(
            project_repositories=[
                ProjectRepositoryItem(
                    label="demo",
                    url="https://gitlab.com/acme/demo.git",
                    target_branch="develop",
                )
            ],
            repository_sets=[
                RepositorySetItem(
                    name="Orders",
                    repositories=[
                        "https://gitlab.com/acme/api.git",
                        "https://gitlab.com/acme/web.git",
                    ],
                )
            ],
        )
    )
    catalog = saved_catalog_path()
    assert catalog.is_file()
    stored = json.loads(catalog.read_text(encoding="utf-8"))
    assert stored["project_repositories"][0]["label"] == "demo"
    assert stored["repository_sets"][0]["name"] == "Orders"
    before = catalog.read_text(encoding="utf-8")

    apply_settings_update(SettingsUpdate(jira_board_id="2"))
    assert catalog.read_text(encoding="utf-8") == before

    runtime = runtime_settings_path()
    raw = json.loads(runtime.read_text(encoding="utf-8"))
    raw.pop("project_repositories", None)
    raw.pop("repository_sets", None)
    runtime.write_text(json.dumps(raw), encoding="utf-8")

    config_mod.settings.project_repositories = ""
    config_mod.settings.repository_sets = ""
    apply_runtime_settings_to(config_mod.settings)
    assert "acme/demo" in config_mod.settings.project_repositories
    assert "Orders" in config_mod.settings.repository_sets

    cleared = apply_settings_update(SettingsUpdate(project_repositories=[]))
    assert cleared.project_repositories == []
    kept = json.loads(catalog.read_text(encoding="utf-8"))
    assert kept["project_repositories"] == []
    assert kept["repository_sets"][0]["name"] == "Orders"


def test_missing_catalog_is_seeded_from_runtime_settings(tmp_path, monkeypatch):
    from src.config import Settings, apply_runtime_settings_to, runtime_settings_path
    from src.dashboard.saved_catalog import saved_catalog_path

    _isolate(tmp_path, monkeypatch)
    path = runtime_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project_repositories": json.dumps(
                    [
                        {
                            "label": "demo",
                            "url": "https://gitlab.com/acme/demo.git",
                            "target_branch": "develop",
                            "source_branch": "",
                        }
                    ]
                ),
                "repository_sets": json.dumps(
                    [
                        {
                            "name": "Orders",
                            "repositories": [
                                "https://gitlab.com/acme/api.git",
                                "https://gitlab.com/acme/web.git",
                            ],
                        }
                    ]
                ),
            }
        ),
        encoding="utf-8",
    )
    catalog = saved_catalog_path()
    assert not catalog.exists()

    fresh = Settings(
        project_repositories="",
        repository_sets="",
        _env_file=None,
    )
    apply_runtime_settings_to(fresh)
    assert catalog.is_file()
    stored = json.loads(catalog.read_text(encoding="utf-8"))
    assert stored["project_repositories"][0]["label"] == "demo"
    assert stored["repository_sets"][0]["name"] == "Orders"
    assert "acme/demo" in fresh.project_repositories
    assert "Orders" in fresh.repository_sets
