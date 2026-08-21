"""E2E proofs for the two critical review claims.

These are not unit tests. Each case:

1. Creates a Jira issue over HTTP (REST v2 ``POST /rest/api/2/issue``)
2. Runs the real poller (board/sprint scan → ``process_issue``)
3. Watches Jira status, comments, local state, queue, and session binds
   after every step

They assert the **fixed** outcomes (abandon tombstones the issue-keyed
bind; a dropped accept posts ERROR).

Run::

    .venv/bin/python -m pytest tests/test_critical_review_claims_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from src.config import settings
from src.daemon import JiraAgentDaemon
from src.jira.poller import JiraPoller
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import bind_id_for
from tests.test_live_jira_session_reuse import (
    TRIGGER,
    _poll_and_process,
    _wire_processor,
)
from tests.test_simple_task_timing_e2e import _make_local_origin

pytest_plugins = [
    "tests.test_live_jira_session_reuse",
    "tests.test_remaining_issues_operator_e2e",
]


def _params(repo: str) -> str:
    return (
        "Critical-claim e2e (automated).\n"
        "{params}\n"
        f"Repository: {repo}\n"
        "Source branch: develop\n"
        "Target branch: develop\n"
        "Mode: build\n"
        "{params}\n"
    )


def _trace(steps: List[str], msg: str) -> None:
    steps.append(msg)
    print(f"  [e2e] {msg}")


def _comment_bodies(board, key: str) -> str:
    comments = board.get_comments(key) if hasattr(board, "get_comments") else []
    return "\n".join(str(c.get("body") or c) for c in comments)


# ---------------------------------------------------------------------------
# Claim 1 — forget_for hashes without issue_key; next Jira rework resumes
# the abandoned session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_jira_rework_resumes_abandoned_session(
    tmp_path, monkeypatch, sim_jira, isolate_jira_agent_artifacts
):
    """HTTP create → poller → build → abandon → To Do rework → same ses_*.

    Claim: ``forget_for`` tombstones ``osb_{hash(..., "")}`` while production
    upserts ``osb_{hash(..., ISSUE)}``. After an empty-timeout abandon the
    live bind still points at the hung session, so the next To Do re-queue
    attaches it again.
    """
    board, _srv = sim_jira
    origin = _make_local_origin(tmp_path / "git")
    repo = origin.resolve().as_uri()
    proc, sm, poller, pending, _ = _wire_processor(tmp_path, monkeypatch, board, repo)
    binds = isolate_jira_agent_artifacts["session_bind_store"]
    steps: List[str] = []
    runs: List[Dict[str, Any]] = []

    monkeypatch.setattr(settings, "agent_task_max_retries", 0)
    monkeypatch.setattr(settings, "agent_task_max_incomplete_retries", 0)

    async def fake_run(self, task: AgentTask, **kwargs: Any) -> Dict[str, Any]:
        n = len(runs) + 1
        runs.append(
            {
                "issue_key": task.issue_key,
                "session_id": task.session_id,
                "n": n,
            }
        )
        on_sid = kwargs.get("on_session_id")
        if n == 1:
            if on_sid:
                on_sid("ses_hung")
            task.abandoned_session_id = "ses_hung"
            return {
                "task_id": task.task_id,
                "returncode": 1,
                "stdout": "",
                "stderr": "timeout: empty session log",
                "session_file": None,
                "opencode_session_id": "ses_hung",
                "timed_out": True,
                "progress": 0,
            }
        if on_sid:
            on_sid("ses_new")
        return {
            "task_id": task.task_id,
            "returncode": 1,
            "stdout": "",
            "stderr": "cold start failed",
            "session_file": None,
            "opencode_session_id": "ses_new",
            "progress": 0,
        }

    monkeypatch.setattr(AgentRunner, "run_agent", fake_run)

    _trace(steps, "POST Jira issue (To Do + bot)")
    created = board.create_issue(
        summary="[vd-e2e] abandon-bind claim",
        description=_params(repo),
        labels=[TRIGGER, "vd-claim-e2e"],
    )
    assert created and created.get("key"), getattr(board.inner, "last_error", None)
    key = created["key"]

    live0 = board.get_issue(key)
    assert live0["fields"]["status"]["name"].lower() == "to do"
    _trace(steps, f"GET {key} status=To Do")

    processed = await _poll_and_process(proc, poller, pending)
    assert key in processed
    _trace(steps, f"poller accepted {key}; processor ran agent")

    live1 = board.get_issue(key)
    assert live1["fields"]["status"]["name"].lower() == "in progress"
    _trace(steps, f"GET {key} status=In Progress (accept)")

    work = f"feature/{key}"
    live_id = bind_id_for(repo, work, "develop", issue_key=key)
    legacy_id = bind_id_for(repo, work, "develop", issue_key="")
    assert live_id != legacy_id
    _trace(steps, f"bind ids differ live={live_id} legacy={legacy_id}")

    rec = binds.get_by_id(live_id)
    assert rec is not None, "first run must upsert the issue-keyed bind"
    assert rec["bind_id"] == live_id
    forgotten = list(rec.get("forgotten_session_ids") or [])
    _trace(
        steps,
        f"after abandon: live bind session={rec.get('session_id')!r} "
        f"forgotten={forgotten}",
    )

    assert "ses_hung" in forgotten, (
        "abandon must tombstone the issue-keyed bind "
        f"(live={live_id} forgotten={forgotten})"
    )
    assert not (rec.get("session_id") or "").strip(), (
        "abandoned bind must drop the live ses_* pointer"
    )

    legacy = binds.get_by_id(legacy_id)
    _trace(
        steps,
        "legacy bind "
        + (
            f"session={legacy.get('session_id')!r} "
            f"forgotten={legacy.get('forgotten_session_ids')}"
            if legacy
            else "absent"
        ),
    )

    st = sm.get_state(key)
    assert st is not None
    assert st.status == TaskStatus.ERROR
    bodies = _comment_bodies(board, key)
    assert bodies, "fail path must still comment on the first run"
    _trace(steps, f"local status={st.status.value}; Jira comments present")

    _trace(steps, "operator moves ticket back to To Do (rework)")
    assert board.update_issue(key, fields={"status": "To Do"})
    live2 = board.get_issue(key)
    assert live2["fields"]["status"]["name"].lower() == "to do"

    processed2 = await _poll_and_process(proc, poller, pending)
    assert key in processed2
    _trace(steps, f"poller re-queued {key} from To Do")

    second = [r for r in runs if r["n"] == 2]
    assert second, f"second agent run missing; runs={runs}"
    _trace(steps, f"second agent attach session_id={second[0]['session_id']!r}")

    assert second[0]["session_id"] in (None, ""), (
        "rework must start cold; abandoned ses_hung must not be attached "
        f"(got {second[0]['session_id']!r})"
    )
    still = binds.get_by_id(live_id)
    assert still is not None
    assert "ses_hung" in (still.get("forgotten_session_ids") or [])
    assert still.get("session_id") != "ses_hung"
    _trace(steps, "abandon tombstone held; rework started a cold session")


# ---------------------------------------------------------------------------
# Claim 2 — accept moves Jira to In Progress, then the live daemon handler
# can drop the event with no ERROR comment and no local job
# ---------------------------------------------------------------------------


async def _real_daemon_handler(daemon: JiraAgentDaemon):
    """Return the nested handler from ``JiraAgentDaemon._start_poller``."""
    captured: Dict[str, Any] = {}

    async def fake_run_in_executor(_executor, _fn, *args):
        if args:
            captured["handler"] = args[0]
        return None

    loop = asyncio.get_running_loop()
    daemon._main_loop = loop
    daemon._running = True
    daemon._stopping = False
    with patch("src.daemon.JiraPoller") as Poller:
        Poller.return_value = MagicMock()
        with patch.object(loop, "run_in_executor", side_effect=fake_run_in_executor):
            await daemon._start_poller()
    handler = captured.get("handler")
    assert handler is not None
    return handler


@pytest.mark.asyncio
async def test_e2e_jira_api_accept_then_stopping_handler_is_silent(
    tmp_path, monkeypatch, jira
):
    """POST issue → poller accept → daemon handler no-ops because stopping.

    Control (already in remaining_issues): handler is None → ERROR comment.
    This claim: handler is the **real** daemon handler, bound, but
    ``_stopping`` is True. Jira stays In Progress **and** an ERROR comment
    + local ERROR must be recorded so the ticket is not silent.
    """
    world, base, client = jira
    steps: List[str] = []
    sm = JiraStateManager(state_dir=tmp_path / "state")
    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    monkeypatch.setattr(settings, "jira_board_id", "10")

    with patch("src.processor.create_jira_client", return_value=client):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = client
    proc.reporter = JiraReporter(client=client)

    daemon = JiraAgentDaemon()
    daemon.processor = proc
    daemon.state_manager = sm
    handler = await _real_daemon_handler(daemon)
    daemon._stopping = True
    _trace(steps, "captured real daemon poller handler; set _stopping=True")

    _trace(steps, "POST /rest/api/2/issue (To Do + bot)")
    created = client.create_issue(
        "KAN",
        "[vd-e2e] silent-accept claim",
        "Mode: build\n{params}\nRepository: https://example.com/g/r.git\n"
        "Source branch: develop\nTarget branch: develop\nMode: build\n{params}",
        labels=["bot"],
    )
    assert created and created.get("key"), client.last_error
    key = created["key"]
    before = client.get_issue(key)
    assert before["fields"]["status"]["name"] == "To Do"
    _trace(steps, f"GET {key} status=To Do comments={len(world.issues[key]['comments'])}")

    poller = JiraPoller(
        client=client, interval_seconds=1, board_id="10", state_manager=sm
    )
    poller._handler = handler
    issues = poller.poll_board()
    match = [i for i in issues if i["key"] == key]
    assert match, f"{key} not on board; poller returned {[i['key'] for i in issues]}"
    _trace(steps, f"poll_board will_process {key}")

    poller.process_issue(match[0], is_update=False)
    _trace(steps, "process_issue returned (handler bound, stopping)")

    live = client.get_issue(key)
    status = live["fields"]["status"]["name"]
    comments = list(world.issues[key]["comments"])
    st = sm.get_state(key)
    queued = proc.queue_store.find_open_jira(key) if hasattr(proc, "queue_store") else None
    _trace(
        steps,
        f"after accept: jira={status} comments={len(comments)} "
        f"local={st.status.value if st else 'NO_STATE'} queue={queued}",
    )

    assert status == "In Progress"
    assert comments, "dropped accept must post a Jira ERROR comment"
    blob = "\n".join(str(c) for c in comments)
    assert "ERROR" in blob
    assert st is not None and st.status == TaskStatus.ERROR, (
        f"dropped accept must record local ERROR (got {st})"
    )
    assert not queued

    again = poller.poll_board()
    again_keys = [i["key"] for i in again]
    _trace(steps, f"next poll_board keys={again_keys}")
    assert key not in again_keys, (
        "In Progress ticket must not be re-selected until operator To Do"
    )
    _trace(steps, "dropped accept recorded ERROR; next poll skipped In Progress")


@pytest.mark.asyncio
async def test_e2e_jira_api_accept_then_enqueue_crash_is_silent(
    tmp_path, monkeypatch, jira
):
    """Same Jira API path, but enqueue raises after In Progress transition."""
    world, base, client = jira
    steps: List[str] = []
    sm = JiraStateManager(state_dir=tmp_path / "state")
    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    monkeypatch.setattr(settings, "jira_board_id", "10")

    with patch("src.processor.create_jira_client", return_value=client):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.jira_client = client
    proc.reporter = JiraReporter(client=client)

    async def boom(_event):
        raise RuntimeError("queue dir unwritable")

    proc.enqueue_jira_event = boom  # type: ignore[method-assign]

    daemon = JiraAgentDaemon()
    daemon.processor = proc
    daemon.state_manager = sm
    handler = await _real_daemon_handler(daemon)
    daemon._stopping = False
    daemon._running = True
    _trace(steps, "real daemon handler; enqueue_jira_event raises")

    created = client.create_issue(
        "KAN",
        "[vd-e2e] enqueue-crash claim",
        "Mode: build",
        labels=["bot"],
    )
    key = created["key"]
    _trace(steps, f"POST issue {key}")

    poller = JiraPoller(
        client=client, interval_seconds=1, board_id="10", state_manager=sm
    )
    poller._handler = handler
    issues = poller.poll_board()
    match = [i for i in issues if i["key"] == key]
    assert match
    poller.process_issue(match[0], is_update=False)
    await asyncio.sleep(0.3)
    _trace(steps, "process_issue + enqueue future settled")

    live = client.get_issue(key)
    comments = list(world.issues[key]["comments"])
    st = sm.get_state(key)
    _trace(
        steps,
        f"jira={live['fields']['status']['name']} comments={len(comments)} "
        f"local={st.status.value if st else 'NO_STATE'}",
    )
    assert live["fields"]["status"]["name"] == "In Progress"
    assert comments, "enqueue crash must post a Jira ERROR comment"
    assert st is not None and st.status == TaskStatus.ERROR
    _trace(steps, "enqueue crash recorded ERROR on In Progress ticket")


def test_e2e_control_missing_handler_does_post_error(tmp_path, monkeypatch, jira):
    """Contrast: handler is None — operator *does* get an ERROR comment."""
    world, _base, client = jira
    sm = JiraStateManager(state_dir=tmp_path / "state")
    monkeypatch.setattr(settings, "trigger_labels", "bot,ai-assist")
    monkeypatch.setattr(settings, "trigger_on_assignment", False)
    monkeypatch.setattr(settings, "jira_board_id", "10")

    created = client.create_issue("KAN", "[vd-e2e] control missing handler", "d", labels=["bot"])
    key = created["key"]
    poller = JiraPoller(
        client=client, interval_seconds=1, board_id="10", state_manager=sm
    )
    poller._handler = None
    issues = poller.poll_board()
    match = [i for i in issues if i["key"] == key]
    assert match
    poller.process_issue(match[0], is_update=False)

    live = client.get_issue(key)
    assert live["fields"]["status"]["name"] == "In Progress"
    st = sm.get_state(key)
    assert st is not None and st.status == TaskStatus.ERROR
    assert world.issues[key]["comments"], "missing-handler path must notify Jira"
