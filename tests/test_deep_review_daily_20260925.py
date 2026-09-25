"""Proofs for the 2026-09-25 whole-tree daily-use review.

Each test asserts the safe outcome. A failure is the defect.
"""

from __future__ import annotations

import json
import pathlib
import threading
from pathlib import Path

import pytest


def _raise_oserror_on_first_index_glob(monkeypatch: pytest.MonkeyPatch) -> None:
    """First index backfill glob fails; a later JSON walk can still see files."""
    real = pathlib.Path.glob
    seen: set[str] = set()

    def wrapped(self: pathlib.Path, pattern: str):
        if pattern in {"job_*.json", "sched_*.json", "osb_*.json"} and pattern not in seen:
            seen.add(pattern)
            raise OSError("index glob denied")
        return real(self, pattern)

    monkeypatch.setattr(pathlib.Path, "glob", wrapped)


def test_job_list_survives_index_backfill_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A reconcile error must not hide job JSON behind an empty SQLite index."""
    from src.state.job_store import JobStore

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "job_keep.json").write_text(
        json.dumps(
            {
                "job_id": "job_keep",
                "issue_key": "KAN-1",
                "summary": "still on disk",
                "created_at": "2026-09-25T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    store = JobStore(jobs)

    def boom(_jobs_dir: Path) -> int:
        raise RuntimeError("sqlite locked")

    assert store._index is not None
    monkeypatch.setattr(store._index, "reconcile", boom)
    listed = store.list_jobs()
    assert [row["job_id"] for row in listed] == ["job_keep"]


def test_job_list_survives_index_glob_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.state.job_store import JobStore

    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "job_keep.json").write_text(
        json.dumps(
            {
                "job_id": "job_keep",
                "issue_key": "KAN-1",
                "summary": "still on disk",
                "created_at": "2026-09-25T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    _raise_oserror_on_first_index_glob(monkeypatch)
    store = JobStore(jobs)
    listed = store.list_jobs()
    assert [row["job_id"] for row in listed] == ["job_keep"]


def test_schedule_list_survives_index_backfill_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A due schedule file must stay visible when the schedule index backfill throws."""
    from src.state.schedule_store import ScheduleStore

    folder = tmp_path / "schedules"
    folder.mkdir()
    (folder / "sched_keep.json").write_text(
        json.dumps(
            {
                "schedule_id": "sched_keep",
                "title": "nightly",
                "status": "scheduled",
                "issue_key": "KAN-9",
                "scheduled_at": "2026-09-25T09:00:00",
                "created_at": "2026-09-25T08:00:00",
            }
        ),
        encoding="utf-8",
    )
    store = ScheduleStore(schedules_dir=folder)

    def boom(_folder: Path) -> int:
        raise RuntimeError("sqlite locked")

    assert store._index is not None
    monkeypatch.setattr(store._index, "reconcile", boom)
    listed = store.list_schedules(status="scheduled", limit=10)
    assert [row["schedule_id"] for row in listed] == ["sched_keep"]


def test_session_bind_survives_index_backfill_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Resume must still find the bind JSON when the session index backfill throws."""
    from src.state.session_bind_store import SessionBindStore

    folder = tmp_path / "binds"
    folder.mkdir()
    (folder / "osb_keep.json").write_text(
        json.dumps(
            {
                "bind_id": "osb_keep",
                "repository_url": "https://gitlab.example.com/acme/app.git",
                "repository_key": "gitlab.example.com/acme/app",
                "branch": "feature/KAN-3",
                "target_branch": "develop",
                "session_id": "ses_keep",
                "kind": "build",
                "issue_key": "KAN-3",
                "job_id": None,
                "working_directory": str(tmp_path / "clone"),
                "forgotten_session_ids": [],
                "created_at": "2026-09-25T00:00:00",
                "updated_at": "2026-09-25T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    store = SessionBindStore(binds_dir=folder)

    def boom(_folder: Path) -> int:
        raise RuntimeError("sqlite locked")

    assert store._index is not None
    monkeypatch.setattr(store._index, "reconcile", boom)
    found = store.find_by_issue_key("KAN-3")
    assert found is not None
    assert found["session_id"] == "ses_keep"


def test_queue_finish_does_not_overwrite_a_terminal_row(tmp_path: Path):
    """A second finish blocks until the first terminal write commits, then leaves it."""
    from src.state.queue_store import WorkQueueStore

    store = WorkQueueStore(tmp_path)
    rec = store.enqueue(source="jira", issue_key="KAN-1", summary="work")
    qid = rec["queue_id"]
    entered = threading.Event()
    release = threading.Event()
    original = store.update
    other: dict = {}

    def slow_update(queue_id: str, **fields: object):
        if fields.get("status") == "completed":
            entered.set()
            assert release.wait(5)
        return original(queue_id, **fields)

    def cancel() -> None:
        other["row"] = store.finish(qid, status="cancelled", error_message="stopped")

    store.update = slow_update  # type: ignore[method-assign]
    worker = threading.Thread(target=lambda: store.finish(qid, status="completed"))
    worker.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=cancel)
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive()
    release.set()
    worker.join(5)
    stopper.join(5)
    final = store.get(qid)
    assert final is not None
    assert final["status"] == "completed"
    assert not final.get("error_message")
    assert other["row"]["status"] == "completed"


def test_board_id_survives_settings_save_that_rewrites_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Board id is runtime-only. A same save that rewrites .env must not drop it on restart."""
    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".env").write_text(
        "JIRA_BOARD_ID=1\nJIRA_TRIGGER_USER=oldbot\nJIRA_HOST=https://jira.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIRA_BOARD_ID", "1")
    monkeypatch.setenv("JIRA_TRIGGER_USER", "oldbot")
    monkeypatch.setenv("JIRA_HOST", "https://jira.example.com")
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)

    from src.config import Settings, apply_runtime_settings_to
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(
        SettingsUpdate(jira_board_id="42", jira_trigger_user="newbot")
    )
    fresh = Settings(_env_file=work / ".env")
    apply_runtime_settings_to(fresh)
    assert fresh.jira_board_id == "42"
    assert "newbot" in (fresh.jira_trigger_user or "")


def test_later_token_save_does_not_restore_old_board_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".env").write_text(
        "JIRA_BOARD_ID=1\nJIRA_API_TOKEN=old-token\nJIRA_HOST=https://jira.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIRA_BOARD_ID", "1")
    monkeypatch.setenv("JIRA_API_TOKEN", "old-token")
    monkeypatch.setenv("JIRA_HOST", "https://jira.example.com")
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)

    from src.config import Settings, apply_runtime_settings_to
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(SettingsUpdate(jira_board_id="42"))
    apply_settings_update(SettingsUpdate(jira_api_token="new-token"))
    fresh = Settings(_env_file=work / ".env")
    apply_runtime_settings_to(fresh)
    assert fresh.jira_board_id == "42"


def test_reprocess_clears_stale_plan_execute_run(tmp_path, monkeypatch):
    """To Do rework after a failed implement must not keep the implement latch."""
    from unittest.mock import patch

    from src.processor import JobProcessor
    from src.state.models import TaskStatus

    with patch("src.processor.create_jira_client", return_value=None):
        proc = JobProcessor()
    proc.state_manager = __import__(
        "src.state.manager", fromlist=["JiraStateManager"]
    ).JiraStateManager(state_dir=tmp_path / "state")
    proc.state_manager.create_state("KAN-1", "plan login", "Mode: plan")
    proc.state_manager.update_state(
        "KAN-1",
        status=TaskStatus.ERROR,
        metadata={"workflow_type": "planning", "plan_execute_run": True},
    )
    proc._reset_for_reprocess("KAN-1")
    loaded = proc.state_manager.get_state("KAN-1")
    assert loaded is not None
    assert loaded.metadata.get("plan_execute_run") is not True


def test_plan_failure_does_not_restore_plan_execute_label(state_manager, reporter, fake_jira):
    """A later plan run that fails must not put plan_execute back on the ticket."""
    from unittest.mock import patch

    from src.processor import JobProcessor
    from src.state.models import TaskStatus

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    state_manager.create_state("KAN-1", "plan login", "Mode: plan")
    state_manager.update_state(
        "KAN-1",
        status=TaskStatus.PLANNING,
        metadata={"workflow_type": "planning", "plan_execute_run": True},
    )
    proc._fail_issue("KAN-1", "agent crashed")
    written = [
        label
        for row in fake_jira.updated
        for label in (row.get("labels") or [])
    ]
    assert "plan_execute" not in written


def test_gitlab_fallback_keys_keep_different_project_paths():
    from src.gitlab.keys import gitlab_issue_key, resolve_mr_issue_key

    assert gitlab_issue_key("acme/demo", 4) != gitlab_issue_key("acme-demo", 4)
    left = resolve_mr_issue_key(project_path="acme/demo", mr_iid=4, mr_title="notes")
    right = resolve_mr_issue_key(project_path="acme-demo", mr_iid=4, mr_title="notes")
    assert left != right


def test_gitlab_fallback_keys_include_host():
    from src.gitlab.keys import resolve_mr_issue_key

    public = resolve_mr_issue_key(
        project_path="acme/demo",
        mr_iid=4,
        mr_title="notes",
        repository_url="https://gitlab.com/acme/demo.git",
    )
    internal = resolve_mr_issue_key(
        project_path="acme/demo",
        mr_iid=4,
        mr_title="notes",
        repository_url="https://gitlab.internal/acme/demo.git",
    )
    assert public != internal


def test_cancel_scrubs_settings_pat_from_origin(tmp_path, monkeypatch):
    """Cancel after origin was pointed at the PAT URL must restore the clean remote."""
    import subprocess
    from unittest.mock import patch

    from src.git_manager import GitManager

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    clean = "https://gitlab.example.com/acme/demo.git"
    subprocess.run(
        ["git", "remote", "add", "origin", clean],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="KAN-1")
    gm.temp_dir = repo
    gm.remote_url = clean
    monkeypatch.setattr(gm, "_pat_for_remote", lambda *_a, **_k: "super-secret-pat")
    assert gm._apply_settings_pat_to_origin() is True
    gm.cancel_processes()
    gm._scrub_remote_credentials()
    shown = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "super-secret-pat" not in shown
    assert shown == clean


def test_azure_review_thread_posts_dotfile_path():
    from src.review.azure_threads import azure_thread_context
    from src.review.diffmap import parse_unified_diff
    from src.review.findings import Finding

    diff = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 3333333..4444444 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,2 @@
 name: ci
-on: push
+on: pull_request
"""
    parsed = parse_unified_diff(diff)
    finding = Finding(
        path=".github/workflows/ci.yml",
        start_line=2,
        end_line=2,
        side="new",
        severity="major",
        title="workflow",
        body="why",
    )
    ctx = azure_thread_context(finding, parsed)
    assert ctx is not None
    assert ctx["filePath"] == "/.github/workflows/ci.yml"


def test_azure_review_thread_keeps_dotfile_path():
    from src.review.azure_threads import azure_thread_context
    from src.review.diffmap import parse_unified_diff
    from src.review.findings import Finding

    diff = """diff --git a/github/workflows/ci.yml b/github/workflows/ci.yml
index 1111111..2222222 100644
--- a/github/workflows/ci.yml
+++ b/github/workflows/ci.yml
@@ -1,2 +1,2 @@
 name: other
-on: push
+on: pull_request
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 3333333..4444444 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1,2 +1,2 @@
 name: ci
-on: push
+on: pull_request
"""
    parsed = parse_unified_diff(diff)
    found = parsed.find(".github/workflows/ci.yml")
    assert found is not None
    assert found.new_path == ".github/workflows/ci.yml"
    finding = Finding(
        path=".github/workflows/ci.yml",
        start_line=2,
        end_line=2,
        side="new",
        severity="major",
        title="workflow",
        body="why",
    )
    ctx = azure_thread_context(finding, parsed)
    assert ctx is not None
    assert ctx["filePath"] == "/.github/workflows/ci.yml"
    assert ctx["rightFileStart"]["line"] == 2


def test_settings_draft_keeps_saved_project_source_branch():
    """Settings → Projects must round-trip source_branch from the server payload."""
    text = Path("web/src/pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    draft = text.split("project_repositories: (s.project_repositories", 1)[1]
    draft = draft.split("})),", 1)[0]
    assert "source_branch" in draft


def test_settings_save_does_not_blank_project_source_branch():
    """Saving a project label must not send source_branch: '' for every remote."""
    text = Path("web/src/pages/settings/SettingsPage.tsx").read_text(encoding="utf-8")
    save = text.split("body.project_repositories = draft.project_repositories", 1)[1]
    save = save.split(".filter", 1)[0]
    assert "source_branch: ''" not in save
    assert "p.source_branch" in save
