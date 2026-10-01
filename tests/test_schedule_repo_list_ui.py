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
    assert "Saved repository" in dialog
    assert "Repository URL" in dialog
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
