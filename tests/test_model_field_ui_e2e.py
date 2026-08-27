"""E2E contract: Model id text field stays hidden until Other id… is selected.

Operators saw a second "Model id" box on Settings and Schedule forms
even when a listed model (or Settings default) was selected. The field
must appear only after the select is set to Other id….
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app


ROOT = Path(__file__).resolve().parents[1]
MODEL_FIELD = ROOT / "web" / "src" / "ui" / "ModelField.tsx"
PICKER = ROOT / "web" / "src" / "util" / "modelPicker.ts"
SCHEDULES = ROOT / "web" / "src" / "pages" / "schedules" / "SchedulesPage.tsx"
SETTINGS = ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx"


def test_e2e_model_id_input_only_when_other_id_selected():
    field = MODEL_FIELD.read_text(encoding="utf-8")
    picker = PICKER.read_text(encoding="utf-8")

    assert "const showInput = showCustomModelId(selectValue)" in field
    assert "|| isCodex || !allowEmpty" not in field
    assert "|| isCodex" not in field.split("const showInput")[1].split("return")[0]
    assert "allowEmpty" not in field.split("const showInput")[1].split("return")[0]

    assert "export function showCustomModelId" in picker
    assert "selectValue === CUSTOM_MODEL" in picker


def test_e2e_schedule_and_settings_use_shared_model_field():
    schedules = SCHEDULES.read_text(encoding="utf-8")
    settings = SETTINGS.read_text(encoding="utf-8")
    assert schedules.count("<ModelField") >= 2
    assert "<ModelField" in settings
    assert 'label="Default model"' in settings


def test_e2e_model_field_disabled_until_inventory_loads():
    """Backend change refetches models; picker and related actions stay locked."""
    field = MODEL_FIELD.read_text(encoding="utf-8")
    schedules = SCHEDULES.read_text(encoding="utf-8")
    settings = SETTINGS.read_text(encoding="utf-8")

    assert "const loading = fetching || loadedWorker !== worker" in field
    assert "disabled={loading}" in field
    assert "onLoadingChange?: (loading: boolean) => void" in field
    assert "onLoadingChange?.(loading)" in field

    assert settings.count("onLoadingChange={setModelsLoading}") == 1
    assert "setModelsLoading(true)" in settings
    assert "if (!draft || modelsLoading) return" in settings
    assert "disabled={saving || modelsLoading || (!dirty && !saved)}" in settings
    assert "Loading models…" in settings

    assert schedules.count("onLoadingChange={setModelsLoading}") == 2
    assert schedules.count("setModelsLoading(true)") >= 3
    assert "disabled={busy || modelsLoading}" in schedules
    assert schedules.count("disabled={busy || modelsLoading}") >= 4
    assert "Loading models…" in schedules


def test_e2e_models_api_lists_settings_default_without_forcing_custom_id(monkeypatch):
    """Schedule/Settings dropdown can pick a listed id; empty custom is valid."""
    from unittest.mock import patch

    from src.config import settings as app_settings
    from src.opencode_models import ModelInfo

    monkeypatch.setattr(app_settings, "default_model", "opencode/hy3-free")
    monkeypatch.setattr(app_settings, "agent_backend", "opencode")
    listed = [
        ModelInfo(id="opencode/hy3-free", name="HY3", provider="opencode"),
        ModelInfo(id="opencode/other", name="Other", provider="opencode"),
    ]
    app = create_dashboard_app()
    client = TestClient(app)
    with patch(
        "src.dashboard.service.list_available_models",
        return_value=(listed, None, None, "opencode/hy3-free"),
    ):
        r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert body.get("default_model") == "opencode/hy3-free"
    ids = [m.get("id") for m in (body.get("models") or [])]
    assert "opencode/hy3-free" in ids
