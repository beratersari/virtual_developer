"""Tests for OpenCode model inventory (CLI + config)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.opencode_models import (
    clear_models_cache,
    list_available_models,
    load_opencode_config,
    models_from_cli,
    models_from_opencode_config,
    write_workspace_context_limit,
    _strip_jsonc,
)


def test_strip_jsonc_removes_comments():
    raw = '{\n  // comment\n  "model": "a/b", /* block */\n  "x": 1\n}\n'
    data = json.loads(_strip_jsonc(raw))
    assert data["model"] == "a/b"
    assert data["x"] == 1


def test_models_from_config_providers(tmp_path, monkeypatch):
    cfg = {
        "model": "local/my-model",
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://127.0.0.1:8080/v1"},
                "models": {
                    "my-model": {"name": "My Model"},
                    "other": {},
                },
            }
        },
    }
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(
        "src.opencode_models.opencode_config_candidates",
        lambda: [path],
    )
    found, data = load_opencode_config()
    assert found == path
    items, default = models_from_opencode_config(data)
    assert default == "local/my-model"
    ids = {m.id for m in items}
    assert "local/my-model" in ids
    assert "local/other" in ids
    named = next(m for m in items if m.id == "local/my-model")
    assert named.name == "My Model"
    assert named.source in ("config_default", "config")


def test_models_from_cli_parses_lines():
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "opencode/a\nopencode/b\n"
    mock.stderr = ""
    with patch("src.opencode_models.subprocess.run", return_value=mock):
        items, err = models_from_cli()
    assert err is None
    assert [m.id for m in items] == ["opencode/a", "opencode/b"]


def test_list_available_models_merges_sources(tmp_path, monkeypatch):
    clear_models_cache()
    cfg = {
        "model": "cfg/default",
        "provider": {
            "cfg": {
                "models": {
                    "default": {"name": "Cfg Default"},
                    "extra": {"name": "Extra"},
                }
            }
        },
    }
    path = tmp_path / "opencode.jsonc"
    path.write_text(
        '// header\n' + json.dumps(cfg),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.opencode_models.opencode_config_candidates",
        lambda: [path],
    )
    from src.config import settings

    monkeypatch.setattr(settings, "default_model", "runtime/current")

    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "opencode/free-one\ncfg/extra\n"
    mock.stderr = ""
    with patch("src.opencode_models.subprocess.run", return_value=mock):
        items, err, cfg_path, cfg_model = list_available_models(refresh=True)

    assert err is None
    assert cfg_path == str(path)
    assert cfg_model == "cfg/default"
    ids = {m.id for m in items}
    assert "runtime/current" in ids
    assert "cfg/default" in ids
    assert "cfg/extra" in ids
    assert "opencode/free-one" in ids
    clear_models_cache()


def test_write_workspace_context_limit_excludes_from_git(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    path = write_workspace_context_limit(
        tmp_path,
        model="opencode/hy3-free",
        context_limit=32768,
    )
    assert path == tmp_path / "opencode.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "opencode/hy3-free"
    assert data["provider"]["opencode"]["models"]["hy3-free"]["limit"]["context"] == 32768
    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/opencode.json" in exclude
    # Second write must not duplicate the exclude line
    write_workspace_context_limit(
        tmp_path, model="opencode/hy3-free", context_limit=16384
    )
    exclude2 = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude2.count("/opencode.json") == 1


def test_settings_and_models_endpoints_separated(monkeypatch):
    clear_models_cache()
    from src.config import settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import (
        apply_settings_update,
        build_models_response,
        build_settings_view,
    )
    from src.opencode_models import ModelInfo

    monkeypatch.setattr(settings, "default_model", "old/model")
    view = apply_settings_update(SettingsUpdate(default_model="new/provider-model"))
    assert settings.default_model == "new/provider-model"
    assert view.default_model == "new/provider-model"
    assert not hasattr(view, "available_models") or "available_models" not in view.model_dump()

    # Settings view stays free of inventory work
    dumped = build_settings_view().model_dump()
    assert "models" not in dumped
    assert dumped["default_model"] == "new/provider-model"

    with patch(
        "src.dashboard.service.list_available_models",
        return_value=(
            [
                ModelInfo(
                    id="new/provider-model",
                    name="New",
                    provider="new",
                    source="settings",
                )
            ],
            "cli missing",
            "/tmp/x.json",
            "x/y",
        ),
    ):
        models = build_models_response()
        assert models.error == "cli missing"
        assert models.opencode_config_path == "/tmp/x.json"
        assert models.models[0].label
        assert models.default_model == "new/provider-model"
    clear_models_cache()
