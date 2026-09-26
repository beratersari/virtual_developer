"""Critical failures found on origin/develop at b6a26b9.

Each test fails on that tree and passes once the matching path is fixed.
"""

import json
import subprocess
from unittest.mock import patch

from src.git_manager import GitCancelledError, GitManager


def test_cancel_during_submodule_set_url_scrubs_origin(tmp_path, monkeypatch):
    """set-url can finish, then cancel raises before the scrub flag is set.

    The kept clone must not keep ``oauth2:<PAT>`` in ``origin``.
    """
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
    (repo / ".gitmodules").write_text('[submodule "x"]\n', encoding="utf-8")
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="KAN-1")
    gm.temp_dir = repo
    gm.remote_url = clean
    gm._init_proc_state()
    monkeypatch.setattr(gm, "_pat_for_remote", lambda *_a, **_k: "super-secret-pat")
    real_tracked = gm._run_tracked

    def tracked(cmd, **kwargs):
        text = " ".join(str(part) for part in cmd)
        if "set-url" in text and "super-secret-pat" in text:
            subprocess.run(
                cmd,
                cwd=kwargs.get("cwd") or repo,
                check=True,
                capture_output=True,
            )
            raise GitCancelledError("git cancelled")
        return real_tracked(cmd, **kwargs)

    monkeypatch.setattr(gm, "_run_tracked", tracked)
    try:
        gm._update_submodules(reason="after clone")
    except GitCancelledError:
        pass
    shown = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "super-secret-pat" not in shown
    assert shown == clean

def test_gitlab_scheme_key_authenticates_client(monkeypatch):
    """A map key saved as ``https://host`` must still auth API calls.

    Settings re-save already accepts that key. ``GitlabClient`` looks up
    the bare host and, because the map is non-empty, skips leftover GITLAB_PAT.
    """
    from src.config import Settings
    from src.gitlab.client import GitlabClient

    s = Settings()
    s.gitlab_host_pats = json.dumps(
        {"https://gitlab.example.com": "glpat-secret"}
    )
    s.gitlab_pat = ""
    monkeypatch.setattr("src.gitlab.client.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    client = GitlabClient(host="gitlab.example.com")
    assert client.pat == "glpat-secret"

def test_azure_collection_case_keeps_pat(monkeypatch):
    """A case-only collection edit must not drop the PAT.

    The settings page omits previous_host when the lowercased URL is
    unchanged, and lookup must still find the saved spelling.
    """
    from src.azure.client import AzureDevOpsClient
    from src.config import Settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update

    s = Settings()
    s.azure_collection_pats = json.dumps(
        {"https://tfs.corp/tfs/DefaultCollection": "secret-pat"}
    )
    s.azure_collection_urls = "[]"
    s.azure_pat = ""
    monkeypatch.setattr("src.dashboard.service.settings", s)
    monkeypatch.setattr("src.config.settings", s)
    monkeypatch.setattr("src.azure.client.settings", s)
    monkeypatch.setattr(
        "src.dashboard.service.save_runtime_settings", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "src.dashboard.service.upsert_dotenv_keys", lambda *_a, **_k: None
    )
    assert (
        s.azure_pat_for_collection("https://tfs.corp/tfs/defaultcollection")
        == "secret-pat"
    )
    apply_settings_update(
        SettingsUpdate(
            azure_credentials=[
                {
                    "host": "https://tfs.corp/tfs/defaultcollection",
                    "pat": "",
                }
            ]
        )
    )
    assert (
        s.azure_pat_for_collection("https://tfs.corp/tfs/defaultcollection")
        == "secret-pat"
    )
    client = AzureDevOpsClient(
        collection_url="https://tfs.corp/tfs/defaultcollection"
    )
    assert client.pat == "secret-pat"
    assert client.api_base

def test_removed_sql_comment_stays_on_the_finding_file():
    """A deleted ``-- comment`` line is ``--- comment`` inside the hunk.

    Treating that as a diff header posts the review on the wrong file.
    """
    from src.review.azure_threads import azure_thread_context
    from src.review.diffmap import parse_unified_diff
    from src.review.findings import Finding

    diff = (
        "diff --git a/db/migrate.sql b/db/migrate.sql\n"
        "--- a/db/migrate.sql\n"
        "+++ b/db/migrate.sql\n"
        "@@ -1,3 +1,2 @@\n"
        " keep\n"
        "--- drop the old column\n"
        " still\n"
    )
    parsed = parse_unified_diff(diff)
    assert parsed.files[0].new_path == "db/migrate.sql"
    finding = Finding(
        path="db/migrate.sql",
        start_line=2,
        end_line=2,
        side="new",
        severity="major",
        title="column",
        body="why",
    )
    ctx = azure_thread_context(finding, parsed)
    assert ctx is not None
    assert ctx["filePath"] == "/db/migrate.sql"

def test_claude_cancel_kills_tool_child():
    """Cancel must kill the tool process Claude spawned, not only Claude."""
    import os
    import time

    from src.backends.claude import ClaudeBackend

    if os.name == "nt":
        return
    proc = subprocess.Popen(
        ["bash", "-c", "sleep 45 & wait"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    kids: list[str] = []
    try:
        time.sleep(0.25)
        kids = subprocess.check_output(
            ["pgrep", "-P", str(proc.pid)], text=True
        ).split()
        assert kids

        class _Proc:
            def __init__(self, inner: subprocess.Popen) -> None:
                self.pid = inner.pid
                self._inner = inner

            def kill(self) -> None:
                self._inner.kill()

        ClaudeBackend().cancel({"proc": _Proc(proc), "pid": proc.pid})
        time.sleep(0.25)
        for kid in kids:
            live = subprocess.run(["ps", "-p", kid], capture_output=True)
            assert live.returncode != 0
    finally:
        subprocess.run(["kill", "-9", str(proc.pid)], capture_output=True)
        for kid in kids:
            subprocess.run(["kill", "-9", kid], capture_output=True)

def test_cancel_does_not_terminalize_a_claim(tmp_path):
    from src.state.queue_store import WorkQueueStore

    q = WorkQueueStore(queue_dir=tmp_path / "queue")
    rec = q.enqueue(source="jira", issue_key="KAN-1", summary="s")
    claimed = q.claim_next(max_running=6)
    assert claimed and claimed["status"] == "running"
    assert q.cancel(rec["queue_id"]) is False
    assert q.get(rec["queue_id"])["status"] == "running"

def test_cancel_update_must_not_clobber_dispatching(tmp_path):
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2020-01-01T00:00:00",
        issue_key="KAN-1",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    assert store.claim_due(sid)["status"] == "dispatching"
    updated = store.update(sid, status="cancelled", error_message=None)
    assert updated["status"] == "dispatching"
    assert store.get(sid)["status"] == "dispatching"

def test_reopen_sees_skipped_row_beyond_500_older_files(tmp_path):
    from unittest.mock import MagicMock

    from src.scheduler.service import _reopen_skipped_dispatched_schedules
    from src.state.queue_store import WorkQueueStore
    from src.state.schedule_store import ScheduleStore

    qdir = tmp_path / "queue"
    q = WorkQueueStore(queue_dir=qdir)
    for i in range(500):
        (qdir / f"q_old{i:04d}.json").write_text(
            json.dumps(
                {
                    "queue_id": f"q_old{i:04d}",
                    "status": "completed",
                    "created_at": "2000-01-01T00:00:00.000",
                    "payload": {},
                }
            ),
            encoding="utf-8",
        )
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    rec = store.create(
        title="t",
        description="",
        repository_url="https://example.com/r.git",
        source_branch="a",
        target_branch="b",
        mode="build",
        scheduled_at="2020-01-01T00:00:00",
        issue_key="KAN-9",
        issue_description="x",
    )
    sid = rec["schedule_id"]
    store.update(sid, status="dispatching")
    store.update(sid, expected_status="dispatching", status="dispatched")
    q.enqueue(source="jira", issue_key="KAN-9", payload={"schedule_id": sid})
    new = next(p for p in qdir.glob("q_*.json") if not p.name.startswith("q_old"))
    data = json.loads(new.read_text(encoding="utf-8"))
    data["status"] = "skipped"
    data["error_message"] = "Reaped stale running queue row (issue not live)"
    data["created_at"] = "2026-09-26T00:00:00.000"
    new.write_text(json.dumps(data), encoding="utf-8")
    proc = MagicMock()
    proc.queue_store = q
    proc.state_manager.get_state.return_value = None
    proc.list_live_processing_keys.return_value = []
    proc.IN_FLIGHT_STATUSES = set()
    n = _reopen_skipped_dispatched_schedules(proc, store)
    assert n == 1
    assert store.get(sid)["status"] == "scheduled"

def test_queued_row_takes_later_plan_execute_payload(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import patch

    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=object()):
        proc = JobProcessor()
    proc.queue_store = __import__(
        "src.state.queue_store", fromlist=["WorkQueueStore"]
    ).WorkQueueStore(queue_dir=tmp_path / "queue")

    async def _noop():
        return None

    monkeypatch.setattr(proc, "dispatch_queue", _noop)
    old = {
        "issue": {
            "key": "KAN-1",
            "fields": {
                "summary": "t",
                "description": "Mode: plan",
                "status": {"name": "To Do"},
                "labels": ["plan_ready"],
            },
        }
    }
    proc.queue_store.enqueue(
        source="jira", issue_key="KAN-1", summary="t", payload=old
    )
    new = {
        "plan_handoff": "execute",
        "issue": {
            "key": "KAN-1",
            "fields": {
                "summary": "t",
                "description": "Mode: plan",
                "status": {"name": "In Progress"},
                "labels": ["plan_execute"],
            },
        },
    }
    result = asyncio.run(proc.enqueue_jira_event(new))
    row = proc.queue_store.get(result["queue_id"])
    assert row["payload"].get("plan_handoff") == "execute"
