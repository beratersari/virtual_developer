"""Settings UI contracts for the modes toolbar and the save-error popup."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODES = ROOT / "web" / "src" / "pages" / "settings" / "ModesPanel.tsx"
SETTINGS = ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx"
COLLECTION_TEST = (
    ROOT / "web" / "src" / "pages" / "settings" / "azureCollection.test.ts"
)


def test_mode_rows_do_not_edit_the_agent():
    text = MODES.read_text(encoding="utf-8")
    card = text.split("{modes.map", 1)[1].split(
        '<div className="flex flex-wrap gap-2">', 1
    )[0]
    assert "Edit agent text" not in text
    assert "openAgent(row.agent)" not in card
    assert "Remove mode" in card


def test_edit_agents_sits_beside_create_agent():
    text = MODES.read_text(encoding="utf-8")
    toolbar = text.split('<div className="flex flex-wrap gap-2">', 1)[1].split(
        "</div>", 1
    )[0]
    assert toolbar.index("Create agent") < toolbar.index("Edit agents")
    assert "onClick={openEdit}" in toolbar
    assert "onClick={openCreate}" in toolbar


def test_settings_save_error_opens_a_popup():
    text = SETTINGS.read_text(encoding="utf-8")
    assert 'title="Could not save"' in text
    assert "<ConfirmDialog" in text
    assert "azureCollectionProblem(host)" in text
    assert '{error && <p className="err">' not in text


def test_bare_collection_name_is_rejected_in_the_page_check():
    completed = subprocess.run(
        ["npx", "tsx", str(COLLECTION_TEST)],
        cwd=ROOT / "web",
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ok" in completed.stdout
