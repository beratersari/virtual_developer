"""Full-app review: frequent operator-facing gaps.

Real FastAPI TestClient, real local HTTP servers, real git-free disk stores.
No unittest.mock / MagicMock / FakeJiraClient.

Each test asserts the *correct* operator-visible behaviour. A failure means
the production bug is still present.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.dashboard.api import create_dashboard_app
from src.dashboard.auth import COOKIE_NAME
from src.dashboard.service import apply_settings_update, build_jobs, build_settings_view
from src.dashboard.schemas import SettingsUpdate
from src.dashboard.temp_storage import reset_size_cache
from src.gitlab.client import GitlabClient
from src.gitlab.webhook import GitlabMrNoteEvent, decide_gitlab_note_webhook
from src.gitlab_connection import probe_gitlab_connection
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection((host, port), timeout=0.2)
            s.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"{host}:{port} did not accept connections")


def _serve(handler_cls) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    _wait_port(host, int(port))
    return httpd


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# 1) Dashboard login / stale cookie must not 500
# ---------------------------------------------------------------------------


def test_stale_or_wrong_length_credentials_do_not_500(monkeypatch):
    """Wrong-length password or a leftover cookie must stay 403/200, never 500.

    hmac.compare_digest raises ValueError on some Python versions when the
    two strings differ in length. The SPA login gate probes GET /api/meta;
    a 500 there shows a broken page instead of the sign-in form.
    """
    monkeypatch.setattr(settings, "dashboard_username", "ops")
    monkeypatch.setattr(settings, "dashboard_password", "s3cret")
    client = TestClient(create_dashboard_app())

    short = client.post("/api/login", json={"username": "ops", "password": "x"})
    assert short.status_code == 403, (
        f"Wrong-length password returned {short.status_code}: {short.text}. "
        "Sign-in must be 403 login_failed, not a 500 from compare_digest."
    )
    assert short.json().get("code") == "login_failed"

    client.cookies.set(COOKIE_NAME, "x.deadbeef")
    meta = client.get("/api/meta")
    assert meta.status_code == 200, (
        f"Stale short cookie made GET /api/meta return {meta.status_code}. "
        "The SPA login probe must stay 200 with authenticated=false."
    )
    body = meta.json()
    assert body.get("dashboard_auth") is True
    assert body.get("authenticated") is False

    locked = client.get("/api/settings")
    assert locked.status_code == 403
    assert locked.json().get("code") == "login_required"


# ---------------------------------------------------------------------------
# 2) Issue page must not list KAN-10 under KAN-1
# ---------------------------------------------------------------------------


def test_issue_page_jobs_are_exact_key_not_substring(tmp_path, isolate_jira_agent_artifacts):
    """GET /api/tasks/KAN-1 is the issue document, not a Jobs-page search.

    build_jobs(issue_key=...) uses a substring needle (key/title/body).
    Operators opening KAN-1 then see KAN-10 / KAN-11 runs mixed in.
    """
    jobs = isolate_jira_agent_artifacts["job_store"]
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("KAN-1", "first ticket")
    sm.create_state("KAN-10", "tenth ticket")
    jobs.create_job(issue_key="KAN-1", summary="KAN-1 work", status="completed")
    jobs.create_job(issue_key="KAN-10", summary="KAN-10 work", status="completed")

    # Same helper GET /api/tasks/{issue_key} uses (issue_key is a search needle).
    listed = build_jobs(
        issue_key="KAN-1",
        store=jobs,
        state_manager=sm,
        limit=100,
        exact_issue_key=True,
    )
    keys = [j.issue_key for j in listed.jobs]
    assert keys == ["KAN-1"], (
        f"Issue page for KAN-1 also listed {keys}. "
        "Task detail must filter by exact issue key, not substring search."
    )


# ---------------------------------------------------------------------------
# 3) Storage "In use" must mean a live job, not a leftover session bind
# ---------------------------------------------------------------------------


def test_finished_session_bind_does_not_mark_clone_in_use(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """After a job finishes, Storage Delete must stay available.

    GET /api/storage sets in_use from every session-bind working_directory.
    Binds persist for resume, so finished clones stay 'In use' forever and
    the SPA disables Delete.
    """
    clones = tmp_path / "clones"
    folder = clones / "repo_deadbeef"
    folder.mkdir(parents=True)
    (folder / "README").write_text("done\n", encoding="utf-8")
    monkeypatch.setattr(settings, "temp_dir_base", clones)
    reset_size_cache()

    binds = isolate_jira_agent_artifacts["session_bind_store"]
    rec = binds.upsert(
        repository_url="https://gitlab.example.com/acme/app.git",
        branch="feature/KAN-9",
        target_branch="develop",
        session_id="ses_finished",
        issue_key="KAN-9",
        working_directory=str(folder),
        kind="build",
    )
    assert rec is not None

    app = create_dashboard_app()
    client = TestClient(app)
    payload = client.get("/api/storage").json()
    rows = [f for f in payload.get("folders") or [] if f.get("name") == folder.name]
    assert rows, f"clone folder missing from storage: {payload}"
    assert rows[0].get("in_use") is False, (
        "Finished clone is marked in_use because a session bind still "
        "points at it. Operators cannot Delete from Storage after the job ends."
    )


# ---------------------------------------------------------------------------
# 4) JIRA_PROJECTS cannot be saved from Settings; MR titles then miss the key
# ---------------------------------------------------------------------------


def test_settings_cannot_save_jira_projects_so_webhook_drops_real_key(
    tmp_path, monkeypatch
):
    """Dashboard Settings shows jira_projects but PATCH cannot change it.

    Default is PROJ. An MR titled feat(KAN-12): … then becomes GL-… instead
    of KAN-12, so the follow-up never attaches to the Jira ticket.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JIRA_PROJECTS=PROJ\n", encoding="utf-8")
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    monkeypatch.setattr(settings, "jira_projects", "PROJ")
    monkeypatch.setattr(settings, "jira_host", "https://jira.example.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok")
    monkeypatch.setattr(settings, "jira_email", "")

    app = create_dashboard_app()
    client = TestClient(app)
    before = client.get("/api/settings").json()["jira_projects"]
    patched = client.patch("/api/settings", json={"jira_projects": "KAN"})
    assert patched.status_code == 200, patched.text
    after = client.get("/api/settings").json()["jira_projects"]
    assert after == "KAN", (
        f"PATCH jira_projects was ignored (before={before!r} after={after!r}). "
        "Operators have no Settings field for JIRA_PROJECTS; webhook intake "
        "keeps the default PROJ and misses real keys like KAN-12."
    )

    monkeypatch.setattr(settings, "jira_projects", after)
    decision = decide_gitlab_note_webhook(
        {
            "object_kind": "note",
            "object_attributes": {
                "id": 77,
                "note": "@berat_ai /yaver fix tests",
                "noteable_type": "MergeRequest",
                "discussion_id": "d1",
            },
            "user": {"username": "alice", "name": "Alice"},
            "project": {
                "id": 3,
                "path_with_namespace": "acme/app",
                "http_url_to_repo": "https://gitlab.example.com/acme/app.git",
                "web_url": "https://gitlab.example.com/acme/app",
            },
            "repository": {},
            "merge_request": {
                "iid": 4,
                "title": "feat(KAN-12): fix login",
                "description": "",
                "source_branch": "feature/KAN-12",
                "target_branch": "develop",
                "web_url": "https://gitlab.example.com/acme/app/-/merge_requests/4",
            },
        },
        headers={
            "X-Gitlab-Event": "Note Hook",
            "X-Gitlab-Token": "secret",
        },
        enabled=True,
        secret="secret",
        bot_mentions=["berat_ai"],
        bot_usernames=["berat_ai"],
    )
    assert decision.accepted, decision.reason
    assert decision.event is not None
    assert decision.event.issue_key == "KAN-12", (
        f"After saving JIRA_PROJECTS=KAN the webhook still bound "
        f"{decision.event.issue_key!r}. Operators expect feat(KAN-12) to "
        "attach to the Jira ticket, not a GL- fallback key."
    )


def test_default_jira_projects_proj_misses_kan_key_in_mr_title(monkeypatch):
    """Stock default JIRA_PROJECTS=PROJ does not extract KAN-12 from MR titles."""
    monkeypatch.setattr(settings, "jira_projects", "PROJ")
    decision = decide_gitlab_note_webhook(
        {
            "object_kind": "note",
            "object_attributes": {
                "id": 88,
                "note": "@berat_ai /yaver please continue",
                "noteable_type": "MergeRequest",
                "discussion_id": "d2",
            },
            "user": {"username": "alice", "name": "Alice"},
            "project": {
                "id": 3,
                "path_with_namespace": "acme/app",
                "http_url_to_repo": "https://gitlab.example.com/acme/app.git",
                "web_url": "https://gitlab.example.com/acme/app",
            },
            "repository": {},
            "merge_request": {
                "iid": 9,
                "title": "feat(KAN-12): fix login",
                "description": "",
                "source_branch": "feature/login",
                "target_branch": "develop",
                "web_url": "https://gitlab.example.com/acme/app/-/merge_requests/9",
            },
        },
        headers={
            "X-Gitlab-Event": "Note Hook",
            "X-Gitlab-Token": "secret",
        },
        enabled=True,
        secret="secret",
        bot_mentions=["berat_ai"],
        bot_usernames=["berat_ai"],
    )
    assert decision.accepted, decision.reason
    assert decision.event is not None
    assert decision.event.issue_key == "KAN-12", (
        f"Default JIRA_PROJECTS=PROJ bound {decision.event.issue_key!r} "
        "instead of KAN-12. The follow-up job never comments on the Jira ticket."
    )


# ---------------------------------------------------------------------------
# 5) Leftover GITLAB_PAT is invisible in Settings
# ---------------------------------------------------------------------------


def test_leftover_gitlab_pat_shows_configured_in_settings(monkeypatch):
    """A lone GITLAB_PAT still clones/pushes; Settings must not say 'no PAT'."""
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    monkeypatch.setattr(settings, "gitlab_pat", "glpat-leftover-token")

    view = build_settings_view()
    assert view.gitlab_pat_configured is True, (
        "Settings reports gitlab_pat_configured=false when only GITLAB_PAT "
        "is set. Clone/push still work; Test connection says no PAT stored."
    )
    assert view.gitlab_credentials, (
        "Settings shows zero host rows for a leftover GITLAB_PAT, so the "
        "operator thinks credentials are missing."
    )


# ---------------------------------------------------------------------------
# 6) Settings GitLab Test is HTTPS-only; live client uses HTTP on localhost
# ---------------------------------------------------------------------------


def test_gitlab_settings_test_reaches_http_localhost_like_live_client(monkeypatch):
    """Local GitLab / simulator is HTTP. Test connection must not require TLS."""
    seen = {"probe": False, "live": False}

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            if self.path.startswith("/api/v4/user"):
                seen["probe"] = True
                _json_response(self, 200, {"id": 1, "username": "bot"})
                return
            if self.path.startswith("/api/v4/projects"):
                seen["live"] = True
                _json_response(self, 200, {"id": 3, "path_with_namespace": "acme/app"})
                return
            self.send_error(404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            seen["live"] = True
            _json_response(self, 201, {"id": 1, "body": "ok"})

    httpd = _serve(_Gitlab)
    try:
        host, port = httpd.server_address
        target = f"127.0.0.1:{port}"
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_pat", "")

        live = GitlabClient(host=target, pat="glpat-local")
        posted = live.post_mr_note(project="acme/app", mr_iid=1, body="ping")
        assert posted is not None, "Live GitlabClient could not reach HTTP localhost"

        probe = probe_gitlab_connection(target, pat="glpat-local")
        assert probe.get("ok") is True, (
            f"Settings Test failed against HTTP localhost ({probe}). "
            "probe_gitlab_connection always uses https:// while GitlabClient "
            "uses http:// for 127.0.0.1 — Test is red while jobs work."
        )
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 7) Any Settings save on a non-Cloud host wipes JIRA_EMAIL
# ---------------------------------------------------------------------------


def test_saving_poll_interval_does_not_blank_jira_email(tmp_path, monkeypatch):
    """Cloud Basic email lives in .env. Saving an unrelated field must keep it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "src.config.runtime_settings_path",
        lambda: tmp_path / "runtime_settings.json",
    )
    env = tmp_path / ".env"
    env.write_text(
        "JIRA_HOST=https://jira.onprem.local\n"
        "JIRA_EMAIL=user@example.com\n"
        "JIRA_API_TOKEN=tok\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "jira_host", "https://jira.onprem.local")
    monkeypatch.setattr(settings, "jira_email", "user@example.com")
    monkeypatch.setattr(settings, "jira_api_token", "tok")
    monkeypatch.setattr(settings, "poll_interval_seconds", 30)

    apply_settings_update(SettingsUpdate(poll_interval_seconds=45))
    text = env.read_text(encoding="utf-8")
    assert "JIRA_EMAIL=user@example.com" in text, (
        f"Saving poll interval cleared JIRA_EMAIL in .env:\n{text}\n"
        "The next Cloud / mixed-host restart then 401s on Basic auth."
    )


# ---------------------------------------------------------------------------
# 8) State filenames collide when '-' is folded to '_'
# ---------------------------------------------------------------------------


def test_state_keys_that_differ_by_hyphen_vs_underscore_stay_separate(tmp_path):
    """GL-KAN-12 (GitLab fallback) and GL_KAN-12 (Jira project) must not share a file."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    gitlab = sm.create_state("GL-KAN-12", "gitlab fallback job")
    jira = sm.create_state("GL_KAN-12", "jira project GL_KAN")
    sm.update_state("GL_KAN-12", status=TaskStatus.COMPLETED)

    left = sm.get_state("GL-KAN-12")
    right = sm.get_state("GL_KAN-12")
    assert left is not None and right is not None
    assert left.issue_key == "GL-KAN-12"
    assert right.issue_key == "GL_KAN-12"
    assert left.status == TaskStatus.PENDING, (
        "GL-KAN-12 was overwritten when GL_KAN-12 completed "
        f"(status={left.status.value} summary={left.issue_summary!r}). "
        "State files fold '-' to '_' so the two keys share one JSON."
    )
    assert right.status == TaskStatus.COMPLETED
    assert left.issue_summary == "gitlab fallback job"
    assert right.issue_summary == "jira project GL_KAN"
    _ = gitlab, jira


# ---------------------------------------------------------------------------
# 9) Dropped-accept must not overwrite COMPLETED
# ---------------------------------------------------------------------------


def test_dropped_accept_does_not_clobber_completed(tmp_path, monkeypatch):
    """A late enqueue failure must not flip a finished ticket to ERROR."""
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.state_manager.create_state("KAN-1", "done")
    proc.state_manager.update_state("KAN-1", status=TaskStatus.COMPLETED)

    proc.record_dropped_accept("KAN-1", "done", reason="loop closed")
    st = proc.state_manager.get_state("KAN-1")
    assert st is not None
    assert st.status == TaskStatus.COMPLETED, (
        f"record_dropped_accept overwrote COMPLETED with {st.status.value}. "
        "The operator sees a finished ticket flip to ERROR."
    )


def test_create_state_does_not_return_phantom_pending_over_completed(tmp_path):
    """create_state on an existing COMPLETED file must not return a fake PENDING."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("FOO-1", "original")
    sm.update_state("FOO-1", status=TaskStatus.COMPLETED)

    returned = sm.create_state("FOO-1", "second create")
    disk = sm.get_state("FOO-1")
    assert disk is not None
    assert disk.status == TaskStatus.COMPLETED
    assert returned.status == TaskStatus.COMPLETED, (
        f"create_state returned {returned.status.value} while disk is "
        f"{disk.status.value}. Callers think they own a new PENDING job."
    )


# ---------------------------------------------------------------------------
# 10) Schedule intake window of 500 newest files hides older tickets
# ---------------------------------------------------------------------------


def test_pending_schedule_is_seen_behind_500_newer_rows(tmp_path, monkeypatch):
    """A future schedule must still suppress poller intake after 500 newer rows."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    waiting = store.create(
        title="wait",
        description="",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="WAIT-9",
        issue_description="",
    )
    wait_path = store.schedules_dir / f"{waiting['schedule_id']}.json"
    old = time.time() - 86_400
    wait_path.touch()
    # Ensure this file is older than the flood we write next.
    os.utime(wait_path, (old, old))

    for i in range(500):
        store.create(
            title=f"flood {i}",
            description="",
            repository_url="https://gitlab.example.com/acme/app.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
            scheduled_at=future,
            issue_key=f"FLOOD-{i}",
            issue_description="",
        )

    monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
    poller = JiraPoller(
        client=JiraClient(host="http://127.0.0.1:9", api_token="x"),
        interval_seconds=30,
        board_id="1",
        state_manager=JiraStateManager(state_dir=tmp_path / "state"),
    )
    poller.schedule_store = store
    assert poller._issue_has_pending_schedule("WAIT-9") is True, (
        "WAIT-9 has a future schedule but the poller only walks the 500 "
        "newest schedule files. The board poller will start the ticket now."
    )


def test_poller_skips_scheduled_ticket_hidden_by_500_newer_files(
    tmp_path, monkeypatch
):
    """End-to-end: board poll must not intake WAIT-9 while it is scheduled."""
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    future = (datetime.now() + timedelta(hours=6)).isoformat(timespec="seconds")
    waiting = store.create(
        title="wait",
        description="",
        repository_url="https://gitlab.example.com/acme/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        scheduled_at=future,
        issue_key="WAIT-9",
        issue_description="",
    )
    wait_path = store.schedules_dir / f"{waiting['schedule_id']}.json"
    os.utime(wait_path, (time.time() - 86_400, time.time() - 86_400))
    for i in range(500):
        store.create(
            title=f"flood {i}",
            description="",
            repository_url="https://gitlab.example.com/acme/app.git",
            source_branch="develop",
            target_branch="develop",
            mode="build",
            scheduled_at=future,
            issue_key=f"FLOOD-{i}",
            issue_description="",
        )

    class _Jira(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.endswith("/sprint"):
                self.send_response(400)
                body = b'{"errorMessages":["Board does not support sprints"]}'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if "/board/" in parsed.path and parsed.path.endswith("/issue"):
                _json_response(
                    self,
                    200,
                    {
                        "total": 1,
                        "issues": [
                            {
                                "key": "WAIT-9",
                                "fields": {
                                    "summary": "scheduled later",
                                    "labels": [],
                                    "assignee": {
                                        "displayName": "DevBot",
                                        "name": "devbot",
                                    },
                                    "status": {
                                        "name": "To Do",
                                        "statusCategory": {"key": "new"},
                                    },
                                },
                            }
                        ],
                    },
                )
                return
            self.send_error(404)

    httpd = _serve(_Jira)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "jira_trigger_user", "devbot")
        monkeypatch.setattr(settings, "jira_trigger_label", "")
        client = JiraClient(host=f"http://{host}:{port}", api_token="tok")
        poller = JiraPoller(
            client=client,
            interval_seconds=30,
            board_id="1",
            state_manager=JiraStateManager(state_dir=tmp_path / "state"),
        )
        poller.schedule_store = store
        intake = poller.poll_board()
        keys = [i.get("key") for i in intake]
        assert "WAIT-9" not in keys, (
            f"Poller intake included scheduled WAIT-9 ({keys}). "
            "The 500-row schedule scan missed the older WAIT-9 file."
        )
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# 11) @bot /yaver on an MR bound to plan_ready must not be silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_yaver_on_plan_ready_posts_a_wait_note(
    tmp_path, monkeypatch
):
    """Commenting /yaver on the plan MR must leave a visible reply.

    The Jira ticket correctly waits for plan_execute. The MR thread must
    not stay silent with queue status skipped.
    """
    notes: list[str] = []

    class _Gitlab(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {}
            notes.append(str(body.get("body") or ""))
            _json_response(self, 201, {"id": 1, "body": body.get("body")})

        def do_GET(self) -> None:
            self.send_error(404)

    httpd = _serve(_Gitlab)
    try:
        host, port = httpd.server_address
        monkeypatch.setattr(settings, "jira_host", "")
        monkeypatch.setattr(settings, "jira_api_token", "")
        monkeypatch.setattr(settings, "gitlab_host_pats", "")
        monkeypatch.setattr(settings, "gitlab_pat", "glpat-test")
        monkeypatch.setattr(settings, "max_concurrent_jobs", 1)

        proc = JobProcessor()
        proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
        proc.state_manager.create_state("KAN-12", "plan the login")
        proc.state_manager.update_state("KAN-12", status=TaskStatus.PLAN_READY)

        event = GitlabMrNoteEvent(
            issue_key="KAN-12",
            note_id="901",
            note_body="@berat_ai /yaver implement the plan",
            prompt="implement the plan",
            author_username="alice",
            author_name="Alice",
            project_id=3,
            project_path="acme/app",
            repository_url=f"http://{host}:{port}/acme/app.git",
            host=f"{host}:{port}",
            mr_iid=4,
            mr_title="feat(KAN-12): plan login",
            mr_description="",
            source_branch="feature/KAN-12",
            target_branch="develop",
            mr_url=f"http://{host}:{port}/acme/app/-/merge_requests/4",
            discussion_id="disc-1",
        )
        result = await proc.enqueue_gitlab_note(event)
        deadline = time.time() + 3.0
        rec = proc.queue_store.get(result.get("queue_id") or "") or {}
        while time.time() < deadline:
            rec = proc.queue_store.get(result.get("queue_id") or "") or rec
            if rec.get("status") in {"skipped", "error", "cancelled", "done"}:
                break
            if notes:
                break
            await asyncio.sleep(0.05)

        assert notes, (
            f"GitLab /yaver on plan_ready posted nothing "
            f"(queue={rec.get('status')!r} reason={rec.get('error_message')!r}). "
            "The MR thread stays silent; operators think the webhook is dead."
        )
    finally:
        httpd.shutdown()
        try:
            proc.shutdown_processing(reason="test teardown")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 12) Missing {params} must comment on Jira (frequent first-run failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_params_posts_jira_error_via_real_http(tmp_path, monkeypatch):
    """First-time tickets without a {params} block must get a Jira comment."""
    comments: list[str] = []
    transitions: list[str] = []

    class _Jira(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path.endswith("/myself"):
                _json_response(self, 200, {"name": "devbot", "displayName": "DevBot"})
                return
            if "/transitions" in path:
                _json_response(
                    self,
                    200,
                    {
                        "transitions": [
                            {"id": "21", "name": "In Progress", "to": {"name": "In Progress"}}
                        ]
                    },
                )
                return
            if "/issue/" in path:
                key = path.rsplit("/", 1)[-1]
                _json_response(
                    self,
                    200,
                    {
                        "key": key,
                        "fields": {
                            "summary": "do the thing",
                            "description": "no template here",
                            "assignee": {"name": "devbot", "displayName": "DevBot"},
                            "status": {"name": "To Do"},
                            "labels": [],
                        },
                    },
                )
                return
            self.send_error(404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                payload = {}
            if path.endswith("/comment"):
                body = payload.get("body")
                if isinstance(body, dict):
                    comments.append(json.dumps(body))
                else:
                    comments.append(str(body or ""))
                _json_response(self, 201, {"id": "c1", "body": body})
                return
            if path.endswith("/transitions"):
                transitions.append(str(payload))
                self.send_response(204)
                self.end_headers()
                return
            self.send_error(404)

        def do_PUT(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            if length:
                self.rfile.read(length)
            self.send_response(204)
            self.end_headers()

    httpd = _serve(_Jira)
    try:
        host, port = httpd.server_address
        base = f"http://{host}:{port}"
        monkeypatch.setattr(settings, "jira_host", base)
        monkeypatch.setattr(settings, "jira_api_token", "tok")
        monkeypatch.setattr(settings, "jira_email", "")
        monkeypatch.setattr(settings, "max_concurrent_jobs", 1)

        proc = JobProcessor()
        proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
        proc.jira_client = JiraClient(host=base, api_token="tok")
        proc.reporter.client = proc.jira_client

        event = {
            "webhookEvent": "jira:issue_created",
            "issue": {
                "key": "KAN-42",
                "fields": {
                    "summary": "do the thing",
                    "description": "please implement this",
                    "assignee": {"displayName": "DevBot"},
                },
            },
        }
        await proc.process_event(event)
        deadline = time.time() + 4.0
        while time.time() < deadline and not comments:
            await asyncio.sleep(0.05)

        st = proc.state_manager.get_state("KAN-42")
        assert st is not None
        assert st.status == TaskStatus.ERROR, st.status.value
        joined = "\n".join(comments)
        assert "{params}" in joined, (
            f"Missing-template ticket left no {{params}} help on Jira "
            f"(comments={comments!r}). Operators only see In Progress + ERROR."
        )
    finally:
        httpd.shutdown()
        try:
            proc.shutdown_processing(reason="test teardown")
        except Exception:
            pass
