"""Integration proofs for critical defects found in the 2026-09-25 full-tree review.

Each test drives production code and asserts the safe outcome. A failure is
the defect. These tests do not change product behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.azure.identity import fetch_bot_identity, reset_identity_cache
from src.azure.keys import azure_issue_key, resolve_pr_issue_key
from src.azure.webhook import decide_azure_comment_webhook, decide_azure_pr_webhook
from src.azure.workitems import find_work_item_key_by_id
from src.config import Settings
from src.dashboard.temp_storage import _run_delete_job
from src.git_manager import GitManager
from src.opencode_serve import (
    DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT,
    ServeOrchestrator,
)
from src.orchestrator.agent_runner import AgentTask
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.queue_store import WorkQueueStore
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from tests.conftest import FakeJiraClient
from tests.test_opencode_serve_e2e import FakeServeClient, _CompactLoopBackend

_METADATA = "http://169.254.169.254/latest/meta-data"
_PAT = "secret-collection-pat"
_REAL_COLLECTION = "https://tfs.example.com/tfs/DefaultCollection"


class _RecordingClient:
    """Stand-in for httpx.Client. Records every GET."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.headers = dict(kwargs.get("headers") or {})
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> "_RecordingClient":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def get(self, url: str, params: Any = None) -> httpx.Response:
        auth = str(self.headers.get("Authorization") or "")
        self.calls.append((url, auth))
        request = httpx.Request("GET", url)
        return httpx.Response(404, request=request)


def _arm_single_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_identity_cache()
    fresh = Settings()
    fresh.azure_pat = ""
    fresh.set_azure_collection_pat_map({_REAL_COLLECTION: _PAT})
    monkeypatch.setattr("src.config.settings", fresh)


class _Hold:
    calls: list[_RecordingClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.inner = _RecordingClient(*args, **kwargs)
        _Hold.calls.append(self.inner)

    def __enter__(self) -> _RecordingClient:
        return self.inner.__enter__()

    def __exit__(self, *exc: Any) -> bool:
        return self.inner.__exit__(*exc)


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    _Hold.calls = []
    _arm_single_pat(monkeypatch)
    monkeypatch.setattr("src.azure.identity.httpx.Client", _Hold)


def _leaked(host_fragment: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for client in _Hold.calls:
        for url, auth in client.calls:
            if host_fragment in url or _PAT in auth:
                out.append((url, auth))
    return out


def test_azure_pr_hook_does_not_send_pat_to_unconfigured_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated PR hook must not GET an attacker baseUrl with the stored PAT."""
    _install_recorder(monkeypatch)
    payload = {
        "eventType": "git.pullrequest.updated",
        "resource": {
            "pullRequestId": 4,
            "status": "active",
            "title": "feat: unrelated",
            "description": "desc",
            "sourceRefName": "refs/heads/feature/x",
            "targetRefName": "refs/heads/develop",
            "repository": {
                "id": "repo-guid",
                "name": "demo",
                "remoteUrl": (
                    "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
                ),
                "project": {"name": "Demo"},
            },
        },
        "resourceContainers": {"collection": {"baseUrl": _METADATA}},
    }
    decide_azure_pr_webhook(payload, enabled=True)
    leaked = _leaked("169.254.169.254")
    assert leaked == [], leaked


def test_azure_comment_guid_does_not_send_pat_off_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GUID chip with no /yaver still must not fetch identity on the hook's baseUrl."""
    _install_recorder(monkeypatch)
    payload = {
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "comment": {
                "content": "@11111111-1111-1111-1111-111111111111 hello",
            },
            "pullRequest": {
                "pullRequestId": 4,
                "title": "feat: unrelated",
                "sourceRefName": "refs/heads/feature/x",
                "targetRefName": "refs/heads/develop",
                "repository": {
                    "id": "repo-guid",
                    "name": "demo",
                    "remoteUrl": (
                        "https://tfs.example.com/tfs/DefaultCollection/Demo/_git/demo"
                    ),
                    "project": {"name": "Demo"},
                },
            },
        },
        "resourceContainers": {"collection": {"baseUrl": _METADATA}},
    }
    decide_azure_comment_webhook(
        payload, enabled=True, bot_mentions=["yaver-bot"]
    )
    leaked = _leaked("169.254.169.254")
    assert leaked == [], leaked


def test_hash_mention_ignores_work_item_with_no_collection(
    tmp_path: Path,
) -> None:
    """#42 on another collection must not bind a WIT row that has no collection."""
    sm = JiraStateManager(state_dir=tmp_path / "state")
    sm.create_state("WIT-DEMO-42", "other collection item")
    sm.update_state(
        "WIT-DEMO-42",
        metadata={
            "source": "azure_workitem",
            "azure_work_item_id": 42,
        },
    )
    other = "https://tfs.example/tfs/Other"
    assert (
        find_work_item_key_by_id(42, collection_url=other, state_manager=sm)
        == ""
    )
    key = resolve_pr_issue_key(
        pr_title="Fixes #42",
        collection_url=other,
        pr_id=9,
        project_path="Other/app",
        state_manager=sm,
    )
    assert key == azure_issue_key("Other/app", 9)
    assert key != "WIT-DEMO-42"


def test_storage_delete_worker_leaves_a_live_clone(tmp_path: Path) -> None:
    """The delete thread must refuse a path a job owns, not only the queue check."""
    target = tmp_path / "clone"
    target.mkdir()
    (target / "keep.txt").write_text("live", encoding="utf-8")

    class _Live:
        def __init__(self, path: Path) -> None:
            self.temp_dir = path

    GitManager._live_by_issue["AUDIT-LIVE"] = _Live(target)  # type: ignore[assignment]
    try:
        _run_delete_job("clone", target, "temp")
        assert target.is_dir()
        assert (target / "keep.txt").is_file()
    finally:
        GitManager._live_by_issue.pop("AUDIT-LIVE", None)


class _BusyAfterAbort(_CompactLoopBackend):
    """Compact-loop until abort, then stay busy so Continue must not be posted."""

    def __init__(self) -> None:
        super().__init__()
        self.released = False

    async def send_message(self, session_id: str, text: str, **kwargs):
        if text == DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT:
            self.released = True
        return await super().send_message(session_id, text, **kwargs)

    async def session_status(self):
        if self.aborted and not self.released:
            self.status_polls += 1
            return {self.session_id: {"type": "busy"}}
        return await super().session_status()


@pytest.mark.asyncio
async def test_compact_loop_does_not_continue_while_session_stays_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continue only after a status read that is idle. Busy after abort is incomplete."""
    orig = ServeOrchestrator._ensure_session_idle

    async def _fast(self, sid, *, _emit, _aborted, wait_seconds=30.0, abort_busy=True):
        return await orig(
            self,
            sid,
            _emit=_emit,
            _aborted=_aborted,
            wait_seconds=0.4,
            abort_busy=abort_busy,
        )

    monkeypatch.setattr(ServeOrchestrator, "_ensure_session_idle", _fast)
    backend = _BusyAfterAbort()
    orch = ServeOrchestrator(
        client=FakeServeClient(backend),
        compact_wait_seconds=5.0,
        compact_poll_seconds=0.05,
        compact_settle_seconds=0.05,
        compact_loop_cycles=3,
    )
    result = await orch.run(prompt="# Build\ndo the work", title="KAN-BUSY")
    assert DEFAULT_COMPACT_LOOP_CONTINUE_PROMPT not in backend.prompts
    assert result.incomplete is True
    assert result.returncode == 2


def _processor(tmp_path: Path) -> JobProcessor:
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.queue_store = WorkQueueStore(tmp_path / "queue")
    proc.jira_client = FakeJiraClient()
    proc.reporter = JiraReporter(client=proc.jira_client)
    return proc


def test_begin_rolls_back_when_job_file_cannot_be_written(tmp_path: Path) -> None:
    """CAS to planning must not stick when the job row cannot be stored."""
    proc = _processor(tmp_path)
    proc.state_manager.create_state("KAN-1", "plan")
    proc.job_store.create_job = lambda **_kwargs: None  # type: ignore[method-assign]
    state = proc.state_manager.get_state("KAN-1")
    assert state is not None
    task = AgentTask(
        description="Plan: KAN-1",
        prompt="plan",
        agent="derman-plan",
        issue_key="KAN-1",
    )
    proc._begin_workflow_run(
        state,
        status=TaskStatus.PLANNING,
        task=task,
        workflow_type="planning",
        agent="derman-plan",
        job_status="planning",
    )
    live = proc.state_manager.get_state("KAN-1")
    assert live is not None
    assert live.status != TaskStatus.PLANNING


def test_workspace_lock_drop_survives_concurrent_insert(tmp_path: Path) -> None:
    """A git-thread insert during drop must not pin the workspace forever."""
    proc = _processor(tmp_path)

    class _Racing(dict):
        def items(self):
            self["lock-b"] = "KAN-2"
            return dict.items(self)

    proc.note_workspace_lock("KAN-1", lock_key="lock-a")
    proc._workspace_lock_holders = _Racing(proc._workspace_lock_holders)
    proc.drop_workspace_lock("KAN-1")
    assert "lock-a" not in proc.live_workspace_lock_keys()
    store = proc.queue_store
    store.enqueue(
        source="jira",
        issue_key="KAN-9",
        lock_key="lock-a",
        repository_url="https://example/repo.git",
        work_branch="feature/KAN-9",
        target_branch="develop",
    )
    claimed = store.claim_next(
        blocked_locks=proc.live_workspace_lock_keys(),
        max_running=4,
    )
    assert claimed is not None
    assert claimed["issue_key"] == "KAN-9"


def test_fetch_bot_identity_single_pat_is_not_used_off_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct call: one configured PAT must not authenticate a different collection."""
    _install_recorder(monkeypatch)
    fetch_bot_identity(
        host="169.254.169.254",
        collection_url=_METADATA,
    )
    assert _leaked("169.254.169.254") == []
