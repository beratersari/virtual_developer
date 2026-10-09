"""Schedule repository list: set add, popup branches, pencil and trash."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIST = ROOT / "web" / "src" / "pages" / "schedules" / "MoreRepositories.tsx"
ROWS = ROOT / "web" / "src" / "pages" / "schedules" / "repoRows.test.ts"


def test_repositories_are_a_list_with_popup_branches():
    text = LIST.read_text(encoding="utf-8")
    listed = text.split('aria-label="Repositories"', 1)[1].split("</ul>", 1)[0]
    dialog = text.split("function RepositoryDialog", 1)[1]
    assert "Add projects from a set" in text
    assert "appendRepoRows" in text
    assert "Add repository" in text
    assert "No repositories yet." in text
    assert "PencilIcon" in listed
    assert "TrashIcon" in listed
    assert ">Remove<" not in listed
    assert "Source branch" not in listed
    assert "Target branch" not in listed
    assert 'role="dialog"' in dialog
    assert 'label="Repository"' in dialog
    assert "<span>URL</span>" not in dialog
    assert 'label="Name"' not in dialog
    assert "Saved repository" not in dialog
    assert "Repository URL" not in dialog
    assert "onDirectUrl" in dialog
    assert "repoRowForUrl" in dialog
    assert "Source branch" in dialog
    assert "Custom branch" in dialog
    assert "Named branch" not in text
    assert "Target branch" in dialog
    assert "Add URL" not in text
    assert "The ticket names only the first one." not in text
    assert "The ticket records each repository's source branch." in text


def test_new_issue_repository_section_starts_empty():
    page = (ROOT / "web" / "src" / "pages" / "schedules" / "SchedulesPage.tsx").read_text(
        encoding="utf-8"
    )
    create = page.split("function CreateNew", 1)[1].split("function ScheduleWhenField", 1)[0]
    assert "rowFromProject" not in create
    assert "rows.length === 1" not in create
    assert "getItem(LAST_REPO_KEY)" not in create
    assert "setItem(LAST_REPO_KEY" in create
    assert "No repositories yet." in (ROOT / "web" / "src" / "pages" / "schedules" / "MoreRepositories.tsx").read_text(
        encoding="utf-8"
    )


def test_saved_repo_menu_stays_on_screen():
    search = (ROOT / "web" / "src" / "ui" / "ProjectSelect.tsx").read_text(encoding="utf-8")
    assert "repoSearchListPlacement" in search
    assert "createPortal" in search
    assert "document.body" in search
    assert "position: 'fixed'" in search
    settings = (ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    assert (
        'className="max-h-40 divide-y divide-border overflow-y-auto" '
        'aria-label="Repositories in this set"'
    ) in settings
    css = (ROOT / "web" / "src" / "index.css").read_text(encoding="utf-8")
    modal = css.split(".vd-modal {", 1)[1].split("}", 1)[0]
    assert "max-height: calc(100vh - 2rem);" in modal
    assert "overflow-y: auto;" in modal


def test_saved_repo_search_stays_closed_until_click():
    search = (ROOT / "web" / "src" / "ui" / "ProjectSelect.tsx").read_text(encoding="utf-8")
    field = search.split("<input", 1)[1].split("/>", 1)[0]
    assert "onClick" in field
    assert "onFocus" not in field
    assert "autoFocus" not in field
    dialog = LIST.read_text(encoding="utf-8").split("<SavedRepoSearch", 1)[1].split("/>", 1)[0]
    assert "autoFocus" not in dialog


def test_select_all_adds_the_matching_repositories():
    search = (ROOT / "web" / "src" / "ui" / "ProjectSelect.tsx").read_text(encoding="utf-8")
    assert "Select all" in search
    assert "Select all matching repositories" in search
    assert "onSelectAll && pickable.length > 0" in search
    assert "selectAllMatching" in search
    settings = (ROOT / "web" / "src" / "pages" / "settings" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    repo_set = settings.split("<SavedRepoSearch", 1)[1].split("/>", 1)[0]
    assert 'label="Repository"' in repo_set
    assert "onSelectAll" in repo_set
    assert "onDirectUrl" in repo_set
    assert "Add a project first." not in settings
    assert "<span>URL</span>" not in settings.split("<SavedRepoSearch", 1)[1].split(
        "Select at least two", 1
    )[0]
    page = (ROOT / "web" / "src" / "pages" / "schedules" / "SchedulesPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "Other URL" not in page
    assert "quiet font-mono" not in page
    assert page.count('label="Repository"') >= 2
    assert "<span>URL</span>" not in page
    assert "onDirectUrl" in page
    search = (ROOT / "web" / "src" / "ui" / "ProjectSelect.tsx").read_text(encoding="utf-8")
    assert 'text-xs text-text-muted">{project.url}' not in search
    dialog = LIST.read_text(encoding="utf-8").split("function RepositoryDialog", 1)[1]
    assert "onSelectAll={state.mode === 'add' ? onAddMatching : undefined}" in dialog
    assert "rowsForSelectedUrls" in LIST.read_text(encoding="utf-8")
    page = (ROOT / "web" / "src" / "pages" / "schedules" / "SchedulesPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "onSelectAll" not in page


def test_repo_row_helpers():
    completed = subprocess.run(
        ["npx", "tsx", str(ROWS)],
        cwd=ROOT / "web",
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "ok" in completed.stdout
