"""Daily-usage integration proofs from a full-tree review (2026-09-11).

Fixed items assert the new behaviour. Intentional items (Jira Cloud,
HTTPS GitLab MR REST, plan_refactor reuse) are documented, not treated
as bugs. Remaining xfail rows are still open.

Run::

  python -m pytest tests/test_daily_usage_deep_review.py -v
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.issue_git_spec import require_issue_git_spec


REAL_REPO = "https://gitlab.example.com/group/test_project.git"


def _params_adf_with_repo_node(repo_node: dict) -> dict:
    """Cloud visual-editor shape: each {params} line is its own paragraph."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "{params}"}]},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Repository: "},
                    repo_node,
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Source branch: develop"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Target branch: main"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Mode: plan"}],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "{params}"}]},
        ],
    }


def _inline_card(url: str) -> dict:
    return {"type": "inlineCard", "attrs": {"url": url}}


def _link_mark_display(text: str, href: str) -> dict:
    return {
        "type": "text",
        "text": text,
        "marks": [{"type": "link", "attrs": {"href": href}}],
    }


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


@pytest.fixture
def poller(state_manager, fake_jira):
    from src.jira.poller import JiraPoller

    p = JiraPoller(client=fake_jira, interval_seconds=1, board_id="1")
    p.state_manager = state_manager
    p._status_before_poll = {}
    p._last_jira_status = {}
    p._seen_issues = set()
    return p


# ---------------------------------------------------------------------------
# F1 — Jira Cloud smart-link / inlineCard repository is dropped on intake
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Jira Cloud is out of scope (on-prem Server/DC only)")
def test_f1_cloud_inline_card_repo_must_survive_jira_refresh_and_git_parse(
    processor, fake_jira
):
    """Daily Cloud path: visual editor stores the repo as ADF inlineCard.

    Git init re-reads the live issue (``_refresh_issue_text_from_jira``) then
    ``require_issue_git_spec``. The URL lives in ``attrs.url``, not a text
    node. Flattening must keep it or every Cloud smart-linked ticket fails
    template validation.
    """
    adf = _params_adf_with_repo_node(_inline_card(REAL_REPO))
    fake_jira.get_issue = MagicMock(
        return_value={
            "key": "CLOUD-1",
            "fields": {"summary": "Plan login", "description": adf},
        }
    )
    processor.state_manager.create_state("CLOUD-1", "Plan login", "")

    summary, description = processor._refresh_issue_text_from_jira("CLOUD-1")
    spec = require_issue_git_spec(summary=summary, description=description)

    assert spec.repository_url.rstrip("/") == REAL_REPO.rstrip("/")
    assert spec.source_branch == "develop"
    assert spec.target_branch == "main"
    assert spec.mode == "plan"


@pytest.mark.skip(reason="Jira Cloud is out of scope (on-prem Server/DC only)")
def test_f1_cloud_link_mark_href_must_be_used_when_display_text_is_not_a_url(
    processor, fake_jira
):
    """Cloud often shows the project name and puts the git URL on the link mark."""
    adf = _params_adf_with_repo_node(
        _link_mark_display("test_project", REAL_REPO)
    )
    fake_jira.get_issue = MagicMock(
        return_value={
            "key": "CLOUD-2",
            "fields": {"summary": "Plan login", "description": adf},
        }
    )
    processor.state_manager.create_state("CLOUD-2", "Plan login", "")

    summary, description = processor._refresh_issue_text_from_jira("CLOUD-2")
    spec = require_issue_git_spec(summary=summary, description=description)
    assert spec.repository_url.rstrip("/") == REAL_REPO.rstrip("/")


@pytest.mark.skip(reason="Jira Cloud is out of scope (on-prem Server/DC only)")
@pytest.mark.asyncio
async def test_f1_handle_issue_created_must_store_parseable_params_from_adf(
    processor, fake_jira
):
    """Poller/create path uses ``_issue_text`` then starts the workflow."""
    adf = _params_adf_with_repo_node(_inline_card(REAL_REPO))
    started = {}

    async def fake_start(state):
        started["description"] = state.description
        started["summary"] = state.issue_summary

    processor._start_planning_workflow = fake_start  # type: ignore[method-assign]
    processor._start_execution_workflow = fake_start  # type: ignore[method-assign]

    event = {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "CLOUD-3",
            "fields": {
                "summary": "Plan login",
                "description": adf,
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": [],
                "assignee": {"displayName": "Jira AI Bot"},
            },
        },
    }
    started_flag, skip = await processor._handle_issue_created(event)
    assert skip is None
    assert started_flag is True
    spec = require_issue_git_spec(
        summary=started.get("summary") or "",
        description=started.get("description") or "",
    )
    assert spec.repository_url.rstrip("/") == REAL_REPO.rstrip("/")
    assert spec.mode == "plan"


# ---------------------------------------------------------------------------
# F2 — Storage Delete removes a clone a running job still owns
# ---------------------------------------------------------------------------


def test_f2_storage_api_must_refuse_delete_of_live_clone(tmp_path, monkeypatch):
    """Storage Delete must refuse a clone a running job still owns."""
    from src.config import settings
    from src.dashboard.api import create_dashboard_app
    from src.dashboard.temp_storage import (
        force_delete_temp_folder,
        reset_delete_jobs,
        reset_size_cache,
    )
    from src.git_manager import GitManager

    base = tmp_path / "clones"
    clone = base / "repo_livejob12"
    clone.mkdir(parents=True)
    (clone / "README").write_text("worktree", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", str(base))
    monkeypatch.chdir(tmp_path)
    reset_delete_jobs()
    reset_size_cache()

    gm = SimpleNamespace(temp_dir=clone, issue_key="LIVE-9")
    previous = GitManager._live_by_issue.get("LIVE-9")
    GitManager._live_by_issue["LIVE-9"] = gm
    try:
        app = create_dashboard_app()
        client = TestClient(app)
        listed = client.get("/api/storage")
        assert listed.status_code == 200
        rows = listed.json().get("folders") or []
        live_row = next(r for r in rows if r.get("name") == "repo_livejob12")
        assert live_row.get("in_use") is True

        deleted = client.post(
            "/api/storage/delete", json={"name": "repo_livejob12"}
        )
        assert deleted.status_code in {400, 409}, (
            f"live clone delete must be refused, got {deleted.status_code} "
            f"{deleted.text}"
        )
        assert clone.is_dir(), "live clone folder must still exist"

        from src.dashboard.temp_storage import TempStorageError

        with pytest.raises(TempStorageError):
            force_delete_temp_folder("repo_livejob12")
        assert clone.is_dir()
        page = (
            Path(__file__).resolve().parents[1]
            / "web"
            / "src"
            / "pages"
            / "storage"
            / "StoragePage.tsx"
        ).read_text(encoding="utf-8")
        assert "folder.in_use" in page
        assert "disabled={isDeleting || folder.in_use}" in page
    finally:
        if previous is None:
            GitManager._live_by_issue.pop("LIVE-9", None)
        else:
            GitManager._live_by_issue["LIVE-9"] = previous
        reset_delete_jobs()
        reset_size_cache()


# ---------------------------------------------------------------------------
# F3 — Schedule list_due / poller pending-check only see the newest 500 files
# ---------------------------------------------------------------------------


def _write_schedule(store, *, issue_key: str, scheduled_at: str, mtime: float):
    rec = store.create(
        title=issue_key,
        description="",
        repository_url=REAL_REPO,
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at=scheduled_at,
        issue_key=issue_key,
        issue_description="x",
    )
    path = store._path(rec["schedule_id"])
    os.utime(path, (mtime, mtime))
    return rec


@pytest.mark.xfail(
    strict=True,
    reason="list_due only inspects the 500 newest schedule files",
)
def test_f3_list_due_must_include_older_due_row_hidden_by_500_newer_files(tmp_path):
    """Daemon dispatch uses ``ScheduleStore.list_due``.

    ``list_due`` calls ``list_schedules(status='scheduled', limit=500)``,
    which keeps the 500 newest *files* (mtime). A due row whose file is
    older than 500 future schedules never fires.
    """
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    now = datetime.now()
    due_at = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    future_at = (now + timedelta(days=7)).isoformat(timespec="seconds")
    base = time.time()

    due = _write_schedule(
        store, issue_key="DUE-1", scheduled_at=due_at, mtime=base - 10_000
    )
    for i in range(500):
        _write_schedule(
            store,
            issue_key=f"FUT-{i}",
            scheduled_at=future_at,
            mtime=base + i,
        )

    due_ids = {r["schedule_id"] for r in store.list_due(now=now)}
    assert due["schedule_id"] in due_ids, (
        "due schedule older than the 500 newest files must still fire"
    )


@pytest.mark.xfail(
    strict=True,
    reason="_issue_has_pending_schedule only scans 500 newest scheduled files",
)
def test_f3_poller_must_honor_pending_schedule_beyond_newest_500(
    poller, tmp_path, monkeypatch
):
    """To Do + bot assignee is rework — except when a schedule is still waiting.

    ``_issue_has_pending_schedule`` also scans only 500 newest scheduled
    rows. An older waiting schedule is invisible, so the poller starts the
    ticket immediately instead of waiting for fire time.
    """
    from src.config import settings
    from src.state.schedule_store import ScheduleStore

    monkeypatch.setattr(settings, "jira_trigger_label", "")
    monkeypatch.setattr(settings, "trigger_labels", "")
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    poller.schedule_store = store

    now = datetime.now()
    wait_at = (now + timedelta(hours=6)).isoformat(timespec="seconds")
    base = time.time()
    _write_schedule(
        store, issue_key="WAIT-9", scheduled_at=wait_at, mtime=base - 10_000
    )
    for i in range(500):
        _write_schedule(
            store,
            issue_key=f"OTHER-{i}",
            scheduled_at=wait_at,
            mtime=base + i,
        )

    assert poller._issue_has_pending_schedule("WAIT-9") is True

    issue = {
        "key": "WAIT-9",
        "fields": {
            "summary": "scheduled later",
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "labels": [],
            "assignee": {"displayName": "Jira AI Bot"},
        },
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.sprint_lookup = "kanban"
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    keys = [i["key"] for i in poller.poll_board()]
    assert "WAIT-9" not in keys, (
        "pending schedule must suppress poller intake until scheduled_at"
    )


# ---------------------------------------------------------------------------
# F4 — Azure identity failed lookup is cached forever
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="fetch_bot_identity caches empty {} on failure keyed only by bool(token)",
)
def test_f4_failed_azure_identity_lookup_must_retry_on_next_comment(monkeypatch):
    """TFS down / bad PAT caches ``{}`` under ``roots|True``.

    The next ``@<GUID> /yaver`` after the operator fixes the PAT (or TFS
    recovers) must retry connectionData. Caching only ``bool(token)`` means
    a new PAT still hits the empty entry until daemon restart.
    """
    from src.azure import identity as ident

    ident.reset_identity_cache()
    clients = {"n": 0}

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {
                "authenticatedUser": {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "providerDisplayName": "Yaver Bot",
                    "uniqueName": "DOMAIN\\\\yaver",
                }
            }

    class _Client:
        def __init__(self, *a, **k):
            clients["n"] += 1
            self.fail = clients["n"] == 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if self.fail:
                raise ident.httpx.HTTPError("connection refused")
            return _Resp()

    monkeypatch.setattr(ident.httpx, "Client", _Client)
    first = ident.fetch_bot_identity(
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        host="https://tfs.example.com",
        pat="old-pat",
    )
    assert first is None
    second = ident.fetch_bot_identity(
        collection_url="https://tfs.example.com/tfs/DefaultCollection",
        host="https://tfs.example.com",
        pat="new-pat",
    )
    ident.reset_identity_cache()
    assert second is not None, (
        "failed identity lookup must not be cached across a later retry / PAT fix"
    )
    assert (second.get("id") or second.get("names")) 


# ---------------------------------------------------------------------------
# F5 — Cancelling a claimed-but-not-started schedule kills another live job
# ---------------------------------------------------------------------------


def test_f5_cancel_dispatching_schedule_is_refused(tmp_path):
    """``dispatching`` cannot be cancelled (would abort a live job)."""
    from src.scheduler.service import cancel_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="later follow-up",
        description="",
        repository_url=REAL_REPO,
        source_branch="develop",
        target_branch="main",
        mode="build",
        scheduled_at=(datetime.now() + timedelta(minutes=1)).isoformat(
            timespec="seconds"
        ),
        issue_key="KAN-77",
        issue_description="x",
    )
    store.update(rec["schedule_id"], status="dispatching")

    cancelled = {"n": 0}

    class _Proc:
        def cancel_job(self, issue_key, reason=""):
            cancelled["n"] += 1
            cancelled["key"] = issue_key
            cancelled["reason"] = reason

    out = cancel_scheduled_job(
        rec["schedule_id"], store=store, processor=_Proc()
    )
    assert out["ok"] is False
    assert "dispatching" in (out.get("error") or "")
    assert (store.get(rec["schedule_id"]) or {}).get("status") == "dispatching"
    assert cancelled["n"] == 0


# ---------------------------------------------------------------------------
# F6 — HTTP GitLab MR REST always uses https://
# ---------------------------------------------------------------------------


def test_f6_http_gitlab_mr_api_uses_https_intentionally(tmp_path, monkeypatch):
    """INTENTIONAL: GitLab REST MR create is always HTTPS."""
    from src.git_manager import GitManager

    captured = {}

    class _Resp:
        status_code = 201
        text = ""

        def json(self):
            return {"web_url": "http://gitlab.corp:8091/g/r/-/merge_requests/3"}

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            return _Resp()

    gm = GitManager(
        issue_key=None,
        remote_url="http://gitlab.corp:8091/group/repo.git",
        source_branch="feature/KAN-1",
        target_branch="main",
    )
    monkeypatch.setattr(gm, "_pat_for_remote", lambda url: "pat-token")
    monkeypatch.setattr(gm, "_assert_remote_host_allowed", lambda url: None)
    import src.git_manager as gm_mod

    monkeypatch.setattr(gm_mod.httpx, "Client", _Client) if hasattr(
        gm_mod, "httpx"
    ) else None
    import httpx as httpx_mod

    monkeypatch.setattr(httpx_mod, "Client", _Client)
    url = gm._create_mr_via_api(
        "feat(KAN-1): x", "body", "feature/KAN-1", "main"
    )
    posted = captured.get("url") or ""
    assert posted.startswith("https://gitlab.corp:8091/"), (
        f"GitLab REST must stay HTTPS, got {posted!r}"
    )
    assert url is not None


# ---------------------------------------------------------------------------
# F7 — Cloud ADF mention chip does not count as tagging the bot
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Jira Cloud is out of scope (on-prem Server/DC only)")
def test_f7_cloud_adf_mention_chip_must_count_as_plan_refactor_tag():
    """Cloud comment chip: mention node with display name + accountId.

    ``JIRA_TRIGGER_USER=devbot`` /myself is ``Jira AI Bot`` + accountId.
    Flattening must not drop the accountId, and the display-name chip
    must still match so plan_refactor does not wait forever.
    """
    from src.jira.plan_labels import latest_comment_tagging_pat_user

    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "mention",
                        "attrs": {
                            "id": "557058:abc",
                            "text": "@Jira AI Bot",
                        },
                    },
                    {"type": "text", "text": " lütfen cache ekle"},
                ],
            }
        ],
    }
    comments = [{"id": "c1", "body": adf}]
    myself = {
        "name": "devbot",
        "displayName": "Jira AI Bot",
        "accountId": "557058:abc",
    }
    body = latest_comment_tagging_pat_user(
        comments,
        myself=myself,
        mention_tokens=["@devbot"],
        extra_needles=["devbot"],
    )
    assert body is not None
    assert "cache" in body.lower()


# ---------------------------------------------------------------------------
# F8 — Turkish dotted İ does not match configured ASCII assignee
# ---------------------------------------------------------------------------


def test_f8_turkish_dotted_i_assignee_must_match_ascii_trigger(monkeypatch):
    """``JIRA_TRIGGER_USER=irem`` vs Jira displayName ``İrem`` (U+0130).

    Python ``.lower()`` turns İ into i+combining-dot, so substring match
    fails and a To Do ticket assigned to the bot is never taken.
    """
    from src.config import settings
    from src.jira.triggers import assignee_looks_like_bot

    monkeypatch.setattr(settings, "jira_trigger_user", "irem")
    monkeypatch.setattr(settings, "trigger_assignee_names", "irem")
    # Display name only — Cloud/on-prem often have no ASCII ``name`` field.
    assert assignee_looks_like_bot(
        {"displayName": "İrem"},
        needles=["irem"],
    )


def test_f8_poller_must_intake_todo_assigned_to_irem(poller, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "jira_trigger_user", "irem")
    monkeypatch.setattr(settings, "trigger_assignee_names", "irem")
    monkeypatch.setattr(settings, "jira_trigger_label", "")
    monkeypatch.setattr(settings, "trigger_labels", "")

    issue = {
        "key": "TR-1",
        "fields": {
            "summary": "Yeni özellik",
            "status": {"name": "Yapılacaklar", "statusCategory": {"key": "new"}},
            "labels": [],
            "assignee": {"displayName": "İrem"},
        },
    }
    poller.client.get_active_sprint = MagicMock(return_value=None)
    poller.client.sprint_lookup = "kanban"
    poller.client.get_board_issues = MagicMock(return_value=[issue])
    keys = [i["key"] for i in poller.poll_board()]
    assert "TR-1" in keys


# ---------------------------------------------------------------------------
# F9 — Re-adding plan_refactor reuses the previous @bot comment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f9_second_plan_refactor_reuses_latest_mention_intentionally(
    processor, fake_jira
):
    """INTENTIONAL: re-adding plan_refactor reuses the latest @bot mention."""
    from src.jira.plan_labels import PLAN_REFACTOR_LABEL
    from src.state.models import TaskStatus

    fake_jira.get_comments = MagicMock(
        return_value=[
            {
                "id": "10",
                "body": "[~devbot] add caching",
                "created": "2026-09-01T10:00:00.000+0000",
            }
        ]
    )
    fake_jira.get_myself = MagicMock(
        return_value={"name": "devbot", "displayName": "DevBot", "key": "devbot"}
    )
    starts = {"n": 0}

    async def fake_plan(state, refactor_comment=None):
        starts["n"] += 1
        starts["comment"] = refactor_comment

    processor._start_planning_workflow = fake_plan  # type: ignore[method-assign]
    st = processor.state_manager.create_state("RF-1", "plan login", "{params}")
    processor.state_manager.update_state("RF-1", status=TaskStatus.PLAN_READY)
    st = processor.state_manager.get_state("RF-1")

    event = {
        "plan_handoff": "refactor",
        "issue": {
            "key": "RF-1",
            "fields": {
                "summary": "plan login",
                "description": "{params}",
                "status": {
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                },
                "labels": [PLAN_REFACTOR_LABEL],
            },
        },
    }
    first = await processor._maybe_handle_plan_handoff(event, st)
    assert first == (True, None)
    assert starts["n"] == 1

    processor.state_manager.update_state("RF-1", status=TaskStatus.PLAN_READY)
    st2 = processor.state_manager.get_state("RF-1")
    second = await processor._maybe_handle_plan_handoff(event, st2)
    # INTENTIONAL: re-adding plan_refactor reuses the latest @bot mention.
    assert second == (True, None)
    assert starts["n"] == 2
