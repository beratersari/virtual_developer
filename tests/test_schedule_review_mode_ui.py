"""Scheduled MR/PR review is a form choice. Issue modes stay plan/build/test."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "src" / "pages" / "schedules" / "SchedulesPage.tsx"
CLIENT = ROOT / "web" / "src" / "api" / "client.ts"
TYPES = ROOT / "web" / "src" / "api" / "types.ts"


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_review_mode_is_only_on_mr_and_pr_forms():
    page = PAGE.read_text(encoding="utf-8")
    mr = _between(page, "function ExistingMr", "function ExistingPr")
    pr = _between(page, "function ExistingPr", "function azureCollectionsFromSettings")
    existing = _between(page, "function Existing(", "function featureBranchForKey")
    create = _between(page, "function CreateNew(", "function ScheduleWhenField")
    builtin = _between(page, "const BUILTIN_MODES = [", "]")

    assert "GitLab review" in mr
    assert "modeChoices" in mr
    assert "mode === 'review' ? 'review' : 'build'" in mr
    assert "default_review_model" in mr
    assert "Azure review" in pr
    assert "modeChoices" in pr
    assert "mode === 'review' ? 'review' : 'build'" in pr
    assert "default_review_model" in pr
    assert "MR review" in page
    assert "PR review" in page
    assert "review" not in builtin
    assert "GitLab review" not in existing
    assert "Azure review" not in existing
    assert "modeChoices" not in existing
    assert "GitLab review" not in create
    assert "Azure review" not in create
    assert "modeChoices" not in create

    client = CLIENT.read_text(encoding="utf-8")
    mr_client = _between(client, "export function scheduleMrFollowup", "export function previewSchedulePr")
    pr_client = _between(client, "export function schedulePrFollowup", "export function fetchAzureProjects")
    assert "if (body.mode) payload.mode = body.mode" in mr_client
    assert "if (body.mode) payload.mode = body.mode" in pr_client
    types = TYPES.read_text(encoding="utf-8")
    mr_body = _between(types, "export type ScheduleMrBody = {", "}")
    pr_body = _between(types, "export type SchedulePrBody = {", "}")
    assert "mode?:" in mr_body
    assert "mode?:" in pr_body
