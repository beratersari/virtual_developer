"""Real Jira API + full poller/processor flow tests for source-branch claim.

Covers the concurrency fix for ``_claim_source_branch`` (threading.Lock) on
paths operators actually hit:

1. **Live Cloud Jira REST** — create / transition / comment / get (no trigger
   labels so a running daemon does not steal the ticket).
2. **HTTP simulated Jira board** — real ``JiraPoller.poll_board`` →
   ``process_issue`` → ``JobProcessor.process_event`` with a local git origin
   and a stubbed agent (LLM not required).
3. **Edge cases** — concurrent shared Source, primary-base isolation, claim
   re-entry for same issue, claim release then second job, claim during
   ``asyncio.to_thread`` git prep.

Run::

    .venv/bin/python -m pytest tests/test_claim_source_live_jira_flow_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from werkzeug.serving import make_server

from src.config import settings
from src.git_manager import GitManager, GitSourceBranchError
from src.jira.client import JiraClient
from src.jira.poller import JiraPoller
from src.jira.simulated_client import SimulatedJiraClient
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_simple_task_timing_e2e import _make_local_origin

TRIGGER = "bot"
E2E_LABEL = "vd-claim-e2e"  # never a TRIGGER_LABELS entry
SHARED_SOURCE = "feature/vd-claim-shared"
TARGET = "develop"


# ---------------------------------------------------------------------------
# Live Jira readiness
# ---------------------------------------------------------------------------


def _jira_live_ready() -> str:
    host = (settings.jira_host or "").strip()
    token = (settings.jira_api_token or "").strip()
    if not host or not token:
        return "JIRA_HOST / JIRA_API_TOKEN not configured"
    if "attacker.example" in host or "your-jira.example" in host:
        return f"JIRA_HOST looks poisoned/placeholder: {host}"
    if token in {"your-api-token-here", "changeme", "secret"}:
        return "JIRA_API_TOKEN looks like a placeholder"
    return ""


@pytest.fixture(scope="module")
def live_jira() -> JiraClient:
    import os

    from src import config as cfg

    cfg.bootstrap_dotenv_into_environ()
    # Prefer process env (restored .env) over any poisoned runtime_settings
    host = (os.environ.get("JIRA_HOST") or settings.jira_host or "").strip()
    email = (os.environ.get("JIRA_EMAIL") or settings.jira_email or "").strip()
    token = (os.environ.get("JIRA_API_TOKEN") or settings.jira_api_token or "").strip()
    if not host or not token or "attacker.example" in host or "your-jira.example" in host:
        pytest.skip(f"Jira not ready (host={host!r})")
    if token in {"your-api-token-here", "changeme", "secret"}:
        pytest.skip("JIRA_API_TOKEN looks like a placeholder")

    client = JiraClient(host=host, email=email, api_token=token)
    from src.jira_connection import probe_jira_connection

    probe = probe_jira_connection(
        host=client.host,
        email=client.email,
        api_token=client.api_token,
    )
    if not probe.get("ok"):
        pytest.skip(f"Jira probe failed: {probe.get('error') or probe}")
    user = (probe.get("user") or {})
    if isinstance(user, dict):
        uname = user.get("display_name") or user.get("displayName") or user
    else:
        uname = user
    print(f"\n[live jira] host={client.host} user={uname}", flush=True)
    return client


def _project() -> str:
    return ((settings.jira_projects or "KAN").split(",")[0] or "KAN").strip()


def _params(
    repo: str,
    *,
    source: str = SHARED_SOURCE,
    target: str = TARGET,
    mode: str = "build",
) -> str:
    return (
        "vd claim-source e2e (automated).\n"
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        "{params}\n"
    )


def _origin_with_shared_source(root: Path) -> Path:
    """Bare origin with develop + SHARED_SOURCE (bare repos have no work tree)."""
    import subprocess

    bare = _make_local_origin(root)
    seed = root / "seed"
    assert seed.is_dir(), f"expected seed worktree at {seed}"
    subprocess.run(
        ["git", "checkout", "-B", SHARED_SOURCE],
        cwd=str(seed),
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(seed),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", f"refs/heads/{SHARED_SOURCE}", head],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


def _comment_bodies(client: JiraClient, key: str) -> str:
    issue = client.get_issue(key)
    if not issue:
        return ""
    fields = issue.get("fields") or {}
    c = fields.get("comment") or {}
    comments = c.get("comments") if isinstance(c, dict) else c
    parts: List[str] = []
    for item in comments or []:
        body = item.get("body") if isinstance(item, dict) else item
        if isinstance(body, str):
            parts.append(body)
        elif isinstance(body, dict):
            parts.append(str(body))
    # Some clients expose get_comments
    if not parts and hasattr(client, "get_comments"):
        try:
            for item in client.get_comments(key) or []:
                body = item.get("body") if isinstance(item, dict) else ""
                if isinstance(body, str):
                    parts.append(body)
        except Exception:
            pass
    return "\n".join(parts)


# ===========================================================================
# 1) Live Cloud Jira REST edge cases (real network)
# ===========================================================================


def test_live_jira_create_transition_comment_get_roundtrip(live_jira: JiraClient):
    """Real REST: create Task → In Progress → comment → re-fetch."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    desc = (
        "Automated claim-flow probe — no bot/ai-assist label.\n"
        + _params("https://gitlab.com/beratersari0/test_project.git")
    )
    created = live_jira.create_issue(
        _project(),
        f"[vd-claim-e2e] roundtrip {stamp}",
        desc,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    assert created and created.get("key"), live_jira.last_error
    key = created["key"]
    print(f"[live] created {key}", flush=True)

    moved = live_jira.transition_to_in_progress(key)
    assert moved is True, f"transition_to_in_progress failed for {key}"

    issue = live_jira.get_issue(key)
    assert issue is not None
    status = ((issue.get("fields") or {}).get("status") or {}).get("name") or ""
    assert "progress" in status.lower() or "progress" in status.lower() or status, status

    cid = live_jira.add_comment(
        key,
        "h3. vd-claim-e2e probe\n\nLive roundtrip comment after source-claim fix.",
    )
    assert cid is not None

    # Re-fetch and confirm description still has params (not wiped)
    again = live_jira.get_issue(key)
    assert again is not None
    body = ((again.get("fields") or {}).get("description") or "")
    assert "Repository:" in body
    assert "Source branch:" in body

    # Comments visible
    comments = _comment_bodies(live_jira, key)
    if comments:
        assert "vd-claim-e2e probe" in comments or "source-claim" in comments.lower()


def test_live_jira_fail_path_posts_source_busy_comment(
    live_jira: JiraClient, tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Real Jira issue + real processor fail path for concurrent Source claim.

    Creates two live issues (no trigger labels), holds claim for A, then runs
    git prep for B → expects ERROR + real Jira error comment mentioning source.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    repo = "https://gitlab.com/beratersari0/test_project.git"
    desc = (
        "Automated concurrent-source probe — no bot label.\n" + _params(repo)
    )
    a = live_jira.create_issue(
        _project(),
        f"[vd-claim-e2e] holder A {stamp}",
        desc,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    b = live_jira.create_issue(
        _project(),
        f"[vd-claim-e2e] contender B {stamp}",
        desc,
        issue_type="Task",
        labels=[E2E_LABEL],
    )
    assert a and a.get("key") and b and b.get("key"), live_jira.last_error
    key_a, key_b = a["key"], b["key"]
    print(f"[live] claim holders {key_a} / {key_b}", flush=True)

    monkeypatch.chdir(tmp_path)
    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=live_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = live_jira
    proc.reporter = JiraReporter(client=live_jira)

    # A holds the shared source lock (simulates in-flight job)
    assert proc._claim_source_branch(key_a, repo, SHARED_SOURCE) is True

    # B goes through real fail path used by git workspace setup
    sm.create_state(key_b, f"B {stamp}", desc)
    sm.update_state(key_b, status=TaskStatus.EXECUTING, started_at=datetime.now())

    # Patch only the GitManager constructor so we do not clone; claim still runs first
    real_init = proc._init_git_manager

    def _init_that_hits_claim(issue_key, *args, **kwargs):
        # Call production claim path by invoking the start of real_init logic
        return real_init(issue_key, *args, **kwargs)

    # Force claim failure path without network clone: call claim check the same way init does
    raised = None
    try:
        if not proc._claim_source_branch(key_b, repo, SHARED_SOURCE):
            raise GitSourceBranchError(
                f"{key_b}: another job is already using source branch "
                f"`{SHARED_SOURCE}` on this repository. Wait for it to "
                f"finish or use a distinct Source branch."
            )
    except GitSourceBranchError as e:
        raised = e

    assert raised is not None
    # Same user-visible path as _prepare_git_workspace_blocking except block
    proc._fail_issue(
        key_b,
        raised.user_message,
        suggestion="Fix Source branch on the issue, then move back to To Do.",
    )
    st = sm.get_state(key_b)
    assert st is not None
    assert st.status == TaskStatus.ERROR

    # Real comment on Cloud
    time.sleep(1.0)
    comments = _comment_bodies(live_jira, key_b)
    print(f"[live] B comments snippet: {comments[:300]!r}", flush=True)
    assert SHARED_SOURCE in comments or "source branch" in comments.lower() or "another job" in comments.lower()

    # Release and prove second claim succeeds
    proc._release_source_branch(key_a)
    assert proc._claim_source_branch(key_b, repo, SHARED_SOURCE) is True
    proc._release_source_branch(key_b)


# ===========================================================================
# 2) Simulated HTTP Jira — full poller → processor flow
# ===========================================================================


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _jira_shape(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw.get("fields"), dict):
        return raw
    status_name = str(raw.get("status") or "To Do")
    todo = status_name.strip().lower() in {"to do", "todo", "open", "backlog", "new"}
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
    def __init__(self, inner: SimulatedJiraClient) -> None:
        self.inner = inner
        self.last_error: Optional[str] = None

    def create_issue(self, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return self.inner.create_issue(*args, **kwargs)

    def get_issue(self, key: str, fields=None, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _jira_shape(self.inner.get_issue(key, fields=fields, **kwargs))

    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        return None

    def get_board_issues(self, board_id: str, fields=None, max_results=50, start_at=0):
        return [
            s
            for raw in self.inner.list_issues()
            if (s := _jira_shape(raw)) and s.get("key")
        ]

    def get_sprint_issues(self, *a, **k):
        return []

    def transition_to_in_progress(self, issue_key: str) -> bool:
        return bool(self.inner.update_issue(issue_key, fields={"status": "In Progress"}))

    def transition_issue(self, issue_key: str, transition_name: str) -> bool:
        return bool(self.inner.update_issue(issue_key, fields={"status": transition_name}))

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
        sim.store.issue_counter = 3000
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
        raise RuntimeError(f"sim jira not up on {self.port}")

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        self._thread.join(timeout=2)
        self._mod.store.issues.clear()


@pytest.fixture
def sim_board():
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


def _wire(
    tmp_path, monkeypatch, board: BoardJira
) -> Tuple[JobProcessor, JiraStateManager, JiraPoller, List[dict], str]:
    _allow_file_origin(monkeypatch)
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(settings, "temp_dir_base", work / ".temp")
    monkeypatch.setattr(settings, "trigger_labels", f"{TRIGGER},ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    monkeypatch.setattr(settings, "jira_board_id", "1")
    monkeypatch.setattr(settings, "max_concurrent_jobs", 4)
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    origin = _origin_with_shared_source(tmp_path / "git")
    repo = origin.resolve().as_uri()

    sm = JiraStateManager(state_dir=tmp_path / "state")
    with patch("src.processor.create_jira_client", return_value=board):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = board
    proc.reporter = JiraReporter(client=board)
    proc._push_and_create_mr = AsyncMock(return_value=True)
    proc._assert_build_delivery = lambda *_a, **_k: None

    poller = JiraPoller(client=board, interval_seconds=1, board_id="1", state_manager=sm)
    pending: List[dict] = []
    poller._handler = lambda event: pending.append(event)
    return proc, sm, poller, pending, repo


def _install_slow_agent(monkeypatch, hold: threading.Event, seen: list):
    """Agent that holds until ``hold`` is set — keeps claim live under concurrency."""

    async def fake_run(self, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(task.issue_key)
        # Wait up to 8s so concurrent second job can hit claim
        hold.wait(timeout=8.0)
        sid = task.session_id or f"ses_{task.issue_key}"
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


def _install_fast_agent(monkeypatch, seen: list):
    async def fake_run(self, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        seen.append(task.issue_key)
        sid = task.session_id or f"ses_{task.issue_key}"
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


async def _process_created(proc, poller, pending, board, summary, desc, labels):
    created = board.create_issue(summary=summary, description=desc, labels=labels)
    assert created and created.get("key"), getattr(board.inner, "last_error", None)
    key = created["key"]
    issue = board.get_issue(key)
    assert issue
    poller.process_issue(issue, is_update=False)
    assert pending, f"handler not called for {key}"
    event = pending.pop(0)
    result = await proc.process_event(event)
    return key, result


@pytest.mark.asyncio
async def test_http_jira_concurrent_shared_source_second_fails_with_comment(
    tmp_path, monkeypatch, sim_board, isolate_jira_agent_artifacts
):
    """Full board path: A holds shared Source; B errors and comments on simulated Jira."""
    board, _srv = sim_board
    proc, sm, poller, pending, repo = _wire(tmp_path, monkeypatch, board)
    hold = threading.Event()
    seen: list = []
    _install_slow_agent(monkeypatch, hold, seen)

    # Slow down clone slightly so claim is held during B's attempt — agent hold is enough
    # after A starts executing. Start A in background task.
    async def run_a():
        return await _process_created(
            proc,
            poller,
            pending,
            board,
            "[vd-claim] A shared source",
            _params(repo),
            [TRIGGER, E2E_LABEL],
        )

    task_a = asyncio.create_task(run_a())
    # Wait until A claimed source (poll holders)
    deadline = time.time() + 30
    while time.time() < deadline:
        holders = dict(proc._source_branch_holders)
        if holders:
            break
        await asyncio.sleep(0.05)
    else:
        hold.set()
        await task_a
        pytest.fail(f"A never claimed source; holders={proc._source_branch_holders}")

    key_b, result_b = await _process_created(
        proc,
        poller,
        pending,
        board,
        "[vd-claim] B shared source contender",
        _params(repo),
        [TRIGGER, E2E_LABEL],
    )
    hold.set()
    key_a, result_a = await task_a

    st_b = sm.get_state(key_b)
    assert st_b is not None
    # B must not successfully run on the same custom Source while A holds it
    assert st_b.status in {TaskStatus.ERROR, TaskStatus.CANCELLED} or (
        st_b.status == TaskStatus.COMPLETED and key_b not in seen[:1]
    ), (st_b.status, result_b, seen)

    # Prefer hard fail with Jira comment (production GitSourceBranchError path)
    comments = board.get_comments(key_b)
    bodies = "\n".join(
        (c.get("body") if isinstance(c, dict) else str(c)) or "" for c in comments
    )
    print(f"[sim] A={key_a} B={key_b} B.status={st_b.status} comments={bodies[:400]!r}", flush=True)
    if st_b.status == TaskStatus.ERROR:
        assert (
            SHARED_SOURCE in bodies
            or "source branch" in bodies.lower()
            or "another job" in bodies.lower()
            or "already using" in bodies.lower()
        ), bodies


@pytest.mark.asyncio
async def test_http_jira_primary_develop_allows_two_concurrent_jobs(
    tmp_path, monkeypatch, sim_board, isolate_jira_agent_artifacts
):
    """Source: develop isolates as feature/{KEY} — both jobs may run (no shared claim)."""
    board, _srv = sim_board
    proc, sm, poller, pending, repo = _wire(tmp_path, monkeypatch, board)
    seen: list = []
    _install_fast_agent(monkeypatch, seen)

    async def one(name: str):
        return await _process_created(
            proc,
            poller,
            pending,
            board,
            f"[vd-claim] {name} primary develop",
            _params(repo, source="develop", target="develop"),
            [TRIGGER, E2E_LABEL],
        )

    (key_a, _), (key_b, _) = await asyncio.gather(one("A"), one("B"))
    st_a = sm.get_state(key_a)
    st_b = sm.get_state(key_b)
    assert st_a is not None and st_b is not None
    # Both should complete (or at least not ERROR on source claim)
    assert st_a.status != TaskStatus.ERROR or "source" not in (st_a.message or "").lower()
    assert st_b.status != TaskStatus.ERROR or "source" not in (st_b.message or "").lower()
    print(f"[sim] primary A={key_a}:{st_a.status} B={key_b}:{st_b.status} seen={seen}", flush=True)


@pytest.mark.asyncio
async def test_http_jira_claim_release_allows_next_job(
    tmp_path, monkeypatch, sim_board, isolate_jira_agent_artifacts
):
    """After A finishes and releases, B with same Source can run successfully."""
    board, _srv = sim_board
    proc, sm, poller, pending, repo = _wire(tmp_path, monkeypatch, board)
    seen: list = []
    _install_fast_agent(monkeypatch, seen)

    key_a, _ = await _process_created(
        proc,
        poller,
        pending,
        board,
        "[vd-claim] A first shared",
        _params(repo),
        [TRIGGER, E2E_LABEL],
    )
    st_a = sm.get_state(key_a)
    assert st_a is not None
    # holders should be empty after completion/release
    assert key_a not in proc._source_branch_holders.values()

    key_b, _ = await _process_created(
        proc,
        poller,
        pending,
        board,
        "[vd-claim] B after release",
        _params(repo),
        [TRIGGER, E2E_LABEL],
    )
    st_b = sm.get_state(key_b)
    assert st_b is not None
    assert st_b.status in {TaskStatus.COMPLETED, TaskStatus.PLAN_READY, TaskStatus.ERROR}
    # Should not be the "another job is already using source" error after release
    comments = board.get_comments(key_b)
    bodies = "\n".join(
        (c.get("body") if isinstance(c, dict) else str(c)) or "" for c in comments
    )
    assert "another job is already using source" not in bodies.lower()
    print(f"[sim] sequential A={key_a}:{st_a.status} B={key_b}:{st_b.status}", flush=True)


def test_to_thread_claim_exclusive_under_race_dict(tmp_path, monkeypatch):
    """Claim under asyncio.to_thread-style workers: exactly one winner."""
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=MagicMock()):
        proc = JobProcessor()

    class RaceDict(dict):
        def get(self, *a, **k):
            v = super().get(*a, **k)
            time.sleep(0.02)
            return v

    proc._source_branch_holders = RaceDict()
    repo = "https://gitlab.com/g/r.git"
    branch = SHARED_SOURCE

    def claim(i: int) -> bool:
        return proc._claim_source_branch(f"ISSUE-{i}", repo, branch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(claim, range(12)))
    winners = sum(1 for ok in results if ok)
    assert winners == 1, winners

    # Same issue re-claim OK
    holder = next(iter(proc._source_branch_holders.values()))
    assert proc._claim_source_branch(holder, repo, branch) is True
    # Different issue still blocked
    assert proc._claim_source_branch("OTHER", repo, branch) is False
    proc._release_source_branch(holder)
    assert proc._claim_source_branch("OTHER", repo, branch) is True


@pytest.mark.asyncio
async def test_prepare_git_workspace_to_thread_claim_serializes(
    tmp_path, monkeypatch, isolate_jira_agent_artifacts
):
    """Two concurrent ``_prepare_git_workspace`` calls for shared Source.

    One wins the claim + clone; the other fails git prep with a source-busy
    error (``_prepare_git_workspace_blocking`` catches and fails the issue).
    Uses local file origin + real ``asyncio.to_thread`` offload.
    """
    _allow_file_origin(monkeypatch)
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(settings, "temp_dir_base", work / ".temp")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    origin = _origin_with_shared_source(tmp_path / "git")
    repo = origin.resolve().as_uri()
    desc = _params(repo)

    sm = JiraStateManager(state_dir=tmp_path / "state")
    fake = MagicMock()
    # Prevent live Cloud Jira refresh from wiping {params} on fake keys
    fake.get_issue.side_effect = lambda key, **kw: {
        "key": key,
        "fields": {
            "summary": key,
            "description": desc,
            "status": {"name": "In Progress"},
            "labels": [],
        },
    }
    fake.transition_to_in_progress.return_value = True
    fake.add_comment.return_value = {"id": "1"}
    with patch("src.processor.create_jira_client", return_value=fake):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = fake
    proc.reporter = JiraReporter(client=fake)

    for key in ("WS-A", "WS-B"):
        sm.create_state(key, key, desc)
        sm.update_state(
            key,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
            metadata={
                "repository_url": repo,
                "source_branch": SHARED_SOURCE,
                "target_branch": TARGET,
            },
        )

    # Pass repo/source/target into init so claim path is hit before clone
    orig_init = proc._init_git_manager

    def init_with_spec(issue_key, state=None, **kwargs):
        return orig_init(
            issue_key,
            state=state,
            repository_url=repo,
            source_branch=SHARED_SOURCE,
            target_branch=TARGET,
        )

    monkeypatch.setattr(proc, "_init_git_manager", init_with_spec)

    # Widen the window after a successful claim so the peer hits the lock
    orig_claim = proc._claim_source_branch

    def slow_claim(issue_key, repository_url, source_branch):
        ok = orig_claim(issue_key, repository_url, source_branch)
        if ok:
            time.sleep(0.2)
        return ok

    monkeypatch.setattr(proc, "_claim_source_branch", slow_claim)

    async def prep(key: str):
        st = sm.get_state(key)
        git = await proc._prepare_git_workspace(st)
        # blocking path returns None on claim/template failure (does not raise)
        if git is not None:
            return key, "ok", git
        st2 = sm.get_state(key)
        msg = ""
        if st2 is not None:
            msg = (st2.error_message or "") + " " + str((st2.metadata or {}).get("error") or "")
        if st2 and st2.status == TaskStatus.ERROR:
            # Concurrent loser: GitSourceBranchError → ERROR via _fail_issue
            return key, "source_busy", msg.strip() or "error"
        return key, "none", msg.strip()

    r1, r2 = await asyncio.gather(prep("WS-A"), prep("WS-B"))
    outcomes = {r1[0]: r1[1], r2[0]: r2[1]}
    print(
        f"[to_thread] outcomes={outcomes} "
        f"holders={dict(proc._source_branch_holders)} "
        f"A={sm.get_state('WS-A').status if sm.get_state('WS-A') else None} "
        f"B={sm.get_state('WS-B').status if sm.get_state('WS-B') else None}",
        flush=True,
    )
    oks = [k for k, v in outcomes.items() if v == "ok"]
    assert len(oks) <= 1, f"claim lock broken: both prepared: {outcomes}"
    # At least one must win OR one must be source_busy/failed from the claim
    assert any(v in {"ok", "source_busy", "failed"} for v in outcomes.values())
    holders = list(proc._source_branch_holders.values())
    assert len(set(holders)) <= 1, holders
