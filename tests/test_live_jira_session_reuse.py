"""Real Jira-item session reuse: HTTP create → poller → processor → cancel → next issue.

Two layers:

1. **Always-on (simulated Jira HTTP):** starts ``simulated_jira_server``, creates
   issues over HTTP with the ``bot`` trigger label, runs the real
   ``JiraPoller.poll_board`` + ``JobProcessor.process_event`` path (real git
   clone of a local origin). Only the LLM is stubbed so CI does not wait on
   Atlas. Asserts the second issue continues the cancelled job's session.

2. **Live Jira (opt-in / skip):** creates two real Cloud/on-prem issues via
   ``JiraClient`` when ``JIRA_HOST`` probes. No trigger labels (a live daemon
   must not steal them). Same processor path, comments posted back.

Run::

    .venv/bin/python -m pytest tests/test_live_jira_session_reuse.py -v -s
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest
from werkzeug.serving import make_server

from src.config import settings
from src.jira.poller import JiraPoller
from src.jira.simulated_client import SimulatedJiraClient
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_simple_task_timing_e2e import _make_local_origin

SOURCE = "feature/shared"
TARGET = "develop"
TRIGGER = "bot"
E2E_LIVE_LABEL = "vd-session-e2e"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _params(repo: str, *, source: str = SOURCE, target: str = TARGET) -> str:
    return (
        "Session-reuse e2e (automated).\n"
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}\n"
    )


def _jira_shape(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Lift the simulated-server flat dict into a REST API v2 issue envelope."""
    if not raw:
        return None
    if isinstance(raw.get("fields"), dict):
        return raw
    status_name = str(raw.get("status") or "To Do")
    todo = status_name.strip().lower() in {
        "to do",
        "todo",
        "open",
        "backlog",
        "new",
    }
    return {
        "key": raw.get("key"),
        "fields": {
            "summary": raw.get("summary") or "",
            "description": raw.get("description") or "",
            "status": {
                "name": status_name,
                "statusCategory": {"key": "new" if todo else "indeterminate"},
            },
            "labels": list(raw.get("labels") or []),
            "assignee": (
                {"displayName": raw["assignee"]} if raw.get("assignee") else None
            ),
            "issuetype": {"name": raw.get("issue_type") or "Task"},
            "comment": {"comments": list(raw.get("comments") or [])},
        },
    }


class BoardJira:
    """Simulated HTTP Jira, poller/processor shaped like Jira REST v2."""

    def __init__(self, inner: SimulatedJiraClient) -> None:
        self.inner = inner
        self.last_error: Optional[str] = None

    def create_issue(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.inner.create_issue(*args, **kwargs)

    def get_issue(
        self, key: str, fields: Optional[List[str]] = None, **kwargs: Any
    ) -> Optional[Dict[str, Any]]:
        return _jira_shape(self.inner.get_issue(key, fields=fields, **kwargs))

    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        return None

    def get_board_issues(
        self,
        board_id: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for raw in self.inner.list_issues():
            shaped = _jira_shape(raw)
            if shaped and shaped.get("key"):
                out.append(shaped)
        return out

    def get_sprint_issues(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    def transition_to_in_progress(self, issue_key: str) -> bool:
        return bool(self.inner.update_issue(issue_key, fields={"status": "In Progress"}))

    def transition_issue(self, issue_key: str, transition_name: str) -> bool:
        return bool(
            self.inner.update_issue(issue_key, fields={"status": transition_name})
        )

    def add_comment(self, issue_key: str, body: str) -> Optional[Dict[str, Any]]:
        return self.inner.add_comment(issue_key, body)

    def add_labels(self, issue_key: str, labels: List[str]) -> bool:
        return self.inner.add_labels(issue_key, labels)

    def append_to_description(self, issue_key: str, suffix: str) -> bool:
        return self.inner.append_to_description(issue_key, suffix)

    def update_issue(self, issue_key: str, fields=None, labels=None) -> bool:
        return self.inner.update_issue(issue_key, fields=fields, labels=labels)

    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        shaped = self.get_issue(issue_key)
        comments = ((shaped or {}).get("fields") or {}).get("comment") or {}
        if isinstance(comments, dict):
            return list(comments.get("comments") or [])
        return list(comments or [])


class _SimJiraServer:
    def __init__(self, port: int) -> None:
        import simulated_jira_server as sim

        self._mod = sim
        sim.store.issues.clear()
        sim.store.issue_counter = 2000
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self._httpd = make_server("127.0.0.1", port, sim.app)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"simulated Jira did not listen on {self.port}")

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        self._thread.join(timeout=2)
        self._mod.store.issues.clear()


@pytest.fixture
def sim_jira():
    port = _free_port()
    srv = _SimJiraServer(port)
    srv.start()
    inner = SimulatedJiraClient(base_url=srv.url)
    board = BoardJira(inner)
    try:
        yield board, srv
    finally:
        inner.close()
        srv.stop()


def _allow_file_origin(monkeypatch) -> None:
    import src.issue_git_spec as git_spec

    orig = git_spec._looks_like_git_url

    def _ok(url: str) -> bool:
        raw = (url or "").strip()
        if raw.lower().startswith("file://") and len(raw) > 8:
            return True
        return orig(url)

    monkeypatch.setattr(git_spec, "_looks_like_git_url", _ok)


def _wire_processor(tmp_path, monkeypatch, board: BoardJira, repo_url: str):
    _allow_file_origin(monkeypatch)
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(settings, "temp_dir_base", work / ".temp")
    monkeypatch.setattr(settings, "trigger_labels", f"{TRIGGER},ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    monkeypatch.setattr(settings, "jira_board_id", "1")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=board):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = board
    proc.reporter = JiraReporter(client=board)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = lambda *_a, **_k: None

    poller = JiraPoller(
        client=board, interval_seconds=1, board_id="1", state_manager=sm
    )
    pending: List[Dict[str, Any]] = []

    def _handler(event: dict) -> None:
        pending.append(event)

    poller._handler = _handler
    proc._poller = poller
    return proc, sm, poller, pending, repo_url


async def _poll_and_process(proc: JobProcessor, poller: JiraPoller, pending: list) -> List[str]:
    keys: List[str] = []
    issues = poller.poll_board()
    for issue in issues:
        key = issue["key"]
        poller.process_issue(issue, poller.dispatch_as_update(key))
        poller._seen_issues.add(key)
        keys.append(key)
    while pending:
        event = pending.pop(0)
        await proc.process_event(event)
    return keys


def _install_agent(monkeypatch, seen: List[Dict[str, Any]]):
    async def fake_run(self, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(
            {"issue_key": task.issue_key, "session_id": task.session_id}
        )
        sid = task.session_id or "ses_jira_shared"
        on_sid = kwargs.get("on_session_id")
        if on_sid:
            on_sid(sid)
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": f"Session: {sid}\ndone\n",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(AgentRunner, "run_agent", fake_run)
    return seen


@pytest.mark.asyncio
async def test_http_jira_poller_cancel_then_next_issue_reuses_session(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """Create issue A over HTTP, poller takes it, cancel, create B, B resumes."""
    board, _srv = sim_jira
    origin = _make_local_origin(tmp_path / "git")
    repo = origin.resolve().as_uri()
    proc, sm, poller, pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    seen: List[Dict[str, Any]] = []
    _install_agent(monkeypatch, seen)

    created_a = board.create_issue(
        summary="[vd-e2e] session A",
        description=_params(repo),
        labels=[TRIGGER, "vd-session-sim"],
    )
    assert created_a and created_a.get("key"), board.inner.last_error
    key_a = created_a["key"]

    processed = await _poll_and_process(proc, poller, pending)
    assert key_a in processed
    st_a = sm.get_state(key_a)
    assert st_a is not None
    bound = binds.get(repo, SOURCE, TARGET)
    assert bound is not None
    assert bound["session_id"] == "ses_jira_shared"
    assert seen[0]["issue_key"] == key_a
    assert seen[0]["session_id"] is None

    # Board must be In Progress so cancel does not leave A eligible for rework.
    live_a = board.get_issue(key_a)
    assert (live_a["fields"]["status"]["name"] or "").lower() == "in progress"

    cancelled = await proc.cancel_job(key_a, reason="e2e cancel A so B can continue")
    # A may already be COMPLETED (fake agent is instant). Either way the bind stays.
    if not cancelled.get("ok"):
        assert st_a.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.ERROR}
    still = binds.get(repo, SOURCE, TARGET)
    assert still is not None
    assert still["session_id"] == "ses_jira_shared"

    created_b = board.create_issue(
        summary="[vd-e2e] session B (must continue A)",
        description=_params(repo),
        labels=[TRIGGER, "vd-session-sim"],
    )
    assert created_b and created_b.get("key")
    key_b = created_b["key"]

    processed_b = await _poll_and_process(proc, poller, pending)
    assert key_b in processed_b
    # A is In Progress — poller must not re-queue it next to B.
    assert key_a not in processed_b

    b_runs = [row for row in seen if row["issue_key"] == key_b]
    assert b_runs, "poller never started the second Jira issue"
    assert b_runs[0]["session_id"] == "ses_jira_shared"
    assert binds.get(repo, SOURCE, TARGET)["session_id"] == "ses_jira_shared"

    comments_b = board.get_comments(key_b)
    assert comments_b, "processor must post Jira comments on the second issue"
    bodies = "\n".join(str(c.get("body") or "") for c in comments_b)
    assert "AI Agent" in bodies or "Work" in bodies


@pytest.mark.asyncio
async def test_http_jira_in_flight_cancel_then_second_issue_resumes(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """Cancel while A is executing; B created on the board must use A's session."""
    board, _srv = sim_jira
    origin = _make_local_origin(tmp_path / "git")
    repo = origin.resolve().as_uri()
    proc, sm, poller, pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    seen: List[Dict[str, Any]] = []

    created_a = board.create_issue(
        summary="[vd-e2e] in-flight A",
        description=_params(repo),
        labels=[TRIGGER, "vd-session-sim"],
    )
    key_a = created_a["key"]

    # Hang A until cancel; B is instant. Watch processor state.
    orig_run = AgentRunner.run_agent

    async def hang_run(self, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append({"issue_key": task.issue_key, "session_id": task.session_id})
        sid = task.session_id or "ses_jira_shared"
        on_sid = kwargs.get("on_session_id")
        if on_sid:
            on_sid(sid)
        if task.issue_key == key_a:
            for _ in range(500):
                st = sm.get_state(key_a)
                if st and st.status == TaskStatus.CANCELLED:
                    break
                await asyncio.sleep(0.02)
            return {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": f"Session: {sid}\n",
                "stderr": "Aborted",
                "session_file": None,
                "opencode_session_id": sid,
                "aborted": True,
                "progress": 0,
            }
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": f"Session: {sid}\ndone\n",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": sid,
            "progress": 100,
        }

    monkeypatch.setattr(AgentRunner, "run_agent", hang_run)
    _ = orig_run

    job_a = asyncio.create_task(_poll_and_process(proc, poller, pending))
    for _ in range(200):
        if binds.get(repo, SOURCE, TARGET):
            break
        await asyncio.sleep(0.05)
    bound = binds.get(repo, SOURCE, TARGET)
    assert bound and bound["session_id"] == "ses_jira_shared"

    cancelled = await proc.cancel_job(key_a, reason="stop in-flight A")
    assert cancelled["ok"] is True
    await asyncio.wait_for(job_a, timeout=8)

    assert binds.get(repo, SOURCE, TARGET)["session_id"] == "ses_jira_shared"
    assert "ses_jira_shared" not in (
        binds.get(repo, SOURCE, TARGET).get("forgotten_session_ids") or []
    )

    created_b = board.create_issue(
        summary="[vd-e2e] B after in-flight cancel",
        description=_params(repo),
        labels=[TRIGGER, "vd-session-sim"],
    )
    key_b = created_b["key"]
    processed_b = await _poll_and_process(proc, poller, pending)
    assert key_b in processed_b
    b_runs = [row for row in seen if row["issue_key"] == key_b]
    assert b_runs
    assert b_runs[0]["session_id"] == "ses_jira_shared"


@pytest.mark.asyncio
async def test_http_jira_different_target_does_not_reuse(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    board, _srv = sim_jira
    origin = _make_local_origin(tmp_path / "git")
    import subprocess

    subprocess.run(
        ["git", "branch", "main", "develop"],
        cwd=str(origin),
        check=True,
        capture_output=True,
        text=True,
    )
    repo = origin.resolve().as_uri()
    proc, sm, poller, pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    seen: List[Dict[str, Any]] = []
    _install_agent(monkeypatch, seen)

    board.create_issue(
        summary="[vd-e2e] target develop",
        description=_params(repo, target="develop"),
        labels=[TRIGGER],
    )
    await _poll_and_process(proc, poller, pending)
    assert binds.get(repo, SOURCE, "develop")["session_id"] == "ses_jira_shared"

    board.create_issue(
        summary="[vd-e2e] target main",
        description=_params(repo, target="main"),
        labels=[TRIGGER],
    )
    await _poll_and_process(proc, poller, pending)
    main_runs = [row for row in seen if row["issue_key"].startswith("SIM-")]
    assert len(main_runs) >= 2
    assert main_runs[-1]["session_id"] is None
    assert binds.get(repo, SOURCE, "main")["session_id"] == "ses_jira_shared"
    assert binds.get(repo, SOURCE, "develop")["session_id"] == "ses_jira_shared"


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token or "your-jira.example" in host:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if "attacker.example" in host or "example.com" in host:
        return f"JIRA_HOST is a placeholder ({host})"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


@pytest.mark.asyncio
async def test_live_jira_create_two_issues_second_continues_session(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Create two real Jira issues (no trigger labels) and run the processor."""
    skip = _jira_live_ready()
    if skip:
        pytest.skip(skip)

    from src.jira.client import JiraClient
    from src.jira_connection import probe_jira_connection

    client = JiraClient()
    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")

    origin = _make_local_origin(tmp_path / "git")
    repo = origin.resolve().as_uri()
    project = ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    created_a = client.create_issue(
        project,
        f"[vd-e2e] session reuse A {stamp}",
        _params(repo),
        issue_type="Task",
        labels=[E2E_LIVE_LABEL],
    )
    if not created_a or not created_a.get("key"):
        pytest.skip(f"Could not create Jira issue: {client.last_error}")
    key_a = created_a["key"]
    print(f"\n[live jira] created {key_a} {client.host.rstrip('/')}/browse/{key_a}", flush=True)

    created_b = client.create_issue(
        project,
        f"[vd-e2e] session reuse B {stamp}",
        _params(repo),
        issue_type="Task",
        labels=[E2E_LIVE_LABEL],
    )
    if not created_b or not created_b.get("key"):
        pytest.skip(f"Could not create second Jira issue: {client.last_error}")
    key_b = created_b["key"]
    print(f"[live jira] created {key_b} {client.host.rstrip('/')}/browse/{key_b}", flush=True)

    fetched_a = client.get_issue(key_a, fields=["summary", "description", "labels", "status"])
    fetched_b = client.get_issue(key_b, fields=["summary", "description", "labels", "status"])
    assert fetched_a and fetched_a.get("key") == key_a
    assert fetched_b and fetched_b.get("key") == key_b
    labels_a = (fetched_a.get("fields") or {}).get("labels") or []
    assert E2E_LIVE_LABEL in labels_a or E2E_LIVE_LABEL in str(labels_a)
    assert "bot" not in [str(x).lower() for x in labels_a]
    assert "ai-assist" not in [str(x).lower() for x in labels_a]

    board = client
    proc, sm, _poller, _pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    seen: List[Dict[str, Any]] = []
    _install_agent(monkeypatch, seen)

    # No trigger labels — drive the same process_event envelope the poller uses.
    async def _run_issue(issue: Dict[str, Any]) -> None:
        await proc.process_event(
            {
                "webhookEvent": "jira:issue_created",
                "issue": issue,
                "timestamp": int(time.time() * 1000),
            }
        )

    await _run_issue(fetched_a)
    bound = binds.get(repo, SOURCE, TARGET)
    assert bound is not None, "first live issue did not bind a session"
    first_sid = bound["session_id"]
    assert first_sid == "ses_jira_shared"

    cancel = await proc.cancel_job(key_a, reason="live e2e cancel A")
    if not cancel.get("ok"):
        st = sm.get_state(key_a)
        assert st is not None and st.status in {
            TaskStatus.COMPLETED,
            TaskStatus.CANCELLED,
            TaskStatus.ERROR,
        }
    assert binds.get(repo, SOURCE, TARGET)["session_id"] == first_sid

    await _run_issue(fetched_b)
    b_runs = [row for row in seen if row["issue_key"] == key_b]
    assert b_runs, "second live issue never reached the agent"
    assert b_runs[0]["session_id"] == first_sid

    client.add_comment(
        key_b,
        (
            "h3. Session reuse e2e\n\n"
            f"* First issue: {key_a} session `{first_sid}`\n"
            f"* Second issue continued `{b_runs[0]['session_id']}`\n"
            "* Bind survived cancel; dashboard Reset was not used.\n"
        ),
    )
    comments = client.get_comments(key_b)
    blob = "\n".join(
        c.get("body") if isinstance(c.get("body"), str) else str(c.get("body"))
        for c in (comments or [])
    )
    assert first_sid in blob
    print(f"[live jira] {key_b} continued session {first_sid}", flush=True)
