"""Delete a clone (age purge or Storage HTTP), then HTTP / job on the same folder.

Covers: bind kept, reclone, same session, live protect, in-flight delete races.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.dashboard.api import create_dashboard_app
from src.dashboard.temp_storage import (
    TempStorageError,
    list_delete_jobs,
    queue_delete_temp_folder,
    reset_delete_jobs,
)
from src.git_manager import GitManager, purge_stale_temp_dirs
from src.orchestrator.agent_runner import AgentTask
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.session_bind_store import SessionBindStore
from src.temp_fs import force_rmtree
from src.gitlab.webhook import decide_gitlab_mr_webhook
from tests.test_azure_webhook import _pr_comment_payload
from tests.test_gitlab_webhook import _mr_lifecycle_payload, _mr_payload
from tests.test_opencode_sessions import _make_session_db

REPO = "https://gitlab.example.com/acme/app.git"
BRANCH = "feature/shared"
TARGET = "develop"
ISSUE = "KAN-12"


def _age(path: Path, days: float = 10) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _seed_clone(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    (path / "README").write_text("old tree", encoding="utf-8")


def _workspace_name(
    *,
    issue_key: str = ISSUE,
    repo: str = REPO,
    source: str = BRANCH,
    target: str = TARGET,
) -> str:
    gm = GitManager(
        issue_key=None,
        remote_url=repo,
        source_branch=source,
        target_branch=target,
        keep_source_work_branch=True,
    )
    gm.issue_key = issue_key
    gm.remote_name = gm._extract_remote_name(gm.remote_url or "")
    gm.work_branch = gm._resolve_work_branch_name(issue_key)
    return gm._workspace_folder_name()


def _bind(store: SessionBindStore, clone: Path, sid: str = "ses_keep") -> None:
    store.upsert(
        repository_url=REPO,
        branch=BRANCH,
        target_branch=TARGET,
        session_id=sid,
        issue_key=ISSUE,
        working_directory=str(clone),
        kind="build",
    )


def _wait_gone(path: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while path.exists() and time.time() < deadline:
        time.sleep(0.04)
    assert not path.exists(), f"still exists after {timeout}s: {path}"


def _wait_delete_status(name: str, want: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = list_delete_jobs().get(name) or {}
        if last.get("status") == want:
            return last
        time.sleep(0.04)
    raise AssertionError(f"delete {name!r} status={last.get('status')!r} want={want!r}")


def _install_fake_clone(monkeypatch) -> list:
    cloned: list = []

    def _clone(self):
        cloned.append(Path(self.temp_dir) if self.temp_dir else None)
        assert self.temp_dir is not None
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        (self.temp_dir / ".git").mkdir(exist_ok=True)
        (self.temp_dir / "README").write_text("new tree", encoding="utf-8")

    monkeypatch.setattr(GitManager, "_clone_into_temp", _clone)
    monkeypatch.setattr(GitManager, "_refresh_existing_clone", lambda self: None)
    monkeypatch.setattr(GitManager, "_existing_clone_usable", lambda self: False)
    monkeypatch.setattr(GitManager, "reclaim_workspace", lambda self, **k: 0)
    monkeypatch.setattr("src.git_manager.set_current_temp_dir", lambda *a, **k: None)
    return cloned


def _new_git_manager() -> GitManager:
    return GitManager(
        issue_key=ISSUE,
        remote_url=REPO,
        source_branch=BRANCH,
        target_branch=TARGET,
        keep_source_work_branch=True,
    )


def _gl_note_payload(note: str, *, note_id: int = 77) -> dict:
    payload = _mr_payload(
        note=note,
        source=BRANCH,
        target=TARGET,
        title="feat(KAN-12): shared work",
        note_id=note_id,
    )
    payload["project"]["http_url_to_repo"] = REPO
    payload["project"]["path_with_namespace"] = "acme/app"
    payload["project"]["web_url"] = "https://gitlab.example.com/acme/app"
    payload["repository"]["url"] = REPO
    payload["merge_request"]["web_url"] = (
        "https://gitlab.example.com/acme/app/-/merge_requests/4"
    )
    return payload


@contextmanager
def _blocked_rmtree():
    started = threading.Event()
    release = threading.Event()

    def _blocked(path, on_progress=None):  # noqa: ARG001
        started.set()
        if not release.wait(8):
            raise TimeoutError("delete not released")
        force_rmtree(path)
        if on_progress is not None:
            on_progress(1, 1)

    with patch(
        "src.dashboard.temp_storage.force_rmtree_progress", side_effect=_blocked
    ):
        yield started, release
    if not release.is_set():
        release.set()


@pytest.fixture(autouse=True)
def _isolate_storage_jobs():
    reset_delete_jobs()
    with GitManager._live_lock:
        GitManager._live_by_issue.clear()
    yield
    reset_delete_jobs()
    with GitManager._live_lock:
        GitManager._live_by_issue.clear()


@pytest.fixture
def clone_env(tmp_path, monkeypatch):
    from src.config import settings

    base = tmp_path / "t"
    name = _workspace_name()
    clone = base / name
    _seed_clone(clone)
    binds = SessionBindStore(binds_dir=tmp_path / "binds")
    monkeypatch.setattr("src.state.session_bind_store.session_bind_store", binds)
    monkeypatch.setattr(settings, "temp_dir_base", base)
    monkeypatch.setattr(settings, "temp_clone_max_age_days", 7.0)
    monkeypatch.chdir(tmp_path)
    return {"base": base, "clone": clone, "binds": binds, "name": name}


def _processor(fake_jira) -> JobProcessor:
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.enqueue_gitlab_note = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": ISSUE}
    )
    proc.enqueue_azure_comment = AsyncMock(
        return_value={"ok": True, "queued": True, "issue_key": ISSUE}
    )
    proc.handle_gitlab_mr_lifecycle = AsyncMock(
        return_value={"ok": True, "reason": "review"}
    )
    return proc


def _enable_webhooks(monkeypatch) -> None:
    monkeypatch.setattr("src.config.settings.gitlab_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.gitlab_webhook_secret", "tok")
    monkeypatch.setattr("src.config.settings.gitlab_bot_mentions", "@berat_ai")
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    monkeypatch.setattr("src.config.settings.azure_webhook_enabled", True)
    monkeypatch.setattr("src.config.settings.azure_webhook_secret", "")
    monkeypatch.setattr("src.config.settings.azure_bot_mentions", "@yaver")


def _post_gitlab_note(client: TestClient, note: str, *, note_id: int = 77):
    return client.post(
        "/yaver/webhook/gitlab",
        json=_gl_note_payload(note, note_id=note_id),
        headers={"X-Gitlab-Token": "tok", "X-Gitlab-Event": "Note Hook"},
    )


# --- age purge then resume / HTTP -------------------------------------------


def test_age_purge_keeps_session_then_setup_reclones(clone_env, monkeypatch):
    clone = clone_env["clone"]
    binds = clone_env["binds"]
    _bind(binds, clone)
    _age(clone)
    removed = purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    assert removed == 1
    assert not clone.exists()
    rec = binds.get(REPO, BRANCH, TARGET, kind="build")
    assert rec is not None
    assert rec.get("session_id") == "ses_keep"

    cloned = _install_fake_clone(monkeypatch)
    gm = _new_git_manager()
    try:
        assert len(cloned) == 1
        assert Path(cloned[0]).resolve() == clone.resolve()
        assert gm.temp_dir is not None
        assert gm.temp_dir.resolve() == clone.resolve()
        assert clone.exists()
        assert (clone / ".git").is_dir()
        assert binds.get(REPO, BRANCH, TARGET, kind="build").get("session_id") == "ses_keep"
    finally:
        gm.cleanup()


def test_attach_resumes_same_session_after_purge_and_reclone(
    clone_env, monkeypatch, tmp_path
):
    clone = clone_env["clone"]
    binds = clone_env["binds"]
    _bind(binds, clone, "ses_keep")
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    _seed_clone(clone)
    db = _make_session_db(
        tmp_path / "opencode.db",
        [{"id": "ses_keep", "directory": str(clone), "title": ISSUE}],
    )
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()
    proc.state_manager = sm
    git = MagicMock()
    git.remote_url = REPO
    git.work_branch = BRANCH
    git.target_branch = TARGET
    git.get_working_directory.return_value = clone
    sm.create_state(ISSUE, "s", "d")
    proc._contexts[ISSUE] = {"git": git, "runner": None}
    task = AgentTask(
        description="t", prompt="p", agent="derman-build", issue_key=ISSUE
    )
    with patch("src.opencode_sessions._default_db_path", return_value=db):
        sid = proc._attach_bound_opencode_session(ISSUE, task, git)
    assert sid == "ses_keep"
    assert task.session_id == "ses_keep"
    rec = binds.get(REPO, BRANCH, TARGET, kind="build")
    assert rec.get("session_id") == "ses_keep"


def test_purge_skips_live_clone_http_job_keeps_folder(clone_env):
    clone = clone_env["clone"]
    _age(clone, 10)
    removed = purge_stale_temp_dirs(
        max_age_days=7.0,
        base_dir=clone_env["base"],
        protect_paths={clone.resolve()},
    )
    assert removed == 0
    assert clone.exists()


def test_age_zero_never_deletes(clone_env):
    clone = clone_env["clone"]
    _age(clone, 20)
    removed = purge_stale_temp_dirs(max_age_days=0.0, base_dir=clone_env["base"])
    assert removed == 0
    assert clone.exists()


def test_fresh_clone_not_purged(clone_env):
    clone = clone_env["clone"]
    os.utime(clone, None)
    removed = purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    assert removed == 0
    assert clone.exists()


def test_http_gitlab_review_after_age_purge_enqueues(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    assert not clone.exists()

    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = _post_gitlab_note(client, "@berat_ai /review the diff")
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    proc.enqueue_gitlab_note.assert_called_once()
    rec = clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")
    assert rec.get("session_id") == "ses_keep"


def test_http_gitlab_yaver_after_age_purge_enqueues(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = _post_gitlab_note(client, "@berat_ai /yaver add tests", note_id=88)
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    proc.enqueue_gitlab_note.assert_called_once()


def test_http_gitlab_assign_after_purge_still_start_review(clone_env, monkeypatch):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    monkeypatch.setattr("src.config.settings.gitlab_trigger_user", "berat_ai")
    payload = _mr_lifecycle_payload(
        action="update", state="opened", source=BRANCH, target=TARGET
    )
    payload["changes"] = {
        "reviewers": {
            "previous": [],
            "current": [{"id": 9, "username": "berat_ai"}],
        }
    }

    with patch(
        "src.gitlab.client.GitlabClient.current_user",
        return_value={"id": 9, "username": "berat_ai", "name": "Bot"},
    ):
        d = decide_gitlab_mr_webhook(
            payload,
            headers={"X-Gitlab-Event": "Merge Request Hook", "X-Gitlab-Token": "s"},
            enabled=True,
            secret="s",
        )
    assert d.event.start_review is True
    rec = clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")
    assert rec.get("session_id") == "ses_keep"


def test_http_azure_comment_after_age_purge_enqueues(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = client.post(
            "/yaver/webhook/azure",
            json=_pr_comment_payload(note="@yaver /review the diff"),
        )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    proc.enqueue_azure_comment.assert_called_once()
    rec = clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")
    assert rec.get("session_id") == "ses_keep"


def test_http_storage_delete_after_age_purge_is_404(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    assert not clone.exists()
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = client.post("/api/storage/delete", json={"name": clone.name})
    assert resp.status_code == 404
    assert clone.name in (resp.json().get("detail") or "")


def test_age_purge_then_setup_reclones_then_http_review(
    clone_env, monkeypatch, fake_jira
):
    """7-day purge, next job reclones same hashed folder, HTTP /review still accepted."""
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _age(clone)
    purge_stale_temp_dirs(max_age_days=7.0, base_dir=clone_env["base"])
    cloned = _install_fake_clone(monkeypatch)
    gm = _new_git_manager()
    try:
        assert gm.temp_dir.resolve() == clone.resolve()
        assert cloned
        _enable_webhooks(monkeypatch)
        proc = _processor(fake_jira)
        with TestClient(create_dashboard_app(processor=proc)) as client:
            resp = _post_gitlab_note(client, "@berat_ai /review after reclone")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        assert clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")[
            "session_id"
        ] == "ses_keep"
    finally:
        gm.cleanup()


# --- start Storage delete, then HTTP for the same folder --------------------


def test_http_delete_then_gitlab_review_while_deleting(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            gone = client.post("/api/storage/delete", json={"name": clone.name})
            assert gone.status_code == 202, gone.text
            assert gone.json()["status"] == "deleting"
            assert started.wait(2)
            assert clone.exists()
            resp = _post_gitlab_note(client, "@berat_ai /review the diff")
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            proc.enqueue_gitlab_note.assert_called_once()
            progress = client.get("/api/storage/deletes")
            assert progress.status_code == 200
            jobs = progress.json()["deletes"]
            assert any(
                j["name"] == clone.name and j["status"] == "deleting" for j in jobs
            )
            release.set()
    _wait_gone(clone)
    assert clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")[
        "session_id"
    ] == "ses_keep"


def test_http_delete_then_gitlab_yaver_while_deleting(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            assert client.post(
                "/api/storage/delete", json={"name": clone.name}
            ).status_code == 202
            assert started.wait(2)
            resp = _post_gitlab_note(client, "@berat_ai /yaver add tests", note_id=91)
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            proc.enqueue_gitlab_note.assert_called_once()
            release.set()
    _wait_gone(clone)


def test_http_delete_then_azure_review_while_deleting(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            assert client.post(
                "/api/storage/delete", json={"name": clone.name}
            ).status_code == 202
            assert started.wait(2)
            resp = client.post(
                "/yaver/webhook/azure",
                json=_pr_comment_payload(note="@yaver /review look at login"),
            )
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            proc.enqueue_azure_comment.assert_called_once()
            release.set()
    _wait_gone(clone)


def test_http_delete_then_gitlab_assign_while_deleting(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    payload = _mr_lifecycle_payload(
        action="update", state="opened", source=BRANCH, target=TARGET
    )
    payload["changes"] = {
        "reviewers": {
            "previous": [],
            "current": [{"id": 9, "username": "berat_ai"}],
        }
    }
    payload["object_attributes"]["reviewers"] = [
        {"id": 9, "username": "berat_ai"}
    ]
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            assert client.post(
                "/api/storage/delete", json={"name": clone.name}
            ).status_code == 202
            assert started.wait(2)
            with patch(
                "src.gitlab.client.GitlabClient.current_user",
                return_value={"id": 9, "username": "berat_ai", "name": "Bot"},
            ):
                resp = client.post(
                    "/yaver/webhook/gitlab",
                    json=payload,
                    headers={
                        "X-Gitlab-Token": "tok",
                        "X-Gitlab-Event": "Merge Request Hook",
                    },
                )
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            proc.handle_gitlab_mr_lifecycle.assert_called_once()
            release.set()
    _wait_gone(clone)


def test_second_http_delete_while_first_runs_is_409(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            first = client.post("/api/storage/delete", json={"name": clone.name})
            assert first.status_code == 202
            assert started.wait(2)
            dup = client.post("/api/storage/delete", json={"name": clone.name})
            assert dup.status_code == 409
            assert "already in progress" in (dup.json().get("detail") or "").lower()
            listed = client.get("/api/storage").json()
            row = next(f for f in listed["folders"] if f["name"] == clone.name)
            assert row["delete"]["status"] == "deleting"
            release.set()
    _wait_gone(clone)


def test_second_queue_delete_while_first_runs_is_409(clone_env):
    clone = clone_env["clone"]
    with _blocked_rmtree() as (started, release):
        queue_delete_temp_folder(clone.name)
        assert started.wait(2)
        with pytest.raises(TempStorageError) as err:
            queue_delete_temp_folder(clone.name)
        assert err.value.status_code == 409
        release.set()
    _wait_gone(clone)


def test_http_delete_refuses_live_clone(clone_env, monkeypatch, fake_jira):
    clone = clone_env["clone"]
    monkeypatch.setattr(
        "src.dashboard.temp_storage._live_git_paths",
        lambda: {clone.resolve()},
    )
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = client.post("/api/storage/delete", json={"name": clone.name})
    assert resp.status_code == 409
    assert "stop the job" in (resp.json().get("detail") or "").lower()
    assert clone.exists()


def test_http_delete_missing_folder_is_404(clone_env, fake_jira):
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = client.post("/api/storage/delete", json={"name": "no_such_clone"})
    assert resp.status_code == 404


def test_http_delete_other_folder_does_not_touch_this_clone(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    other = clone_env["base"] / "other_deadbeef12"
    _seed_clone(other)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        resp = client.post("/api/storage/delete", json={"name": other.name})
        assert resp.status_code == 202
    _wait_gone(other)
    assert clone.exists()


def test_http_delete_error_then_retry_accepted(clone_env, monkeypatch, fake_jira):
    clone = clone_env["clone"]

    def _boom(path, on_progress=None):  # noqa: ARG001
        raise OSError("disk busy")

    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        with patch(
            "src.dashboard.temp_storage.force_rmtree_progress", side_effect=_boom
        ):
            first = client.post("/api/storage/delete", json={"name": clone.name})
            assert first.status_code == 202
            _wait_delete_status(clone.name, "error")
            assert clone.exists()
        second = client.post("/api/storage/delete", json={"name": clone.name})
        assert second.status_code == 202
    _wait_gone(clone)


def test_http_delete_completes_then_setup_reclones_same_folder(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    proc = _processor(fake_jira)
    with TestClient(create_dashboard_app(processor=proc)) as client:
        resp = client.post("/api/storage/delete", json={"name": clone.name})
        assert resp.status_code == 202
    _wait_gone(clone)
    assert clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")[
        "session_id"
    ] == "ses_keep"

    cloned = _install_fake_clone(monkeypatch)
    gm = _new_git_manager()
    try:
        assert gm.temp_dir.resolve() == clone.resolve()
        assert len(cloned) == 1
        assert Path(cloned[0]).resolve() == clone.resolve()
        assert clone.exists()
        assert (clone / ".git").is_dir()
    finally:
        gm.cleanup()


def test_http_delete_completes_then_http_review_then_setup_reclones(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with TestClient(app) as client:
        assert client.post(
            "/api/storage/delete", json={"name": clone.name}
        ).status_code == 202
        _wait_gone(clone)
        resp = _post_gitlab_note(client, "@berat_ai /review after delete")
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        proc.enqueue_gitlab_note.assert_called_once()

    cloned = _install_fake_clone(monkeypatch)
    gm = _new_git_manager()
    try:
        assert gm.temp_dir.resolve() == clone.resolve()
        assert cloned
        assert clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")[
            "session_id"
        ] == "ses_keep"
    finally:
        gm.cleanup()


def test_http_delete_in_flight_then_setup_clones_delete_still_wins(
    clone_env, monkeypatch
):
    """Delete started first; a job that clones during the wipe can lose the tree."""
    clone = clone_env["clone"]
    _bind(clone_env["binds"], clone)
    cloned = _install_fake_clone(monkeypatch)
    with _blocked_rmtree() as (started, release):
        accepted = queue_delete_temp_folder(clone.name)
        assert accepted["accepted"] is True
        assert started.wait(2)
        gm = _new_git_manager()
        try:
            assert cloned
            assert gm.temp_dir.resolve() == clone.resolve()
            release.set()
        finally:
            gm.cleanup()
    _wait_gone(clone)
    assert clone_env["binds"].get(REPO, BRANCH, TARGET, kind="build")[
        "session_id"
    ] == "ses_keep"
    cloned.clear()
    gm2 = _new_git_manager()
    try:
        assert cloned
        assert gm2.temp_dir.resolve() == clone.resolve()
        assert clone.exists()
    finally:
        gm2.cleanup()


def test_http_during_blocked_delete_still_accepts_webhook(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    started = threading.Event()
    release = threading.Event()

    def _blocked(path, on_progress=None):  # noqa: ARG001
        started.set()
        release.wait(5)
        force_rmtree(path)

    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with patch(
        "src.dashboard.temp_storage.force_rmtree_progress", side_effect=_blocked
    ):
        queue_delete_temp_folder(clone.name)
        assert started.wait(2)
        with TestClient(app) as client:
            resp = _post_gitlab_note(client, "@berat_ai /yaver add tests", note_id=101)
        assert resp.status_code == 200
        assert resp.json().get("ok") is True
        proc.enqueue_gitlab_note.assert_called_once()
        release.set()
    _wait_gone(clone)


def test_http_delete_then_ask_webhook_still_accepted(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            assert client.post(
                "/api/storage/delete", json={"name": clone.name}
            ).status_code == 202
            assert started.wait(2)
            resp = _post_gitlab_note(
                client, "@berat_ai /ask why is this lock needed?", note_id=110
            )
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            proc.enqueue_gitlab_note.assert_called_once()
            release.set()
    _wait_gone(clone)


def test_http_delete_then_unrelated_mr_webhook_does_not_409(
    clone_env, monkeypatch, fake_jira
):
    clone = clone_env["clone"]
    _enable_webhooks(monkeypatch)
    proc = _processor(fake_jira)
    app = create_dashboard_app(processor=proc)
    other = _gl_note_payload("@berat_ai /review other repo", note_id=120)
    other["project"]["http_url_to_repo"] = (
        "https://gitlab.example.com/acme/other.git"
    )
    other["repository"]["url"] = "https://gitlab.example.com/acme/other.git"
    with _blocked_rmtree() as (started, release):
        with TestClient(app) as client:
            assert client.post(
                "/api/storage/delete", json={"name": clone.name}
            ).status_code == 202
            assert started.wait(2)
            resp = client.post(
                "/yaver/webhook/gitlab",
                json=other,
                headers={"X-Gitlab-Token": "tok", "X-Gitlab-Event": "Note Hook"},
            )
            assert resp.status_code == 200
            assert resp.json().get("ok") is True
            release.set()
    _wait_gone(clone)
