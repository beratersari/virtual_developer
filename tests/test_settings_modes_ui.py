"""Settings UI contracts for modes, save errors, and saved projects."""

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
SAVED_PROJECTS_TEST = (
    ROOT / "web" / "src" / "pages" / "settings" / "savedProjects.test.ts"
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


def _run_tsx(script: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "tsx", str(script)],
        cwd=ROOT / "web",
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )


def test_edit_dialogs_leave_delete_on_the_trash_icon():
    text = SETTINGS.read_text(encoding="utf-8")
    repo = text.split("function RepoSetList", 1)[1].split("function RowActions", 1)[0]
    actions = repo.split('className="vd-modal-actions"', 1)[1].split("</div>", 1)[0]
    assert ">Remove<" not in actions
    assert "onRemove:" not in repo
    listed = repo.split('aria-label="Repositories in this set"', 1)[1].split(
        "</ul>", 1
    )[0]
    assert "TrashIcon" in listed
    assert ">Remove<" not in listed
    projects = text.split("function ProjectRepoList", 1)[1].split(
        "export function SettingsPage", 1
    )[0]
    project_actions = projects.split('className="vd-modal-actions"', 1)[1].split(
        "</div>", 1
    )[0]
    assert ">Remove<" not in project_actions


def test_saved_projects_can_be_searched_by_name_and_deleted_together():
    text = SETTINGS.read_text(encoding="utf-8")
    card = text.split("function ProjectRepoList", 1)[1].split(
        "export function SettingsPage", 1
    )[0]
    assert 'placeholder="Name"' in card
    assert "Select all" in card
    assert "Delete selected" in card
    assert "No projects match that name." in card
    assert "filterSavedProjects" in card
    assert "Reload from tokens" in card
    assert 'aria-label="Add project"' in card
    completed = _run_tsx(SAVED_PROJECTS_TEST)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ok" in completed.stdout


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
