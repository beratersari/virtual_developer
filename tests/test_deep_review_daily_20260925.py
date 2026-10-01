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
    """First import glob fails. The next call can import and delete the file."""
    real = pathlib.Path.glob
    seen: set[str] = set()

    def wrapped(self: pathlib.Path, pattern: str):
        if pattern in {"job_*.json", "sched_*.json", "osb_*.json"} and pattern not in seen:
            seen.add(pattern)
            raise OSError("index glob denied")
        return real(self, pattern)

    monkeypatch.setattr(pathlib.Path, "glob", wrapped)


def test_job_list_retries_import_after_backfill_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed import leaves the file. The next list imports it."""
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
    assert store._index is not None
    real = store._index.reconcile
    calls = {"n": 0}

    def flaky(folder: Path) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sqlite locked")
        return real(folder)

    monkeypatch.setattr(store._index, "reconcile", flaky)
    assert store.list_jobs() == []
    assert (jobs / "job_keep.json").is_file()
    listed = store.list_jobs()
    assert [row["job_id"] for row in listed] == ["job_keep"]
    assert not (jobs / "job_keep.json").is_file()


def test_job_list_retries_import_after_glob_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    assert store.list_jobs() == []
    assert (jobs / "job_keep.json").is_file()
    listed = store.list_jobs()
    assert [row["job_id"] for row in listed] == ["job_keep"]
    assert not (jobs / "job_keep.json").is_file()


def test_schedule_list_retries_import_after_backfill_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed import leaves the file. The next list imports it."""
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
    assert store._index is not None
    real = store._index.reconcile
    calls = {"n": 0}

    def flaky(path: Path) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sqlite locked")
        return real(path)

    monkeypatch.setattr(store._index, "reconcile", flaky)
    assert store.list_schedules(status="scheduled", limit=10) == []
    assert (folder / "sched_keep.json").is_file()
    listed = store.list_schedules(status="scheduled", limit=10)
    assert [row["schedule_id"] for row in listed] == ["sched_keep"]
    assert not (folder / "sched_keep.json").is_file()


def test_session_bind_retries_import_after_backfill_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed import leaves the file. The next lookup imports it."""
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
    assert store._index is not None
    real = store._index.reconcile
    calls = {"n": 0}

    def flaky(path: Path) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sqlite locked")
        return real(path)

    monkeypatch.setattr(store._index, "reconcile", flaky)
    assert store.find_by_issue_key("KAN-3") is None
    assert (folder / "osb_keep.json").is_file()
    found = store.find_by_issue_key("KAN-3")
    assert found is not None
    assert found["session_id"] == "ses_keep"
    assert not (folder / "osb_keep.json").is_file()


def test_queue_finish_does_not_overwrite_a_terminal_row(tmp_path: Path):
    """A second finish blocks until the first terminal write commits, then leaves it."""
    from src.state.queue_store import WorkQueueStore

    store = WorkQueueStore(tmp_path)
    rec = store.enqueue(source="jira", issue_key="KAN-1", summary="work")
    qid = rec["queue_id"]
    entered = threading.Event()
    release = threading.Event()
    original = store._write
    other: dict = {}

    def slow_write(row: dict) -> None:
        if row.get("status") == "completed":
            entered.set()
            assert release.wait(5)
        original(row)

    def cancel() -> None:
        other["row"] = store.finish(qid, status="cancelled", error_message="stopped")

    store._write = slow_write  # type: ignore[method-assign]
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


def _reload_like_a_new_process(env_file: Path, board_in_dotenv: str):
    """Settings() reads the process env. A restart loads the .env value, not the last save."""
    import os

    from src.config import Settings, apply_runtime_settings_to

    os.environ["JIRA_BOARD_ID"] = board_in_dotenv
    fresh = Settings(_env_file=env_file)
    apply_runtime_settings_to(fresh)
    return fresh


def test_board_id_survives_settings_save_that_rewrites_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Board id is runtime-only. A same save that rewrites .env must not drop it on restart."""
    import time

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

    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(
        SettingsUpdate(jira_board_id="42", jira_trigger_user="newbot")
    )
    time.sleep(1.1)
    fresh = _reload_like_a_new_process(work / ".env", "1")
    assert fresh.jira_board_id == "42"
    assert "newbot" in (fresh.jira_trigger_user or "")


def test_later_token_save_does_not_restore_old_board_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import time

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

    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(SettingsUpdate(jira_board_id="42"))
    time.sleep(1.1)
    apply_settings_update(SettingsUpdate(jira_api_token="new-token"))
    fresh = _reload_like_a_new_process(work / ".env", "1")
    assert fresh.jira_board_id == "42"


def test_hand_edited_dotenv_board_id_wins_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A .env edit after the last dashboard write still wins on the next start."""
    import time

    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    env_file = work / ".env"
    env_file.write_text(
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

    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(SettingsUpdate(jira_board_id="42"))
    time.sleep(1.1)
    text = env_file.read_text(encoding="utf-8")
    text = text.replace("JIRA_BOARD_ID=1", "JIRA_BOARD_ID=7").replace(
        "JIRA_BOARD_ID=42", "JIRA_BOARD_ID=7"
    )
    env_file.write_text(text, encoding="utf-8")
    fresh = _reload_like_a_new_process(env_file, "7")
    assert fresh.jira_board_id == "7"


def test_stale_runtime_host_does_not_replace_newer_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Saving the board id must not bring back an older Jira host on restart."""
    import json
    import os
    import time

    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    env_file = work / ".env"
    env_file.write_text(
        "JIRA_HOST=https://good.example.com\nJIRA_BOARD_ID=1\n",
        encoding="utf-8",
    )
    now = time.time()
    os.utime(env_file, (now, now))
    now = env_file.stat().st_mtime
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        json.dumps(
            {
                "jira_host": "https://stale.example.com",
                "jira_board_id": "9",
                "_updated": {
                    "jira_host": now - 5000,
                    "jira_board_id": now - 4000,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)
    monkeypatch.setenv("JIRA_HOST", "https://good.example.com")
    monkeypatch.setenv("JIRA_BOARD_ID", "1")

    from src.config import Settings, apply_runtime_settings_to

    unmarked = Settings(_env_file=env_file)
    apply_runtime_settings_to(unmarked)
    assert unmarked.jira_host == "https://good.example.com"
    assert unmarked.jira_board_id == "1"

    protected = json.loads(runtime.read_text(encoding="utf-8"))
    protected["_dotenv_written_at"] = int(now * 1000)
    protected["_dotenv_written_keys"] = ["JIRA_BOARD_ID"]
    protected["_updated"]["jira_board_id"] = now - 10
    runtime.write_text(json.dumps(protected), encoding="utf-8")
    monkeypatch.setenv("JIRA_BOARD_ID", "1")
    fresh = Settings(_env_file=env_file)
    apply_runtime_settings_to(fresh)
    assert fresh.jira_host == "https://good.example.com"
    assert fresh.jira_board_id == "9"


def test_other_mirrored_settings_survive_a_later_dotenv_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A later token save must not restore older .env lines for other Settings fields."""
    import os
    import time

    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    env_file = work / ".env"
    env_file.write_text(
        "\n".join(
            [
                "JIRA_HOST=https://jira.example.com",
                "JIRA_BOARD_ID=1",
                "JIRA_API_TOKEN=old-token",
                "POLL_INTERVAL_SECONDS=30",
                "MAX_CONCURRENT_JOBS=6",
                "TEMP_CLONE_MAX_AGE_DAYS=7",
                "AGENT_TASK_TIMEOUT_SECONDS=900",
                "AGENT_TASK_MAX_RETRIES=3",
                "AGENT_TASK_MAX_INCOMPLETE_RETRIES=256",
                "DEFAULT_MODEL=opencode/hy3-free",
                "DEFAULT_REVIEW_MODEL=old-review",
                "AGENT_BACKEND=opencode",
                "JIRA_TRIGGER_USER=oldbot",
                "TRIGGER_ASSIGNEE_NAMES=Old Name",
                "TRIGGER_MENTIONS=@old",
                "JIRA_TRIGGER_LABEL=oldlabel",
                "TRIGGER_LABELS=oldlabel",
                "GITLAB_TRIGGER_USER=oldgit",
                "GITLAB_BOT_MENTIONS=@oldgit",
                "AZURE_TRIGGER_USER=oldaz",
                "AZURE_BOT_MENTIONS=@oldaz",
                "AZURE_WEBHOOK_ENABLED=false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for key, value in {
        "JIRA_HOST": "https://jira.example.com",
        "JIRA_BOARD_ID": "1",
        "JIRA_API_TOKEN": "old-token",
        "POLL_INTERVAL_SECONDS": "30",
        "MAX_CONCURRENT_JOBS": "6",
        "DEFAULT_MODEL": "opencode/hy3-free",
        "AGENT_BACKEND": "opencode",
        "JIRA_TRIGGER_USER": "oldbot",
        "TRIGGER_ASSIGNEE_NAMES": "Old Name",
    }.items():
        monkeypatch.setenv(key, value)
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)

    from src.config import Settings, apply_runtime_settings_to
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    apply_settings_update(
        SettingsUpdate(
            poll_interval_seconds=45,
            max_concurrent_jobs=4,
            temp_clone_max_age_days=3,
            agent_task_timeout_seconds=7200,
            agent_task_max_retries=5,
            agent_task_max_incomplete_retries=64,
            default_model="opencode/deepseek-v4-flash-free",
            default_review_model="review/model",
            agent_backend="codex",
            jira_trigger_user="devbot",
            jira_trigger_label="bot",
            gitlab_trigger_user="berat_ai",
            azure_trigger_user="azurebot",
            azure_webhook_enabled=True,
        )
    )
    time.sleep(1.1)
    apply_settings_update(SettingsUpdate(jira_api_token="new-token"))
    stale = {
        "JIRA_BOARD_ID": "1",
        "POLL_INTERVAL_SECONDS": "30",
        "MAX_CONCURRENT_JOBS": "6",
        "TEMP_CLONE_MAX_AGE_DAYS": "7",
        "AGENT_TASK_TIMEOUT_SECONDS": "900",
        "AGENT_TASK_MAX_RETRIES": "3",
        "AGENT_TASK_MAX_INCOMPLETE_RETRIES": "256",
        "DEFAULT_MODEL": "opencode/hy3-free",
        "DEFAULT_REVIEW_MODEL": "old-review",
        "AGENT_BACKEND": "opencode",
        "JIRA_TRIGGER_USER": "oldbot",
        "TRIGGER_ASSIGNEE_NAMES": "Old Name",
        "TRIGGER_MENTIONS": "@old",
        "JIRA_TRIGGER_LABEL": "oldlabel",
        "TRIGGER_LABELS": "oldlabel",
        "GITLAB_TRIGGER_USER": "oldgit",
        "GITLAB_BOT_MENTIONS": "@oldgit",
        "AZURE_TRIGGER_USER": "oldaz",
        "AZURE_BOT_MENTIONS": "@oldaz",
        "AZURE_WEBHOOK_ENABLED": "false",
        "JIRA_HOST": "https://jira.example.com",
    }
    for key, value in stale.items():
        os.environ[key] = value
    fresh = Settings(_env_file=env_file)
    apply_runtime_settings_to(fresh)
    assert fresh.poll_interval_seconds == 45
    assert fresh.max_concurrent_jobs == 4
    assert fresh.temp_clone_max_age_days == 3
    assert fresh.agent_task_timeout_seconds == 7200
    assert fresh.agent_task_max_retries == 5
    assert fresh.agent_task_max_incomplete_retries == 64
    assert fresh.default_model == "opencode/deepseek-v4-flash-free"
    assert fresh.default_review_model == "review/model"
    assert fresh.agent_backend == "codex"
    assert fresh.jira_trigger_user == "devbot"
    assert fresh.trigger_assignee_names == "devbot"
    assert fresh.trigger_mentions == "devbot"
    assert fresh.jira_trigger_label == "bot"
    assert fresh.trigger_labels == "bot"
    assert fresh.gitlab_trigger_user == "berat_ai"
    assert fresh.gitlab_bot_mentions == "berat_ai"
    assert fresh.azure_trigger_user == "azurebot"
    assert fresh.azure_bot_mentions == "azurebot"
    assert fresh.azure_webhook_enabled is True
    assert fresh.jira_host == "https://jira.example.com"
    text = env_file.read_text(encoding="utf-8")
    assert "POLL_INTERVAL_SECONDS=45" in text
    assert "MAX_CONCURRENT_JOBS=4" in text
    assert "TEMP_CLONE_MAX_AGE_DAYS=3" in text
    assert "DEFAULT_MODEL=opencode/deepseek-v4-flash-free" in text
    assert "AGENT_BACKEND=codex" in text
    assert "TRIGGER_ASSIGNEE_NAMES=devbot" in text
    assert "TRIGGER_MENTIONS=devbot" in text
    assert "GITLAB_BOT_MENTIONS=berat_ai" in text
    assert "AZURE_BOT_MENTIONS=azurebot" in text
    assert "AZURE_WEBHOOK_ENABLED=true" in text
    assert "JIRA_HOST=https://jira.example.com" in text


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
