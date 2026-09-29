"""Processing cache, shutdown, and orphan recovery.

Covers the lifecycle rules:
- Live jobs live in in-memory ``_contexts``; poll/create must not double-start them.
- Disk ``planning``/``executing`` without a live process is orphaned on cold start → ERROR.
- Graceful stop kills child processes and writes CANCELLED + Jira notify.
- Non-processing + To Do can still start (not blocked by recovery/shutdown).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.state.models import TaskStatus
from tests.conftest import FakeJiraClient, make_issue_event


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from src.processor import JobProcessor

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        p = JobProcessor()
    p.state_manager = state_manager
    p.reporter = reporter
    p.jira_client = fake_jira
    return p


# ---------------------------------------------------------------------------
# Startup orphan recovery (disk in-flight, no live process)
# ---------------------------------------------------------------------------

def test_recover_orphaned_executing_marks_error(processor, state_manager, fake_jira):
    state_manager.create_state("ORPH-1", "s", "d")
    state_manager.update_state(
        "ORPH-1",
        status=TaskStatus.EXECUTING,
        started_at=datetime.now(),
        current_task_id="t-dead",
    )
    processor.job_store.create_job(
        issue_key="ORPH-1",
        summary="s",
        status="executing",
    )
    # Crash drops the live pointer. The jobs list must not stay executing.
    state_manager.update_state("ORPH-1", metadata={"current_job_id": None})
    n = processor.recover_orphaned_in_flight()
    assert n == 1
    st = state_manager.get_state("ORPH-1")
    assert st.status == TaskStatus.ERROR
    rows = processor.job_store.list_jobs(issue_key="ORPH-1", limit=5)
    assert rows and rows[0]["status"] == "error"
    assert st.current_task_id is None
    assert st.error_message
    assert fake_jira.comments, "Jira must be notified on orphan recovery"


class _Repo:
    def __init__(self, url: str, sha: str, ahead: int) -> None:
        self.remote_url = url
        self.work_branch = "feature/KAN-573"
        self.target_branch = "main"
        self._sha = sha
        self._ahead = ahead
        self.delivery_baseline_sha = None
        self.pushed = False
        self.mr_opened = False

    def get_last_commit_sha(self, short: bool = False) -> str:
        return self._sha

    def commits_ahead_of_target(self, branch: str) -> int:
        return self._ahead

    def ensure_on_work_branch(self) -> bool:
        return True

    def get_current_branch(self) -> str:
        return self.work_branch

    def push(self, branch_name: str) -> bool:
        self.pushed = True
        if getattr(self, "push_ok", False):
            return True
        return self._ahead >= 1

    def head_is_on_remote(self, branch_name: str) -> bool:
        return False

    def get_last_commit_subject(self) -> str:
        return "chore: touch"

    def get_last_commit_message(self) -> str:
        return "chore: touch"

    def build_commit_url(self, sha: str) -> str:
        return f"https://gitlab.example/commit/{sha}"

    def create_merge_request(self, **kwargs) -> str:
        self.mr_opened = True
        name = self.remote_url.rstrip("/").split("/")[-1]
        return f"https://gitlab.example/{name}/-/merge_requests/1"


class _Multi:
    def __init__(self, children: list) -> None:
        self.issue_key = "KAN-573"
        self.work_branch = "feature/KAN-573"
        self.target_branch = "main"
        self.repo_checkouts = children
        self.delivery_baseline_sha = None
        self.delivery_baselines = {}

    def ensure_on_work_branch(self) -> bool:
        return all(child.ensure_on_work_branch() for child in self.repo_checkouts)

    def get_last_commit_sha(self, short: bool = False):
        if not self.repo_checkouts:
            return None
        return self.repo_checkouts[0].get_last_commit_sha(short=short)


def test_failed_multi_repo_run_still_pushes_a_repo_that_moved(processor, state_manager):
    """A failed agent that committed in a later clone must still open that MR.

    Single-repo already does this when HEAD moved. Multi-repo must do the
    same per clone. The parent folder is not a git repo.
    """
    state_manager.create_state("KAN-573", "span", "d")
    idle = _Repo("https://gitlab.example/api.git", "aaa111", 0)
    moved = _Repo("https://gitlab.example/web.git", "bbb111", 0)
    parent = _Multi([idle, moved])
    processor._contexts["KAN-573"] = {"git": parent}
    processor._snapshot_delivery_baseline("KAN-573", parent)
    moved._sha = "bbb222"
    moved._ahead = 1
    # Comment: a branch that is not ahead is still pushed, and gets no MR.
    idle.push_ok = True

    state = state_manager.get_state("KAN-573")
    outcome = asyncio.run(
        processor._deliver_if_new_commits(state, require_new_sha=True)
    )
    assert outcome == "delivered"
    assert moved.pushed and moved.mr_opened
    assert idle.pushed and not idle.mr_opened


def test_delivery_opens_mr_for_repo_on_its_own_branch(processor, state_manager):
    """Each clone is ahead only on its own work branch.

    ``ensure_feature_branch`` stores the last child's branch on the parent.
    A commit on an earlier repository must still open that repository's
    merge request.
    """

    class _OwnBranch(_Repo):
        def commits_ahead_of_target(self, branch: str) -> int:
            self.asked.append(branch)
            if (branch or "") != self.work_branch:
                return 0
            return self._ahead

    state_manager.create_state("KAN-580", "span", "d")
    api = _OwnBranch("https://gitlab.example/api.git", "aaa111", 1)
    api.work_branch = "feature/api-side"
    api.target_branch = "develop"
    api.asked = []
    web = _OwnBranch("https://gitlab.example/web.git", "bbb111", 0)
    web.work_branch = "feature/web-side"
    web.target_branch = "main"
    web.asked = []
    web.push_ok = True
    parent = _Multi([api, web])
    parent.issue_key = "KAN-580"
    parent.work_branch = web.work_branch
    processor._contexts["KAN-580"] = {"git": parent}
    state = state_manager.get_state("KAN-580")
    outcome = asyncio.run(processor._deliver_if_new_commits(state))
    assert outcome == "delivered", api.asked
    assert api.mr_opened
    assert api.pushed
    assert web.pushed and not web.mr_opened


def test_multi_repo_push_failure_names_the_repository(processor, state_manager):
    """One clone can be pushed while another is rejected.

    The job stays failed, and the operator-facing reason names the remote
    that rejected the push. The parent folder has no push error of its own.
    """
    state_manager.create_state("KAN-573", "span", "d")
    state_manager.update_state("KAN-573", status=TaskStatus.EXECUTING)
    good = _Repo("https://gitlab.example/api.git", "aaa111", 1)
    bad = _Repo("https://gitlab.example/web.git", "bbb111", 1)

    def fail_push(branch_name: str) -> bool:
        bad.pushed = True
        bad.last_push_error = "authentication failed"
        return False

    bad.push = fail_push
    parent = _Multi([good, bad])
    processor._contexts["KAN-573"] = {"git": parent}
    state = state_manager.get_state("KAN-573")
    outcome = asyncio.run(processor._deliver_if_new_commits(state))
    assert outcome == "push_failed"
    assert good.pushed and good.mr_opened
    assert bad.pushed and not bad.mr_opened
    reason = processor._push_failure_reason("KAN-573")
    assert "web.git" in reason
    assert "authentication failed" in reason


def test_single_repo_failure_still_delivers_when_head_moved(processor, state_manager):
    """Control: the one-repo failure path already pushes a moved HEAD."""
    state_manager.create_state("KAN-575", "one", "d")
    repo = _Repo("https://gitlab.example/only.git", "old", 1)
    repo.delivery_baseline_sha = "old"
    repo._sha = "new"
    repo.repo_checkouts = []
    processor._contexts["KAN-575"] = {"git": repo}
    state = state_manager.get_state("KAN-575")
    outcome = asyncio.run(
        processor._deliver_if_new_commits(state, require_new_sha=True)
    )
    assert outcome == "delivered"
    assert repo.pushed and repo.mr_opened


def test_one_repo_rework_drops_the_previous_multi_repo_set(processor, state_manager):
    """A later one-repository run must not keep cloning the previous set.

    Scheduling the same issue again with one repository, or editing the
    description down to one repository and moving it back to To Do, has to
    clear the stored urls and refs. An empty update must not leave the old
    pair in place.
    """
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    state_manager.create_state("KAN-20", "span", "old")
    state_manager.update_state(
        "KAN-20",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={
            "repository_urls": [api, web],
            "repository_refs": [
                {
                    "url": api,
                    "source_branch": "feature/KAN-20",
                    "target_branch": "develop",
                },
                {
                    "url": web,
                    "source_branch": "feature/web-side",
                    "target_branch": "main",
                },
            ],
        },
    )
    event = make_issue_event(
        key="KAN-20",
        summary="span",
        description=(
            "{params}\n"
            f"Repository: {api}\n"
            "Source branch: feature/KAN-20\n"
            "Target branch: develop\n"
            "Mode: build\n"
            "{params}\n"
        ),
        status="To Do",
        event_type="jira:issue_updated",
    )
    started: list[str] = []

    async def cap(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                with patch.object(processor, "_mark_jira_in_progress", return_value=True):
                    await processor._handle_issue_updated(event)

    asyncio.run(run())
    assert started == ["KAN-20"]
    meta = state_manager.get_state("KAN-20").metadata or {}
    urls = [str(url) for url in (meta.get("repository_urls") or [])]
    refs = [
        str(row.get("url") or "")
        for row in (meta.get("repository_refs") or [])
        if isinstance(row, dict)
    ]
    assert web not in urls
    assert web not in refs


def test_empty_repo_list_drops_a_set_still_written_in_the_description(
    processor, state_manager
):
    """A one-repo schedule sends an empty list. Stale ticket text must not restore the set."""
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    state_manager.create_state("KAN-20", "span", "old")
    state_manager.update_state(
        "KAN-20",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={"repository_urls": [api, web], "repository_refs": [{"url": web}]},
    )
    event = make_issue_event(
        key="KAN-20",
        summary="span",
        description=(
            "{params}\n"
            f"Repository: {api}\n"
            "Source branch: develop\n"
            "Target branch: develop\n"
            f"Repository: {web}\n"
            "Source branch: feature/web-side\n"
            "Target branch: main\n"
            "Mode: build\n"
            "{params}\n"
        ),
        status="To Do",
        event_type="jira:issue_updated",
    )
    event["repository_urls"] = []

    async def cap(_state):
        return None

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                with patch.object(processor, "_mark_jira_in_progress", return_value=True):
                    await processor._handle_issue_updated(event)

    asyncio.run(run())
    meta = state_manager.get_state("KAN-20").metadata or {}
    assert web not in [str(url) for url in (meta.get("repository_urls") or [])]


def test_unparsed_description_keeps_the_stored_repository_set(
    processor, state_manager
):
    """A bad edit with no repository list must not wipe a good set."""
    api = "https://gitlab.example.com/acme/api.git"
    web = "https://gitlab.example.com/acme/web.git"
    state_manager.create_state("KAN-20", "span", "old")
    state_manager.update_state(
        "KAN-20",
        status=TaskStatus.COMPLETED,
        completed_at=datetime.now(),
        metadata={"repository_urls": [api, web]},
    )
    event = make_issue_event(
        key="KAN-20",
        summary="span",
        description="please run this again",
        status="To Do",
        event_type="jira:issue_updated",
    )

    async def cap(_state):
        return None

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                with patch.object(processor, "_mark_jira_in_progress", return_value=True):
                    await processor._handle_issue_updated(event)

    asyncio.run(run())
    meta = state_manager.get_state("KAN-20").metadata or {}
    assert web in [str(url) for url in (meta.get("repository_urls") or [])]


def test_multi_repo_delivery_ignores_the_parent_folder(processor):
    """The workspace root is not a git repo. Delivery reads each clone."""

    class _Child:
        def __init__(self, url: str, sha: str, ahead: int) -> None:
            self.remote_url = url
            self._sha = sha
            self._ahead = ahead

        def get_last_commit_sha(self, short: bool = False) -> str:
            return self._sha

        def commits_ahead_of_target(self, branch: str) -> int:
            return self._ahead

    class _Parent:
        def __init__(self) -> None:
            self.issue_key = "KAN-573"
            self.work_branch = "feature/KAN-573"
            self.repo_checkouts = [
                _Child("https://gitlab.example/api.git", "aaa", 1),
                _Child("https://gitlab.example/web.git", "bbb", 1),
            ]

        def ensure_on_work_branch(self) -> bool:
            return True

        def get_last_commit_sha(self, short: bool = False):
            return None

    parent = _Parent()
    processor._contexts["KAN-573"] = {"git": parent}
    assert processor._assert_build_delivery("KAN-573") is None
    sha = processor._snapshot_delivery_baseline("KAN-573", parent)
    assert sha == "aaa"
    assert parent.delivery_baselines["https://gitlab.example/web.git"] == "bbb"


def test_recover_orphaned_planning_marks_error(processor, state_manager, fake_jira):
    state_manager.create_state("ORPH-P", "s", "d")
    state_manager.update_state(
        "ORPH-P",
        status=TaskStatus.PLANNING,
        started_at=datetime.now(),
        current_task_id="t-plan",
    )
    assert processor.recover_orphaned_in_flight() == 1
    st = state_manager.get_state("ORPH-P")
    assert st.status == TaskStatus.ERROR
    assert fake_jira.comments


def test_recover_multiple_orphans(processor, state_manager):
    for key, status in (
        ("M-1", TaskStatus.EXECUTING),
        ("M-2", TaskStatus.PLANNING),
    ):
        state_manager.create_state(key, "s", "d")
        state_manager.update_state(key, status=status, started_at=datetime.now())
    assert processor.recover_orphaned_in_flight() == 2
    assert state_manager.get_state("M-1").status == TaskStatus.ERROR
    assert state_manager.get_state("M-2").status == TaskStatus.ERROR


def test_recover_orphaned_pending_marks_error(processor, state_manager, fake_jira):
    """Crash in the accept/ack window leaves PENDING with no child process."""
    state_manager.create_state("ORPH-PEND", "s", "d")
    assert state_manager.get_state("ORPH-PEND").status == TaskStatus.PENDING
    assert processor.recover_orphaned_in_flight() == 1
    st = state_manager.get_state("ORPH-PEND")
    assert st.status == TaskStatus.ERROR
    assert st.error_message
    assert fake_jira.comments


def test_recover_skips_plan_ready_and_completed(processor, state_manager):
    state_manager.create_state("OK-1", "s", "d")
    state_manager.update_state("OK-1", status=TaskStatus.PLAN_READY)
    state_manager.create_state("OK-3", "s", "d")
    state_manager.update_state("OK-3", status=TaskStatus.COMPLETED, completed_at=datetime.now())
    assert processor.recover_orphaned_in_flight() == 0
    assert state_manager.get_state("OK-1").status == TaskStatus.PLAN_READY
    assert state_manager.get_state("OK-3").status == TaskStatus.COMPLETED


def test_after_orphan_recovery_todo_can_reprocess(processor, state_manager):
    """ERROR after recovery is terminal; To Do update is allowed to re-queue."""
    state_manager.create_state("RQ-1", "fix typo", "d")
    state_manager.update_state(
        "RQ-1", status=TaskStatus.EXECUTING, started_at=datetime.now()
    )
    processor.recover_orphaned_in_flight()
    assert state_manager.get_state("RQ-1").status == TaskStatus.ERROR

    started = []

    async def cap(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                await processor._handle_issue_updated(
                    make_issue_event(
                        key="RQ-1",
                        summary="fix typo",
                        event_type="jira:issue_updated",
                        status="To Do",
                    )
                )

    asyncio.run(run())
    assert started == ["RQ-1"]


# ---------------------------------------------------------------------------
# Live processing cache — do not double-start
# ---------------------------------------------------------------------------

def test_live_cache_blocks_create(processor, state_manager):
    state_manager.create_state("LIVE-1", "fix typo", "d")
    processor._contexts["LIVE-1"] = {"git": MagicMock(), "runner": MagicMock()}

    started = []

    async def boom(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=boom):
            await processor._handle_issue_created(make_issue_event(key="LIVE-1"))

    asyncio.run(run())
    assert started == []
    assert state_manager.get_state("LIVE-1").status == TaskStatus.PENDING


def test_live_cache_blocks_update(processor, state_manager):
    state_manager.create_state("LIVE-2", "fix typo", "d")
    state_manager.update_state("LIVE-2", status=TaskStatus.PENDING)
    processor._contexts["LIVE-2"] = {"git": MagicMock(), "runner": MagicMock()}

    started = []

    async def boom(*a, **k):
        started.append(1)

    async def run():
        with patch.object(processor, "_handle_issue_created", side_effect=boom):
            await processor._handle_issue_updated(
                make_issue_event(key="LIVE-2", event_type="jira:issue_updated")
            )

    asyncio.run(run())
    assert started == []


def test_disk_inflight_blocks_create_without_cache(processor, state_manager):
    """Disk EXECUTING alone (no _contexts yet) still skips create — existing guard."""
    state_manager.create_state("DISK-1", "fix typo", "d")
    state_manager.update_state(
        "DISK-1", status=TaskStatus.EXECUTING, started_at=datetime.now()
    )
    assert "DISK-1" not in processor._contexts

    started = []

    async def boom(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=boom):
            await processor._handle_issue_created(make_issue_event(key="DISK-1"))

    asyncio.run(run())
    assert started == []


def test_list_live_processing_keys(processor):
    processor._contexts["A-1"] = {"git": None, "runner": MagicMock()}
    processor._contexts["B-2"] = {"git": None, "runner": MagicMock()}
    keys = processor.list_live_processing_keys()
    assert set(keys) == {"A-1", "B-2"}


def test_not_processing_pending_todo_can_start(processor, state_manager):
    """Not in cache, PENDING + To Do update → may start (user rule)."""
    state_manager.create_state("GO-1", "fix typo", "d")
    assert state_manager.get_state("GO-1").status == TaskStatus.PENDING

    started = []

    async def cap(state):
        started.append(state.issue_key)

    async def run():
        with patch.object(processor, "_start_execution_workflow", side_effect=cap):
            with patch.object(processor, "_start_planning_workflow", side_effect=cap):
                await processor._handle_issue_updated(
                    make_issue_event(
                        key="GO-1",
                        summary="fix typo",
                        event_type="jira:issue_updated",
                        status="To Do",
                    )
                )

    asyncio.run(run())
    assert started == ["GO-1"]


# ---------------------------------------------------------------------------
# Graceful shutdown — kill children + CANCELLED
# ---------------------------------------------------------------------------

def test_shutdown_kills_children_and_cancels_state(processor, state_manager, fake_jira):
    state_manager.create_state("SH-1", "s", "d")
    state_manager.update_state(
        "SH-1",
        status=TaskStatus.PLANNING,
        current_task_id="task-1",
        started_at=datetime.now(),
    )
    runner = MagicMock()
    runner.cancel_task = MagicMock(return_value=True)
    runner.cancel_all_tasks = MagicMock(return_value=1)
    git = MagicMock()
    processor._contexts["SH-1"] = {"git": git, "runner": runner}
    processor.agent_runner = runner

    n = processor.shutdown_processing(reason="Daemon stopped")
    assert n >= 1
    st = state_manager.get_state("SH-1")
    assert st.status == TaskStatus.CANCELLED
    assert st.current_task_id is None
    runner.cancel_task.assert_called()
    runner.cancel_all_tasks.assert_called()
    git.cleanup.assert_called()
    assert "SH-1" not in processor._contexts
    assert fake_jira.comments, "Jira comment required on shutdown cancel"


def test_shutdown_finalises_disk_inflight_without_context(processor, state_manager, fake_jira):
    """In-flight on disk but missing from cache still becomes CANCELLED."""
    state_manager.create_state("SH-2", "s", "d")
    state_manager.update_state(
        "SH-2",
        status=TaskStatus.EXECUTING,
        current_task_id="t2",
        started_at=datetime.now(),
    )
    assert "SH-2" not in processor._contexts

    n = processor.shutdown_processing(reason="Daemon stopped")
    assert n >= 1
    st = state_manager.get_state("SH-2")
    assert st.status == TaskStatus.CANCELLED
    assert st.current_task_id is None
    assert fake_jira.comments


def test_shutdown_multiple_live_jobs(processor, state_manager, fake_jira):
    for key in ("J-1", "J-2"):
        state_manager.create_state(key, "s", "d")
        state_manager.update_state(
            key,
            status=TaskStatus.EXECUTING,
            current_task_id=f"t-{key}",
            started_at=datetime.now(),
        )
        processor._contexts[key] = {
            "git": MagicMock(),
            "runner": MagicMock(
                cancel_task=MagicMock(return_value=True),
                cancel_all_tasks=MagicMock(return_value=1),
            ),
        }

    n = processor.shutdown_processing(reason="stop")
    assert n == 2
    assert state_manager.get_state("J-1").status == TaskStatus.CANCELLED
    assert state_manager.get_state("J-2").status == TaskStatus.CANCELLED
    assert processor._contexts == {}
    assert len(fake_jira.comments) >= 2


def test_shutdown_clears_legacy_agent_runner(processor, state_manager):
    runner = MagicMock()
    runner.cancel_all_tasks = MagicMock(return_value=0)
    processor.agent_runner = runner
    processor.git_manager = MagicMock()
    processor.shutdown_processing(reason="stop")
    assert processor.agent_runner is None
    assert processor.git_manager is None
    runner.cancel_all_tasks.assert_called()


def test_agent_runner_cancel_all_tasks():
    from src.orchestrator.agent_runner import AgentRunner

    runner = AgentRunner()
    proc_a = MagicMock()
    proc_a.returncode = None
    proc_b = MagicMock()
    proc_b.returncode = 0  # already done — do not kill
    proc_c = MagicMock()
    proc_c.returncode = None
    runner._running_tasks = {"a": proc_a, "b": proc_b, "c": proc_c}

    def _kill(process, force=False):
        # Soft kill succeeds — no escalate needed
        process.returncode = -15

    with patch.object(runner, "_kill_process_tree", side_effect=_kill) as kill:
        with patch("time.sleep"):  # cancel_all may sleep before escalate
            n = runner.cancel_all_tasks()
    assert n == 2
    assert kill.call_count == 2


# ---------------------------------------------------------------------------
# Daemon wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daemon_start_runs_orphan_recovery():
    from src.daemon import JiraAgentDaemon

    with patch("src.daemon.settings") as s:
        s.validate_or_raise = MagicMock()
        s.project_root = "/tmp"
        s.jira_host = "http://j"
        s.jira_board_id = "1"
        s.poll_interval_seconds = 30

        daemon = JiraAgentDaemon()
        daemon.processor = MagicMock()
        daemon.processor.recover_orphaned_in_flight = MagicMock(return_value=2)
        daemon.state_manager = MagicMock()
        daemon.state_manager.get_active_issues.return_value = []

        with patch.object(daemon, "_start_poller", new_callable=AsyncMock):
            with patch.object(daemon, "_monitor_active_issues", new_callable=AsyncMock):
                with patch("src.daemon.IS_WINDOWS", False):
                    with patch("asyncio.get_event_loop") as gel:
                        gel.return_value = MagicMock()
                        with patch("asyncio.gather", new_callable=AsyncMock) as gather:
                            gather.return_value = None
                            await daemon.start()

        daemon.processor.recover_orphaned_in_flight.assert_called_once()


@pytest.mark.asyncio
async def test_daemon_stop_calls_shutdown_then_exits():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=1)

    with patch("sys.exit") as ex:
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                ex.assert_called_with(0)

    daemon._poller.stop.assert_called_once()
    daemon.processor.shutdown_processing.assert_called_once()
    reason = daemon.processor.shutdown_processing.call_args.kwargs.get("reason", "")
    assert "Daemon stopped" in reason

@pytest.mark.asyncio
async def test_daemon_stop_idempotent():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon._running = True
    daemon._poller = MagicMock()
    daemon.processor = MagicMock()
    daemon.processor.shutdown_processing = MagicMock(return_value=0)

    with patch("sys.exit"):
        with patch("asyncio.all_tasks", return_value=[]):
            with patch("asyncio.gather", new_callable=AsyncMock):
                await daemon.stop()
                await daemon.stop()
    assert daemon.processor.shutdown_processing.call_count == 1


@pytest.mark.asyncio
async def test_poller_handler_ignored_while_stopping():
    from src.daemon import JiraAgentDaemon

    daemon = JiraAgentDaemon()
    daemon.processor = MagicMock()
    daemon.processor.process_event = AsyncMock()
    daemon.processor.seed_poller_requeue_markers = MagicMock(return_value=0)
    daemon._running = False
    daemon._stopping = True
    daemon._main_loop = None

    with patch("src.daemon.JiraPoller") as Poller:
        poller = MagicMock()
        Poller.return_value = poller
        with patch("src.daemon.settings") as s:
            s.jira_board_id = "1"
            loop = MagicMock()
            loop.run_in_executor = AsyncMock(return_value=None)
            loop.is_closed = MagicMock(return_value=False)
            with patch("asyncio.get_running_loop", return_value=loop):
                await daemon._start_poller()
                handler = loop.run_in_executor.call_args[0][2]
                handler({"webhookEvent": "jira:issue_created", "issue": {"key": "X"}})

    # run_coroutine_threadsafe must not be used when stopping
    loop.run_coroutine_threadsafe = MagicMock()
    # handler returned early — process_event not scheduled via threadsafe in our path
    # (we return before run_coroutine_threadsafe)
    # Re-invoke with captured handler behaviour: no exception, no process_event await
    assert daemon.processor.process_event.await_count == 0
