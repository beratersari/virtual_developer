"""Job processor for handling JIRA events."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings
from src.git_manager import (
    GitCloneError,
    GitManager,
    GitSourceBranchError,
    GitTargetBranchError,
)
from src.issue_git_spec import (
    IssueGitConfigError,
    parse_issue_mode,
    require_issue_git_spec,
)
from src.jira.client import JiraClient, create_jira_client
from src.logger import logger
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.reporter.jira_reporter import JiraReporter
from src.state.job_store import JobStore, job_store
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, RetryAttempt, TaskStatus


class _JobSlotLimiter:
    """Async concurrency limiter that supports live resize without over-admit.

    Unlike replacing ``asyncio.Semaphore``, shrinking the limit only blocks
    *new* acquires; in-flight holders are tracked and must release.
    """

    def __init__(self, limit: int, *, active: int = 0) -> None:
        self._limit = max(1, int(limit))
        self._active = max(0, int(active))
        self._cond = asyncio.Condition()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def resize(self, limit: int) -> None:
        """Update the admit cap and wake waiters (so raising limit unblocks them)."""
        self._limit = max(1, int(limit))
        self._schedule_wake()

    def _schedule_wake(self) -> None:
        """Notify condition waiters on the owning event loop."""

        async def _wake() -> None:
            async with self._cond:
                self._cond.notify_all()

        try:
            running = asyncio.get_running_loop()
            self._loop = running
            running.create_task(_wake())
            return
        except RuntimeError:
            pass
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_wake(), loop)

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    async def __aenter__(self) -> "_JobSlotLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.release()

    async def acquire(self) -> None:
        self._loop = asyncio.get_running_loop()
        async with self._cond:
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


class JobProcessor:
    """Processes JIRA events and manages agent workflows."""

    def __init__(self):
        # Legacy single-slot fields (tests still set these); prefer _contexts.
        self.git_manager: Optional[GitManager] = None
        self.state_manager = JiraStateManager()
        self.reporter = JiraReporter()
        self.agent_runner: Optional[AgentRunner] = None
        # Per-issue isolation: concurrent jobs must not share git/agent slots
        self._contexts: Dict[str, Dict[str, Any]] = {}
        self._job_semaphore: Optional[asyncio.Semaphore] = None
        self.job_store: JobStore = job_store
        # issue_key -> active job_id for this process
        self._active_jobs: Dict[str, str] = {}
        # Per-issue locks prevent double-start races under concurrent events
        self._issue_locks: Dict[str, asyncio.Lock] = {}
        
        logger.info("Initializing JobProcessor")
        
        # Use simulated client if JIRA not properly configured
        use_simulated = not settings.is_configured() or settings.jira_host in ['', 'a', 'https://yourcompany.atlassian.net']
        if use_simulated:
            logger.info("Using simulated JIRA client")
        else:
            logger.info("Using real JIRA client")
        
        self.jira_client = create_jira_client(simulated=use_simulated)
        logger.debug(f"JobProcessor initialized - default_agent: {settings.default_agent}, "
                     f"planning_agent: {settings.planning_agent}, orchestrator_agent: {settings.orchestrator_agent}")
    
    # Statuses where an agent is actively running — never restart these from updates
    IN_FLIGHT_STATUSES = {
        TaskStatus.PLANNING,
        TaskStatus.EXECUTING,
    }
    TERMINAL_STATUSES = {
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
        TaskStatus.CANCELLED,
    }
    # Statuses that must not be overwritten by a late agent success path
    ABORTED_STATUSES = {
        TaskStatus.ERROR,
        TaskStatus.CANCELLED,
    }

    def _resolve_workflow(
        self,
        issue_key: str,
        summary: str,
        description: str,
    ) -> WorkflowType:
        """Pick plan vs build vs oracle from the issue text.

        * Explicit ``Mode: plan|build`` selects the workflow.
        * Otherwise ``WorkflowRouter.route_issue`` (keyword / oracle heuristics).
        * Template validity (Repository, Source, Target, **Mode**) is **not**
          checked here — same path as always: ``require_issue_git_spec`` inside
          ``_prepare_git_workspace`` after a job is opened.
        """
        mode = parse_issue_mode(summary, description)
        if mode == "plan":
            return WorkflowType.PLANNING
        if mode == "build":
            return WorkflowType.EXECUTION
        return WorkflowRouter.route_issue(issue_key, summary, description)

    def _mark_jira_in_progress(self, issue_key: str) -> None:
        """Move the Jira issue to an In Progress-like status when work starts.

        Soft-fails: a missing transition must not block agent work. Also updates
        the poller status tracker so leave→return To Do still requeues.
        (Poller may also transition when dispatching; both paths are safe.)
        """
        try:
            client = self.jira_client
            if client is None:
                return
            if hasattr(client, "transition_to_in_progress"):
                ok = client.transition_to_in_progress(issue_key)
                if ok:
                    logger.info(f"{issue_key} transitioned to In Progress on Jira")
                    poller = getattr(self, "_poller", None)
                    if poller is not None and hasattr(poller, "_last_jira_status"):
                        poller._last_jira_status[issue_key] = "in progress"
                else:
                    logger.warning(
                        f"{issue_key}: could not transition to In Progress "
                        f"(no matching transition or already in progress)"
                    )
        except Exception as e:
            logger.warning(f"{issue_key}: In Progress transition failed: {e}")

    def _ensure_job_for_failure(self, issue_key: str) -> None:
        """Create a job row if validation failed before ``_begin_workflow_run``.

        Config errors (missing Mode / {params}) never start a workflow, so they
        used to leave no Jobs-tab entry while Jira still got an error comment.
        """
        if self._active_jobs.get(issue_key):
            return
        st = self.state_manager.get_state(issue_key)
        if st is None:
            return
        cur = (st.metadata or {}).get("current_job_id")
        if cur:
            existing = self.job_store.get_job(cur)
            if existing and (existing.get("status") or "") not in {
                "completed",
                "error",
                "cancelled",
                "superseded",
                "plan_ready",
            }:
                self._active_jobs[issue_key] = cur
                return
        wf = (st.metadata or {}).get("workflow_type") or "unknown"
        self._start_job_record(
            st,
            workflow_type=str(wf),
            agent="(validation)",
            task_id=None,
            status="running",
        )

    def _fail_issue(
        self,
        issue_key: str,
        error_message: str,
        *,
        suggestion: Optional[str] = None,
    ) -> None:
        """Mark issue ERROR, finish job record, allow re-queue, notify Jira."""
        error_text = (error_message or "Unknown error")[:2000]
        try:
            # Always surface a job on the dashboard (incl. template/Mode failures)
            self._ensure_job_for_failure(issue_key)
            meta_patch = self._archive_run_identifiers(issue_key)
            meta_patch["requeue_eligible"] = True
            # Fingerprint current text so poller only reprocesses when user edits
            # the description (e.g. adds Mode) while staying on To Do.
            st0 = self.state_manager.get_state(issue_key)
            if st0 is not None:
                raw = f"{st0.issue_summary or ''}\n{st0.description or ''}"
                import hashlib

                meta_patch["last_intake_fingerprint"] = hashlib.sha256(
                    raw.encode("utf-8", errors="replace")
                ).hexdigest()[:20]
            updated = self.state_manager.update_state(
                issue_key,
                status=TaskStatus.ERROR,
                error_message=error_text,
                completed_at=datetime.now(),
                current_task_id=None,
                metadata=meta_patch,
            )
            self._finish_job_record(
                issue_key, status="error", error_message=error_text, progress_percentage=0
            )
            # Poller: do not force auto-requeue while Jira stayed To Do.
            # Keep last observed status; requeue_eligible gates leave→return.
            self._nudge_poller_after_terminal(issue_key, marker="__terminal_local__")
            state = updated or self.state_manager.get_state(issue_key)
            if state is None:
                self.reporter.post_comment_response(
                    issue_key,
                    f"An error occurred while processing this issue:\n\n{{code}}\n{error_text}\n{{code}}",
                )
                return
            comment_id = self.reporter.post_error(
                state, error_text, suggestion=suggestion
            )
            if not comment_id:
                logger.error(f"Jira post_error returned no comment for {issue_key}")
        except Exception as e:
            logger.exception(f"Failed to report error for {issue_key}: {e}", e)

    def _archive_run_identifiers(
        self,
        issue_key: str,
        *,
        task_id: Optional[str] = None,
        opencode_session_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build metadata patch that appends ids to history (never replace history).

        Issue-level ``current_*`` fields are single-slot; multi-run history lives
        in ``metadata.task_ids``, ``opencode_session_ids``, and ``job_ids``.
        """
        prior = self.state_manager.get_state(issue_key)
        meta = dict((prior.metadata if prior else None) or {})
        patch: Dict[str, Any] = {}

        tid = task_id
        if tid is None and prior is not None:
            tid = prior.current_task_id
        if tid:
            task_ids = list(meta.get("task_ids") or [])
            if tid not in task_ids:
                task_ids.append(tid)
            patch["task_ids"] = task_ids[-100:]
            patch["last_task_id"] = tid

        sid = opencode_session_id
        if sid is None and prior is not None:
            sid = prior.current_opencode_session_id
        if sid:
            history = list(meta.get("opencode_session_ids") or [])
            if sid not in history:
                history.append(sid)
            patch["opencode_session_ids"] = history[-100:]
            patch["last_opencode_session_id"] = sid

        jid = job_id
        if jid is None:
            jid = meta.get("current_job_id")
        if jid:
            job_ids = list(meta.get("job_ids") or [])
            if jid not in job_ids:
                job_ids.append(jid)
            patch["job_ids"] = job_ids[-200:]

        return patch

    def _reset_for_reprocess(self, issue_key: str) -> None:
        """Clear runtime fields before restarting work on an issue.

        Archives previous task/session/job ids into metadata history so a new
        run never erases identifiers of earlier jobs.
        """
        # Close any still-open job record for this issue
        self._finish_job_record(
            issue_key,
            status="superseded",
            error_message="Superseded by reprocess",
        )
        meta_patch = self._archive_run_identifiers(issue_key)
        meta_patch["requeue_eligible"] = False
        # Drop live pointer only; history keys stay in meta_patch
        meta_patch["current_job_id"] = None
        self.state_manager.update_state(
            issue_key,
            status=TaskStatus.PENDING,
            progress_percentage=0,
            error_message=None,
            current_task_id=None,
            current_opencode_session_id=None,
            timed_out=False,
            completed_at=None,
            metadata=meta_patch,
        )

    def _clear_requeue_flag(self, issue_key: str) -> None:
        """Clear poller re-queue eligibility when work actually starts."""
        self.state_manager.update_state(
            issue_key,
            metadata={"requeue_eligible": False},
        )

    def _runner_for(self, issue_key: str) -> Optional[AgentRunner]:
        """Return the agent runner bound to this issue (isolation-safe).

        Never falls back to another issue's runner under multi-job concurrency.
        Legacy ``self.agent_runner`` is used only when no other contexts exist.
        """
        ctx = self._contexts.get(issue_key)
        if ctx and ctx.get("runner") is not None:
            return ctx["runner"]
        # Legacy single-slot: only when no foreign live contexts
        if self.agent_runner is not None and (
            not self._contexts or set(self._contexts.keys()) <= {issue_key}
        ):
            return self.agent_runner
        return None

    def _git_for(self, issue_key: str) -> Optional[GitManager]:
        """Return the git manager bound to this issue (isolation-safe).

        Never returns another issue's git manager when ``ctx["git"]`` is None
        (oracle/sandbox paths) or missing under multi-job concurrency.
        """
        ctx = self._contexts.get(issue_key)
        if ctx is not None:
            # Explicit None means sandbox/oracle — do not fall back to foreign git
            return ctx.get("git")
        if self.git_manager is not None and (
            not self._contexts or set(self._contexts.keys()) <= {issue_key}
        ):
            gm = self.git_manager
            gm_key = getattr(gm, "issue_key", None)
            # Real GitManager: only if unbound or same issue. Tests often use
            # MagicMock without a string issue_key — treat as single-slot.
            if gm_key is None or gm_key == issue_key or not isinstance(gm_key, str):
                return gm
        return None

    def _is_aborted(self, issue_key: str) -> bool:
        """True if issue was cancelled/errored while work was still running."""
        state = self.state_manager.get_state(issue_key)
        return bool(state and state.status in self.ABORTED_STATUSES)

    def _record_agent_retry(
        self,
        issue_key: str,
        *,
        attempt_number: int,
        delay_seconds: float,
        reason: str,
        session_file: Optional[str] = None,
        error_message: Optional[str] = None,
        return_code: Optional[int] = None,
        session_id: Optional[str] = None,
        new_task_id: Optional[str] = None,
        progress_percentage: int = 0,
    ) -> None:
        """Record a retry attempt without overwriting CANCELLED/ERROR status.

        Uses locked ``record_retry_attempt`` (re-read under RLock) so a concurrent
        cancel/watchdog cannot be clobbered by a stale full ``set_state``.
        """
        if self._is_aborted(issue_key):
            logger.info(f"Skipping retry bookkeeping for aborted {issue_key}")
            return

        retry_attempt = RetryAttempt(
            attempt_number=attempt_number,
            timestamp=datetime.now(),
            reason=reason,
            delay_seconds=delay_seconds,
            session_log_path=session_file,
            error_message=error_message,
            return_code=return_code,
            opencode_session_id=session_id,
        )
        updated = self.state_manager.record_retry_attempt(
            issue_key,
            retry_attempt,
            abort_statuses=self.ABORTED_STATUSES,
            current_task_id=new_task_id,
            current_opencode_session_id=session_id,
        )
        if updated is None or updated.status in self.ABORTED_STATUSES:
            return

        if session_id:
            self._record_opencode_session(
                issue_key, session_id, session_file=session_file
            )
        jid = self._active_jobs.get(issue_key)
        if jid and (new_task_id or session_id or session_file):
            patch: Dict[str, Any] = {}
            if new_task_id:
                patch["task_id"] = new_task_id
            if session_id:
                patch["opencode_session_id"] = session_id
            if session_file:
                patch["session_log_path"] = session_file
            try:
                self.job_store.update_job(jid, **patch)
            except Exception:
                pass

        self.reporter.post_progress_update(
            updated,
            f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
            progress_percentage=progress_percentage,
        )

    def _release_context(self, issue_key: str, *, success: Optional[bool] = None) -> None:
        """Drop per-issue context; optionally cleanup temp dir."""
        ctx = self._contexts.pop(issue_key, None)
        git = ctx.get("git") if ctx else None
        if git is not None:
            try:
                git.cleanup(success=success)
            except Exception as e:
                logger.warning(f"Cleanup failed for {issue_key}: {e}")

    def _is_live_processing(self, issue_key: str) -> bool:
        """True when this process holds an in-memory processing slot for the issue."""
        return issue_key in self._contexts

    def list_live_processing_keys(self) -> list[str]:
        """Issue keys currently held in the in-memory processing cache."""
        return list(self._contexts.keys())

    def _get_issue_lock(self, issue_key: str) -> asyncio.Lock:
        """Return (and create) a per-issue asyncio lock."""
        lock = self._issue_locks.get(issue_key)
        if lock is None:
            lock = asyncio.Lock()
            self._issue_locks[issue_key] = lock
        return lock

    def resize_job_semaphore(self, new_limit: int) -> None:
        """Update concurrency cap without over-admitting mid-flight jobs.

        Uses an adaptive limiter so shrinking the limit only affects *new*
        admits; in-flight jobs keep their slots until they finish.
        """
        limit = max(1, int(new_limit or 1))
        settings.max_concurrent_jobs = limit
        if isinstance(self._job_semaphore, _JobSlotLimiter):
            self._job_semaphore.resize(limit)
        else:
            # First resize or cold path — install adaptive limiter
            active = len(self._contexts)
            self._job_semaphore = _JobSlotLimiter(limit, active=active)
        logger.info(f"Job concurrency limit set to {limit}")

    def _nudge_poller_after_terminal(
        self, issue_key: str, *, marker: str = "__cancelled__"
    ) -> None:
        """Update poller status tracker after local cancel/error.

        - If Jira was already To Do-like: keep/set ``to do`` so cancel does
          **not** auto-requeue on the next poll.
        - If Jira was non-todo (e.g. ``in progress``): **leave that value**
          so a later real return to To Do is detected via
          ``entered_todo_from_elsewhere`` / ``force_after_in_progress``.
        - Never use synthetic markers that look like “left To Do” while the
          board column never moved.
        """
        try:
            poller = getattr(self, "_poller", None)
            if poller is None or not hasattr(poller, "_last_jira_status"):
                return
            prev = (poller._last_jira_status.get(issue_key) or "").strip().lower()
            todo_names = {
                "to do",
                "todo",
                "open",
                "backlog",
                "selected for development",
                "yapılacak",
                "yapilacak",
                "yeni",
            }
            synthetic = {"__cancelled__", "__terminal_local__"}
            if prev in synthetic or prev == "" or prev in todo_names:
                # Normalize unknown/synthetic/todo to plain "to do" (no auto-requeue)
                if prev not in todo_names or prev in synthetic or prev == "":
                    poller._last_jira_status[issue_key] = "to do"
            # else: keep non-todo (e.g. "in progress") so To Do return requeues
            _ = marker  # retained for call-site compatibility
        except Exception:
            pass

    def seed_poller_requeue_markers(self) -> int:
        """After poller attaches: seed trackers for requeue-eligible terminals.

        Avoids auto-requeue while still To Do; only ensures leave→return works
        when the last known board status was non-todo.
        """
        poller = getattr(self, "_poller", None)
        if poller is None or not hasattr(poller, "_last_jira_status"):
            return 0
        n = 0
        for st in self.state_manager.get_all_states():
            if st.status not in self.TERMINAL_STATUSES:
                continue
            meta = st.metadata or {}
            if not meta.get("requeue_eligible"):
                continue
            key = st.issue_key
            prev = (poller._last_jira_status.get(key) or "").strip().lower()
            if not prev:
                # Unknown board status: assume still To Do (safe, no auto-requeue)
                poller._last_jira_status[key] = "to do"
                n += 1
        return n

    def _record_opencode_session(
        self,
        issue_key: str,
        session_id: Optional[str],
        *,
        session_file: Optional[str] = None,
    ) -> None:
        """Persist current OpenCode session id and append to history metadata."""
        if not session_id:
            return
        state = self.state_manager.get_state(issue_key)
        if not state:
            return
        history = list((state.metadata or {}).get("opencode_session_ids") or [])
        if session_id not in history:
            history.append(session_id)
        entries = list((state.metadata or {}).get("opencode_sessions") or [])
        entries.append(
            {
                "id": session_id,
                "session_file": session_file,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        # Keep last 50 history entries
        entries = entries[-50:]
        self.state_manager.update_state(
            issue_key,
            current_opencode_session_id=session_id,
            metadata={
                "opencode_session_ids": history,
                "opencode_sessions": entries,
            },
        )
        logger.info(f"{issue_key} OpenCode session: {session_id}")

    def _link_job_session_paths(
        self,
        issue_key: str,
        session_path: Optional[str] = None,
        prompt_path: Optional[str] = None,
    ) -> None:
        """Attach session/prompt paths to the active job as soon as files exist."""
        job_id = self._active_jobs.get(issue_key)
        if not job_id:
            return
        patch: Dict[str, Any] = {}
        if session_path:
            patch["session_log_path"] = session_path
        if prompt_path:
            patch["prompt_path"] = prompt_path
        if not patch:
            return
        try:
            self.job_store.update_job(job_id, **patch)
        except Exception:
            pass

    def _apply_agent_result_session(self, issue_key: str, result: Dict[str, Any]) -> None:
        """Pull session id from agent result (and retry_info) into state."""
        sid = result.get("opencode_session_id")
        if not sid and result.get("retry_info"):
            sid = result["retry_info"].get("last_opencode_session_id")
        self._record_opencode_session(
            issue_key,
            sid,
            session_file=result.get("session_file"),
        )
        job_id = self._active_jobs.get(issue_key)
        if job_id:
            patch: Dict[str, Any] = {}
            if sid:
                patch["opencode_session_id"] = sid
            if result.get("session_file"):
                patch["session_log_path"] = result.get("session_file")
                try:
                    p = Path(str(result["session_file"]))
                    prompt = p.parent / f"{p.stem}.prompt.txt"
                    if prompt.is_file():
                        patch["prompt_path"] = str(prompt)
                        # Backfill description from frozen prompt if missing
                        existing = self.job_store.get_job(job_id) or {}
                        if not (existing.get("description") or "").strip():
                            from src.state.job_store import description_from_prompt_path

                            recovered = description_from_prompt_path(str(prompt))
                            if recovered:
                                patch["description"] = recovered
                except Exception:
                    pass
            if patch:
                self.job_store.update_job(job_id, **patch)

    def _begin_workflow_run(
        self,
        state: JiraAgentState,
        *,
        status: TaskStatus,
        task: AgentTask,
        workflow_type: str,
        agent: str,
        job_status: str,
        started_at: Optional[datetime] = None,
    ) -> str:
        """Archive previous run ids, claim in-flight fields, create a new job.

        Call this instead of writing ``current_task_id`` then starting a job
        separately — archive must run **before** the previous task id is
        overwritten.
        """
        archive = self._archive_run_identifiers(state.issue_key)
        archive["requeue_eligible"] = False
        self.state_manager.update_state(
            state.issue_key,
            status=status,
            started_at=started_at or datetime.now(),
            current_task_id=task.task_id,
            current_opencode_session_id=None,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
            metadata=archive,
        )
        state.current_task_id = task.task_id
        state.current_opencode_session_id = None
        return self._start_job_record(
            state,
            workflow_type=workflow_type,
            agent=agent,
            task_id=task.task_id,
            status=job_status,
        )

    def _start_job_record(
        self,
        state: JiraAgentState,
        *,
        workflow_type: str,
        agent: str,
        task_id: Optional[str] = None,
        status: str = "running",
    ) -> str:
        """Create a **new** job history row for this run; returns job_id.

        Never reuses or overwrites a previous job file. Each run gets a unique
        ``job_*`` record with its own task_id / session_id fields.
        """
        # If a previous run left an active job pointer, finish it first
        if self._active_jobs.get(state.issue_key):
            self._finish_job_record(
                state.issue_key,
                status="superseded",
                error_message="Superseded by new job start",
            )

        job = self.job_store.create_job(
            issue_key=state.issue_key,
            summary=state.issue_summary or "",
            # Snapshot at run start — never share live issue description
            description=state.description or "",
            workflow_type=workflow_type,
            agent=agent,
            task_id=task_id,
            status=status,
        )
        job_id = job["job_id"]
        self._active_jobs[state.issue_key] = job_id

        st = self.state_manager.get_state(state.issue_key)
        meta = dict((st.metadata if st else None) or {})
        job_ids = list(meta.get("job_ids") or [])
        if job_id not in job_ids:
            job_ids.append(job_id)
        task_ids = list(meta.get("task_ids") or [])
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        patch: Dict[str, Any] = {
            "job_ids": job_ids[-200:],
            "current_job_id": job_id,
        }
        if task_id:
            patch["task_ids"] = task_ids[-100:]
            patch["last_task_id"] = task_id
        self.state_manager.update_state(state.issue_key, metadata=patch)
        logger.info(
            f"Job started: {job_id} issue={state.issue_key} "
            f"task_id={task_id} workflow={workflow_type}"
        )
        return job_id

    def _finish_job_record(
        self,
        issue_key: str,
        *,
        status: str,
        error_message: Optional[str] = None,
        progress_percentage: Optional[int] = None,
    ) -> None:
        job_id = self._active_jobs.pop(issue_key, None)
        if not job_id:
            st = self.state_manager.get_state(issue_key)
            job_id = (st.metadata or {}).get("current_job_id") if st else None
        if not job_id:
            return
        # Do not overwrite a terminal status with another terminal on the same job
        _TERMINAL_JOB = (
            "completed",
            "error",
            "cancelled",
            "superseded",
            "plan_ready",
        )
        existing = self.job_store.get_job(job_id)
        if existing and (existing.get("status") or "") in _TERMINAL_JOB:
            # Still allow progress/session fill-in if missing — never overwrite ids
            fill: Dict[str, Any] = {}
            st = self.state_manager.get_state(issue_key)
            if (
                st
                and st.current_opencode_session_id
                and not existing.get("opencode_session_id")
            ):
                fill["opencode_session_id"] = st.current_opencode_session_id
            if st and st.current_task_id and not existing.get("task_id"):
                fill["task_id"] = st.current_task_id
            if fill:
                self.job_store.update_job(job_id, **fill)
            return

        fields: Dict[str, Any] = {
            "status": status,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
        }
        if error_message is not None:
            fields["error_message"] = (error_message or "")[:2000]
        if progress_percentage is not None:
            fields["progress_percentage"] = progress_percentage
        st = self.state_manager.get_state(issue_key)
        # Prefer ids already on the job (supersede must not stamp the new run's ids)
        if st and st.current_opencode_session_id and not (
            existing and existing.get("opencode_session_id")
        ):
            fields["opencode_session_id"] = st.current_opencode_session_id
        if st and st.current_task_id and not (existing and existing.get("task_id")):
            fields["task_id"] = st.current_task_id
        self.job_store.update_job(job_id, **fields)
        # Keep job_id in history; clear live pointer when this finish is terminal
        meta = self._archive_run_identifiers(issue_key, job_id=job_id)
        if status in _TERMINAL_JOB:
            meta["current_job_id"] = None
        self.state_manager.update_state(issue_key, metadata=meta)

    async def cancel_job(
        self, issue_key: str, *, reason: str = "Cancelled from dashboard"
    ) -> dict:
        """Cancel a job: kill agent children, set CANCELLED, notify Jira.

        Runs under the per-issue asyncio lock so cancel cannot race success paths
        or double-start (B6/B7). Returns a status dict for the dashboard API.
        """
        async with self._get_issue_lock(issue_key):
            state = self.state_manager.get_state(issue_key)
            if not state:
                return {
                    "ok": False,
                    "error": "No local state for this issue",
                    "issue_key": issue_key,
                }

            if state.status in self.TERMINAL_STATUSES:
                return {
                    "ok": False,
                    "error": f"Issue is already terminal ({state.status.value})",
                    "issue_key": issue_key,
                    "status": state.status.value,
                }

            killed = False
            try:
                runner = self._runner_for(issue_key)
                if runner and state.current_task_id:
                    killed = bool(runner.cancel_task(state.current_task_id))
                if runner and hasattr(runner, "cancel_all_tasks"):
                    n = runner.cancel_all_tasks()
                    if n:
                        killed = True
            except Exception as e:
                logger.warning(f"cancel_job kill failed for {issue_key}: {e}")

            self._cancel_issue_state(
                issue_key,
                message=reason,
                status=TaskStatus.CANCELLED,
            )
            try:
                self._release_context(issue_key, success=False)
            except Exception as e:
                logger.warning(f"cancel_job context release failed for {issue_key}: {e}")

            logger.info(f"Job cancelled via API: {issue_key} killed={killed}")
            refreshed = self.state_manager.get_state(issue_key)
            return {
                "ok": True,
                "issue_key": issue_key,
                "status": refreshed.status.value if refreshed else "cancelled",
                "process_signalled": killed,
                "message": reason,
            }

    async def start_plan_execution(
        self, issue_key: str, *, reason: str = "Started from ops dashboard"
    ) -> dict:
        """Start execution for a plan_ready issue (internal / label path).

        Preferred operator path: set ``Mode: build`` and move the issue to To Do
        (board poller). Dashboard HTTP Start is disabled (410).

        Uses the same job semaphore + per-issue lock as ``process_event`` (B7).
        """
        if self._job_semaphore is None:
            limit = max(1, int(settings.max_concurrent_jobs or 1))
            self._job_semaphore = _JobSlotLimiter(limit)

        async with self._job_semaphore:
            async with self._get_issue_lock(issue_key):
                state = self.state_manager.get_state(issue_key)
                if not state:
                    return {
                        "ok": False,
                        "error": "No local state for this issue",
                        "issue_key": issue_key,
                    }
                if state.status != TaskStatus.PLAN_READY:
                    return {
                        "ok": False,
                        "error": f"Issue is not plan_ready (status={state.status.value})",
                        "issue_key": issue_key,
                        "status": state.status.value,
                    }
                if self._is_live_processing(issue_key):
                    return {
                        "ok": False,
                        "error": "Issue is already being processed",
                        "issue_key": issue_key,
                        "status": state.status.value,
                    }
                try:
                    logger.info(f"{issue_key}: starting plan execution ({reason})")
                    await self._start_execution_workflow(state)
                    refreshed = self.state_manager.get_state(issue_key)
                    return {
                        "ok": True,
                        "issue_key": issue_key,
                        "status": refreshed.status.value if refreshed else "executing",
                        "message": reason,
                    }
                except Exception as e:
                    logger.exception(
                        f"start_plan_execution failed for {issue_key}: {e}", e
                    )
                    self._fail_issue(
                        issue_key,
                        f"Failed to start plan execution: {e}",
                        suggestion="Check logs, then retry Start from the dashboard.",
                    )
                    self._release_context(issue_key, success=False)
                    return {
                        "ok": False,
                        "error": str(e),
                        "issue_key": issue_key,
                    }

    def _kill_children_for_issue(self, issue_key: str) -> None:
        """Best-effort kill of agent subprocesses for one issue."""
        state = self.state_manager.get_state(issue_key)
        runner = self._runner_for(issue_key)
        if not runner:
            return
        try:
            if state and state.current_task_id:
                runner.cancel_task(state.current_task_id)
            if hasattr(runner, "cancel_all_tasks"):
                runner.cancel_all_tasks()
        except Exception as e:
            logger.warning(f"Could not kill agent processes for {issue_key}: {e}")

    def _cancel_issue_state(
        self,
        issue_key: str,
        *,
        message: str,
        status: TaskStatus = TaskStatus.CANCELLED,
    ) -> None:
        """Write a terminal status and notify Jira; clear live task id only.

        Preserves OpenCode session id and records last_task_id in metadata so
        the dashboard can still show identifiers after cancel.
        """
        text = (message or "Work interrupted")[:2000]
        try:
            prior = self.state_manager.get_state(issue_key)
            meta_patch = self._archive_run_identifiers(issue_key)
            # Allow poller to re-queue when the user returns the issue to To Do
            if status in (TaskStatus.CANCELLED, TaskStatus.ERROR):
                meta_patch["requeue_eligible"] = True

            update_kwargs: Dict[str, Any] = {
                "status": status,
                "error_message": text,
                "completed_at": datetime.now(),
                # Live agent slot is free; ids kept in metadata.task_ids / last_task_id
                "current_task_id": None,
                "metadata": meta_patch,
            }
            # Intentionally do NOT clear current_opencode_session_id

            updated = self.state_manager.update_state(issue_key, **update_kwargs)
            self._finish_job_record(
                issue_key,
                status=status.value,
                error_message=text,
            )

            self._nudge_poller_after_terminal(issue_key, marker="__cancelled__")

            state = updated or self.state_manager.get_state(issue_key)
            if state is None:
                self.reporter.post_comment_response(
                    issue_key,
                    f"Work interrupted:\n\n{{code}}\n{text}\n{{code}}",
                )
                return
            if status == TaskStatus.ERROR:
                self.reporter.post_error(
                    state,
                    text,
                    suggestion=(
                        "Move the issue back to To Do to re-queue after the daemon is running."
                    ),
                )
            else:
                self.reporter.post_comment_response(
                    issue_key,
                    (
                        f"*Work interrupted* (`{status.value}`)\n\n"
                        f"{text}\n\n"
                        "Move the issue back to *To Do* to re-queue when ready."
                    ),
                )
        except Exception as e:
            logger.exception(f"Failed to finalise interrupted state for {issue_key}: {e}", e)

    def recover_orphaned_in_flight(self) -> int:
        """On cold start: disk PLANNING/EXECUTING cannot be live — finalise them.

        In-memory cache is empty after a process restart, so any leftover
        in-flight status is orphaned (no child process). Mark ERROR so poller
        can re-queue from To Do, and Jira users are not left with a silent hang.
        """
        recovered = 0
        for state in self.state_manager.get_active_issues():
            if state.status not in self.IN_FLIGHT_STATUSES:
                continue
            logger.warning(
                f"Orphaned in-flight state for {state.issue_key} "
                f"({state.status.value}); recovering to ERROR"
            )
            self._cancel_issue_state(
                state.issue_key,
                message=(
                    f"Daemon started with leftover status '{state.status.value}' "
                    "but no live agent process. The previous run was interrupted "
                    "or crashed. Marking as error so work can be re-queued from To Do."
                ),
                status=TaskStatus.ERROR,
            )
            recovered += 1
        if recovered:
            logger.info(f"Recovered {recovered} orphaned in-flight issue(s) on startup")
        return recovered

    def shutdown_processing(self, *, reason: str = "Daemon stopped") -> int:
        """Stop all child agents and finalise every live/in-flight job.

        Uses the in-memory processing cache (``_contexts``) plus disk in-flight
        statuses so nothing is left as planning/executing after a clean quit.
        """
        logger.info(f"Shutting down processing: {reason}")
        keys: set[str] = set(self._contexts.keys())
        for state in self.state_manager.get_active_issues():
            if state.status in self.IN_FLIGHT_STATUSES:
                keys.add(state.issue_key)

        # Also kill legacy single-slot runner if present
        if self.agent_runner is not None and hasattr(self.agent_runner, "cancel_all_tasks"):
            try:
                self.agent_runner.cancel_all_tasks()
            except Exception as e:
                logger.warning(f"Legacy agent_runner cancel_all failed: {e}")

        finalised = 0
        for issue_key in list(keys):
            try:
                self._kill_children_for_issue(issue_key)
                state = self.state_manager.get_state(issue_key)
                if state and state.status in self.IN_FLIGHT_STATUSES:
                    self._cancel_issue_state(
                        issue_key,
                        message=(
                            f"{reason}. Agent process was stopped; local status is "
                            f"no longer '{state.status.value}'."
                        ),
                        status=TaskStatus.CANCELLED,
                    )
                    finalised += 1
                elif issue_key in self._contexts:
                    # Context without in-flight status — still drop context/children
                    finalised += 1
            finally:
                try:
                    self._release_context(issue_key, success=False)
                except Exception as e:
                    logger.warning(f"Context release failed for {issue_key}: {e}")

        self.git_manager = None
        self.agent_runner = None
        logger.info(f"Shutdown processing complete: {finalised} issue(s) finalised")
        return finalised

    async def process_event(self, event: Dict[str, Any]):
        """Process a JIRA poll (or CLI) event.

        Events use a ``webhookEvent`` key for historical compatibility with
        the poller envelope (``jira:issue_created`` / ``jira:issue_updated``).
        HTTP webhooks are not supported.
        """
        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")
        
        logger.info(f"Processing event: {event_type} for issue: {issue_key}")
        logger.debug(f"Event data keys: {list(event.keys())}")

        if self._job_semaphore is None:
            limit = max(1, int(settings.max_concurrent_jobs or 1))
            self._job_semaphore = _JobSlotLimiter(limit)
        
        try:
            async with self._job_semaphore:
                async with self._get_issue_lock(issue_key):
                    # Accept both on-prem/cloud comment event names
                    if event_type == "jira:issue_created":
                        logger.info(f"Handling issue created event for {issue_key}")
                        await self._handle_issue_created(event)
                    elif event_type == "jira:issue_updated":
                        logger.info(f"Handling issue updated event for {issue_key}")
                        await self._handle_issue_updated(event)
                    elif event_type in ("comment_created", "jira:issue_commented"):
                        logger.info(f"Handling comment created event for {issue_key}")
                        await self._handle_comment_created(event)
                    else:
                        logger.debug(f"Unknown event type: {event_type}, ignoring")
        except Exception as e:
            logger.exception(f"Unhandled error processing event for {issue_key}: {e}", e)
            if issue_key and issue_key != "unknown":
                self._fail_issue(
                    issue_key,
                    f"Unhandled error while processing event: {e}",
                    suggestion="Check daemon logs and re-queue the issue if needed.",
                )
                self._release_context(issue_key, success=False)
    
    async def _handle_issue_created(self, event: Dict[str, Any]):
        issue = event.get("issue", {})
        issue_key = issue.get("key", "unknown")
        fields = issue.get("fields", {})
        
        summary = fields.get("summary", "")
        description = fields.get("description", "") or ""
        if not isinstance(description, str):
            # On-prem is usually plain text; tolerate accidental non-string payloads
            description = str(description)
        
        logger.info(f"Handling issue created for {issue_key}: {summary[:80]}")
        logger.debug(f"Issue description length: {len(description)} chars")
        
        # Live in-memory cache wins over disk — never double-start a held job
        if self._is_live_processing(issue_key):
            logger.info(f"Issue {issue_key} already live in processing cache, skipping")
            return

        existing = self.state_manager.get_state(issue_key)
        if existing and existing.status in self.IN_FLIGHT_STATUSES:
            logger.info(f"Issue {issue_key} already in progress (status: {existing.status.value}), skipping")
            return
        # PLAN_READY: do not re-plan from a create event. Execution starts when
        # the operator sets Mode: build and moves the issue to To Do (issue_updated).
        if existing and existing.status == TaskStatus.PLAN_READY:
            logger.info(
                f"Issue {issue_key} has plan ready; "
                f"set Mode: build and move to To Do to execute (skip re-create)"
            )
            return
        
        if existing:
            logger.info(f"Found existing state for {issue_key} with status: {existing.status.value}")
            if existing.status in self.TERMINAL_STATUSES:
                logger.info(f"Resetting {issue_key} from {existing.status.value} to PENDING for reprocessing")
                self._reset_for_reprocess(issue_key)
                refreshed = self.state_manager.get_state(issue_key)
                if refreshed:
                    existing = refreshed
            state = existing
            # Prefer fresh summary/description from the event when present
            if summary:
                state.issue_summary = summary
            if description:
                state.description = description
            workflow_type = self._resolve_workflow(
                issue_key, state.issue_summary, state.description
            )
            self.state_manager.update_state(
                issue_key,
                issue_summary=state.issue_summary,
                description=state.description,
                metadata={"workflow_type": workflow_type.value},
            )
            state = self.state_manager.get_state(issue_key) or state
            logger.info(
                f"Determined workflow type: {workflow_type.value} for existing issue {issue_key}"
            )
        else:
            assignee_data = fields.get("assignee")
            assignee = assignee_data.get("displayName") if assignee_data else None

            workflow_type = self._resolve_workflow(issue_key, summary, description)
            logger.info(
                f"Determined workflow type: {workflow_type.value} for new issue {issue_key}"
            )

            state = self.state_manager.create_state(
                issue_key=issue_key,
                issue_summary=summary,
                description=description,
                triggered_by="poller",
                jira_assignee=assignee,
            )
            self.state_manager.update_state(
                issue_key,
                metadata={"workflow_type": workflow_type.value},
            )
            state = self.state_manager.get_state(issue_key) or state
            logger.info(
                f"Created new state for {issue_key} with workflow type: {workflow_type.value}"
            )

        try:
            logger.debug(f"Posting initial acknowledgment for {issue_key}")
            self.reporter.post_initial_acknowledgment(state)
        except Exception as e:
            logger.warning(f"Failed to post initial acknowledgment for {issue_key}: {e}")

        logger.info(f"Starting {workflow_type.value} workflow for {issue_key}")
        try:
            if workflow_type == WorkflowType.PLANNING:
                await self._start_planning_workflow(state)
            elif workflow_type == WorkflowType.EXECUTION:
                await self._start_execution_workflow(state)
            elif workflow_type == WorkflowType.ORACLE_CONSULT:
                await self._start_oracle_consultation(state)
        except Exception as e:
            logger.exception(f"Workflow {workflow_type.value} crashed for {issue_key}: {e}", e)
            self._fail_issue(
                issue_key,
                f"Workflow failed: {e}",
                suggestion="Check agent/session logs, then move the issue back to TO DO to retry.",
            )
            self._release_context(issue_key, success=False)
    
    async def _handle_issue_updated(self, event: Dict[str, Any]):
        issue = event.get("issue") or {}
        issue_key = issue.get("key")
        if not issue_key:
            logger.warning("issue_updated event missing issue key, ignoring")
            return
        fields = issue.get("fields", {})
        status_data = fields.get("status", {})
        status_name = status_data.get("name", "")
        
        if self._is_live_processing(issue_key):
            logger.info(f"{issue_key} is live in processing cache; ignoring update event")
            return

        state = self.state_manager.get_state(issue_key)

        logger.debug(f"Issue {issue_key} - Event status: '{status_name}', State status: {state.status.value if state else 'NO_STATE'}")

        if not state:
            await self._handle_issue_created(event)
            return

        # Never interrupt or restart in-flight agent work from poll noise
        if state.status in self.IN_FLIGHT_STATUSES:
            logger.info(
                f"{issue_key} is in-flight ({state.status.value}); ignoring update event"
            )
            return

        # Locale-safe To Do (English "To Do", Turkish "Yapılacaklar", statusCategory=new)
        from src.jira.poller import JiraPoller

        is_todo = JiraPoller._is_todo_status(fields)

        # Terminal → reprocess only when Jira is To Do.
        # ERROR/CANCELLED require requeue_eligible (set by cancel/fail).
        # COMPLETED may reprocess when poller already detected a real reopen.
        if state.status in self.TERMINAL_STATUSES:
            if is_todo:
                meta = state.metadata or {}
                if state.status in (TaskStatus.ERROR, TaskStatus.CANCELLED):
                    if not meta.get("requeue_eligible"):
                        logger.debug(
                            f"{issue_key} is {state.status.value} without "
                            f"requeue_eligible; ignoring update event"
                        )
                        return
                logger.info(
                    f"Reprocessing {issue_key} from terminal state {state.status.value} "
                    f"(Jira status '{status_name}' is To Do, requeue_eligible="
                    f"{bool(meta.get('requeue_eligible'))})"
                )
                self._reset_for_reprocess(issue_key)
                await self._handle_issue_created(event)
            else:
                logger.debug(
                    f"{issue_key} is {state.status.value}; Jira status "
                    f"'{status_name}' is not To Do — not reprocessing"
                )
            return

        # Non-terminal waiting: re-kick PENDING if still To Do
        if is_todo and state.status == TaskStatus.PENDING:
            logger.info(f"{issue_key} is PENDING and still To Do, starting work...")
            await self._handle_issue_created(event)
            return

        # plan_ready → execute only when operator sets Mode: build and moves to To Do
        # (or legacy ai-start-work / ai-execute label). No dashboard Start button.
        if state.status == TaskStatus.PLAN_READY:
            if self._is_live_processing(issue_key):
                logger.info(f"{issue_key} plan_ready but already live; skip start")
                return

            labels = list(fields.get("labels") or [])
            label_set = {str(x).strip().lower() for x in labels}
            has_start_label = (
                "ai-start-work" in label_set or "ai-execute" in label_set
            )

            summary = fields.get("summary", "") or state.issue_summary or ""
            description = fields.get("description", "") or ""
            if not isinstance(description, str):
                description = str(description)
            if not description:
                description = state.description or ""

            # Prefer fresh Jira text so Mode: build is visible to the router
            if summary:
                state.issue_summary = summary
            if description:
                state.description = description

            workflow_type = self._resolve_workflow(
                issue_key, state.issue_summary, state.description
            )
            want_build = (
                workflow_type == WorkflowType.EXECUTION
                or has_start_label
            )

            if is_todo and want_build:
                self.state_manager.update_state(
                    issue_key,
                    issue_summary=state.issue_summary,
                    description=state.description,
                    metadata={"workflow_type": workflow_type.value},
                )
                state = self.state_manager.get_state(issue_key) or state
                logger.info(
                    f"{issue_key} plan_ready + To Do + Mode: build "
                    f"(or start label); beginning execution"
                )
                try:
                    await self._start_execution_workflow(state)
                except Exception as e:
                    logger.exception(
                        f"Plan start from To Do failed for {issue_key}: {e}", e
                    )
                    self._fail_issue(
                        issue_key,
                        f"Failed to start plan execution: {e}",
                        suggestion=(
                            "Check logs, set Mode: build in the description, "
                            "and move the issue back to To Do."
                        ),
                    )
                    self._release_context(issue_key, success=False)
            elif is_todo and not want_build:
                logger.info(
                    f"{issue_key} plan_ready and To Do but Mode is not build; "
                    f"set Mode: build in the description to start execution "
                    f"(workflow={workflow_type.value})"
                )
            elif not is_todo:
                logger.debug(
                    f"{issue_key} plan_ready; waiting for To Do + Mode: build "
                    f"(jira status '{status_name}')"
                )
    
    async def _handle_comment_created(self, event: Dict[str, Any]):
        """Handle new comments (for @mentions)."""
        issue = event.get("issue", {})
        comment = event.get("comment", {})
        
        issue_key = issue.get("key")
        comment_body = comment.get("body", "")
        if isinstance(comment_body, dict):
            # ADF-ish payload — extract plain text best-effort
            comment_body = str(comment_body)
        
        if not issue_key or not comment_body:
            return
        
        # Check for @bot mention
        command = WorkflowRouter.extract_mention_command(comment_body)
        if not command:
            return
        
        # Handle command
        await self._handle_bot_command(issue_key, command)
    
    async def _handle_bot_command(self, issue_key: str, command: str):
        """Handle a command sent via @bot mention."""
        state = self.state_manager.get_state(issue_key)
        
        cmd_lower = command.lower().strip()
        
        if cmd_lower.startswith("/start-work"):
            # Start execution of existing plan
            if state and state.status == TaskStatus.PLAN_READY:
                try:
                    await self._start_execution_workflow(state)
                except Exception as e:
                    logger.exception(f"Execution workflow crashed for {issue_key}: {e}", e)
                    self._fail_issue(
                        issue_key,
                        f"Execution workflow failed: {e}",
                        suggestion="Check logs, then try /start-work again.",
                    )
            else:
                self.reporter.post_comment_response(
                    issue_key,
                    "No plan is ready for execution yet.\n\n"
                    "* If planning is still running, wait for the *Plan Ready* comment.\n"
                    "* If planning failed, move the issue back to To Do to re-queue.\n"
                    "* Then reply with `/start-work` when status is `plan_ready`.",
                )
        
        elif cmd_lower.startswith("/status"):
            # Report current status
            if state:
                err = f"\n*Last error:* {state.error_message[:300]}" if state.error_message else ""
                mr = (state.metadata or {}).get("merge_request_url")
                mr_line = f"\n*Merge request:* {mr}" if mr else ""
                status_msg = (
                    f"*Current status:* {state.status.value}\n"
                    f"*Progress:* {state.progress_percentage}%\n"
                    f"*Workflow:* {(state.metadata or {}).get('workflow_type', 'n/a')}"
                    f"{mr_line}{err}"
                )
                self.reporter.post_comment_response(issue_key, status_msg)
            else:
                self.reporter.post_comment_response(
                    issue_key,
                    "No active work found for this issue in the agent state store.",
                )
        
        elif cmd_lower.startswith("/cancel"):
            # Always cancel state and notify Jira; kill live process when registered
            if state and state.current_task_id:
                runner = self._runner_for(issue_key)
                if runner:
                    runner.cancel_task(state.current_task_id)
            if state:
                self._cancel_issue_state(
                    issue_key,
                    message=(
                        "Work cancelled via bot command. The agent process was "
                        "signalled to stop."
                    ),
                    status=TaskStatus.CANCELLED,
                )
                try:
                    self._release_context(issue_key, success=False)
                except Exception:
                    pass
            else:
                self.reporter.post_comment_response(
                    issue_key,
                    "No active work found to cancel for this issue "
                    "(no local state).",
                )
        
        else:
            # Treat as a direct request
            await self._handle_direct_request(issue_key, command)
    
    def _refresh_issue_text_from_jira(
        self,
        issue_key: str,
        state: Optional[JiraAgentState] = None,
    ) -> tuple[str, str]:
        """Load live summary/description from Jira so git params match the ticket.

        Stale state (or job snapshot) can lag behind edits on the issue; always
        prefer the live fields when the API is available.
        """
        st = state or self.state_manager.get_state(issue_key)
        summary = (st.issue_summary if st else "") or ""
        description = (st.description if st else "") or ""
        try:
            issue = self.jira_client.get_issue(
                issue_key, fields=["summary", "description"]
            )
        except Exception as e:
            logger.warning(f"{issue_key}: live Jira refresh failed: {e}")
            issue = None
        if issue:
            fields = issue.get("fields") or {}
            live_summary = fields.get("summary")
            live_desc = fields.get("description")
            if live_summary is not None and str(live_summary).strip():
                summary = str(live_summary)
            if live_desc is not None:
                if not isinstance(live_desc, str):
                    live_desc = str(live_desc)
                # Allow empty description only when API returned a value
                description = live_desc
            if st and (summary != (st.issue_summary or "") or description != (st.description or "")):
                logger.info(
                    f"{issue_key}: refreshed summary/description from live Jira "
                    f"(desc_len={len(description or '')})"
                )
                self.state_manager.update_state(
                    issue_key,
                    issue_summary=summary,
                    description=description,
                )
                st.issue_summary = summary
                st.description = description
        return summary, description

    def _init_git_manager(
        self,
        issue_key: str,
        state: Optional[JiraAgentState] = None,
    ) -> GitManager:
        """Clone from the issue template (Repository + Source + Target).

        Always re-reads summary/description from Jira when possible so
        ``{params}`` matches the current ticket, not a stale state snapshot.

        Raises IssueGitConfigError / GitCloneError / GitSourceBranchError /
        GitTargetBranchError with user-facing messages suitable for Jira comments.
        """
        logger.info(f"Initializing git manager for {issue_key}")
        st = state or self.state_manager.get_state(issue_key)
        summary, description = self._refresh_issue_text_from_jira(issue_key, st)
        # Re-load state after refresh (update may have persisted)
        st = self.state_manager.get_state(issue_key) or st
        spec = require_issue_git_spec(summary=summary, description=description)
        logger.info(
            f"{issue_key} git from issue: repo={spec.repository_url} "
            f"source_branch={spec.source_branch} target_branch={spec.target_branch} "
            f"(MR plan: work branch from target, then MR source → target)"
        )
        if st:
            self.state_manager.update_state(
                issue_key,
                metadata={
                    "repository_url": spec.repository_url,
                    "source_branch": spec.source_branch,
                    "target_branch": spec.target_branch,
                },
            )

        git = GitManager(
            issue_key=issue_key,
            remote_url=spec.repository_url,
            source_branch=spec.source_branch,
            target_branch=spec.target_branch,
        )
        working_dir = git.get_working_directory()
        logger.debug(f"Working directory: {working_dir}")
        runner = AgentRunner(working_directory=working_dir)
        logger.debug(f"AgentRunner initialized with working directory: {working_dir}")
        # Per-issue isolation: concurrent jobs must not share git/agent slots
        self._contexts[issue_key] = {"git": git, "runner": runner}
        # Keep legacy mirrors for tests/call sites that still use the fields
        self.git_manager = git
        self.agent_runner = runner
        return git

    def _prepare_git_workspace(self, state: JiraAgentState) -> Optional[GitManager]:
        """Init git + work branch from target, or fail the issue with a Jira message.

        Returns GitManager on success, None after calling ``_fail_issue``.
        """
        try:
            git = self._init_git_manager(state.issue_key, state)
            branch_name = git.ensure_feature_branch(state.issue_key)
            logger.info(
                f"Work branch ready: {branch_name} "
                f"(based on target={git.target_branch}; MR {branch_name} → {git.target_branch})"
            )
            return git
        except IssueGitConfigError as e:
            logger.warning(
                f"{state.issue_key} git template error: {e.user_message[:200]}"
            )
            self._fail_issue(
                state.issue_key,
                e.user_message,
                suggestion=(
                    "Update the issue `{params}` block with Repository, "
                    "Source branch, and Target branch, then move back to To Do."
                ),
            )
            self._release_context(state.issue_key, success=False)
            return None
        except GitCloneError as e:
            logger.error(f"{state.issue_key} clone failed: {e}")
            self._fail_issue(
                state.issue_key,
                e.user_message,
                suggestion="Fix repository access or URL, then move back to To Do.",
            )
            self._release_context(state.issue_key, success=False)
            return None
        except GitTargetBranchError as e:
            logger.error(f"{state.issue_key} target branch failed: {e}")
            self._fail_issue(
                state.issue_key,
                e.user_message,
                suggestion=(
                    "Fix Target branch on the issue so it names a branch that "
                    "already exists on GitLab, then move back to To Do."
                ),
            )
            self._release_context(state.issue_key, success=False)
            return None
        except GitSourceBranchError as e:
            logger.error(f"{state.issue_key} source/work branch failed: {e}")
            self._fail_issue(
                state.issue_key,
                e.user_message,
                suggestion="Fix Source branch on the issue, then move back to To Do.",
            )
            self._release_context(state.issue_key, success=False)
            return None
        except Exception as e:
            logger.exception(f"{state.issue_key} git workspace setup failed: {e}", e)
            self._fail_issue(
                state.issue_key,
                f"*Virtual Developer* could not prepare the git workspace.\n\n`{e}`",
                suggestion="Check logs, then move the issue back to To Do.",
            )
            self._release_context(state.issue_key, success=False)
            return None

    async def _start_planning_workflow(self, state: JiraAgentState):
        logger.info(f"Starting planning workflow for {state.issue_key}")
        workflow_start_time = datetime.now()

        # Claim in-flight BEFORE slow git clone so poll cannot double-start
        task = AgentTask(
            description=f"Plan: {state.issue_key}",
            prompt=PromptBuilder.build_prometheus_prompt(
                issue_key=state.issue_key,
                summary=state.issue_summary,
                description=state.description,
            ),
            agent=settings.planning_agent,
            issue_key=state.issue_key,
        )
        self._begin_workflow_run(
            state,
            status=TaskStatus.PLANNING,
            task=task,
            workflow_type="planning",
            agent=settings.planning_agent,
            job_status="planning",
            started_at=workflow_start_time,
        )
        self._mark_jira_in_progress(state.issue_key)

        git = self._prepare_git_workspace(state)
        if git is None:
            return
        runner = self._runner_for(state.issue_key)
        assert runner is not None, "AgentRunner not initialized"

        # Run agent with progress tracking and retry logic
        def on_progress(percentage: int, message: str):
            """Update progress in state - suppress from console."""
            # Progress is tracked in state file, don't print to console
            self.state_manager.update_state(
                state.issue_key,
                progress_percentage=percentage,
            )
            jid = self._active_jobs.get(state.issue_key)
            if jid:
                self.job_store.update_job(jid, progress_percentage=percentage)

        def on_output(stream: str, line: str):
            """Handle output from agent - suppress all output to console.
            
            All output is already logged to session file by agent_runner.
            Only critical messages (retries, timeouts, errors) are printed separately.
            """
            # Suppress all agent output from console - logs go to file only
            pass

        def on_retry(
            attempt_number: int,
            delay_seconds: float,
            reason: str,
            session_file: Optional[str] = None,
            error_message: Optional[str] = None,
            return_code: Optional[int] = None,
            session_id: Optional[str] = None,
            new_task_id: Optional[str] = None,
        ):
            self._record_agent_retry(
                state.issue_key,
                attempt_number=attempt_number,
                delay_seconds=delay_seconds,
                reason=reason,
                session_file=session_file,
                error_message=error_message,
                return_code=return_code,
                session_id=session_id,
                new_task_id=new_task_id,
                progress_percentage=state.progress_percentage,
            )

        result = await runner.run_agent_with_retry(
            task,
            on_output=on_output,
            on_progress=on_progress,
            on_retry=on_retry,
            on_session_file=lambda sp, pp=None: self._link_job_session_paths(
                state.issue_key, sp, pp
            ),
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
            should_abort=lambda: self._is_aborted(state.issue_key),
        )
        self._apply_agent_result_session(state.issue_key, result)

        # Aborted while agent ran (cancel / stuck watchdog) — do not overwrite
        if self._is_aborted(state.issue_key) or result.get("aborted"):
            logger.info(f"Planning aborted for {state.issue_key}; skipping success path")
            self._release_context(state.issue_key, success=False)
            return

        # Update state with retry info
        if result.get("retry_info"):
            retry_info = result["retry_info"]
            update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
            if result.get("timed_out"):
                update_data["timed_out"] = True
            self.state_manager.update_state(state.issue_key, **update_data)

        # Calculate duration from workflow start to final completion (including all retries)
        completed_at = datetime.now()
        duration = (completed_at - workflow_start_time).total_seconds()

        # Check result — plan mode: no GitLab push; durable plan + Jira description
        if result["returncode"] == 0:
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"Planning aborted for {state.issue_key}; skipping plan_ready"
                )
                self._release_context(state.issue_key, success=False)
                return

            plan_path = self._resolve_plan_path(state.issue_key, require_exists=True)
            plan_content = ""
            if plan_path and plan_path.exists():
                try:
                    plan_content = plan_path.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"Could not read plan file {plan_path}: {e}")

            # If agent only left a draft, still accept non-empty markdown as the plan
            if not plan_content.strip():
                self.state_manager.update_state(
                    state.issue_key,
                    execution_duration_seconds=duration,
                )
                self._fail_issue(
                    state.issue_key,
                    "Planning agent exited 0 but no plan file with content was found.",
                    suggestion=(
                        "The planner must write a non-empty plan to "
                        f"`.sisyphus/plans/{state.issue_key}.md` (or `.omo/plans/`) "
                        "without waiting for chat approval. Check session logs, then "
                        "re-queue from To Do."
                    ),
                )
                self._release_context(state.issue_key, success=False)
                return

            # Normalize into preferred sisyphus path in the workspace when we only
            # found an .omo draft/plan so durable + build paths stay consistent
            git = self._git_for(state.issue_key)
            working = git.get_working_directory() if git else None
            if working and plan_path:
                preferred = Path(working) / settings.sisyphus_plans_dir / f"{state.issue_key}.md"
                try:
                    if plan_path.resolve() != preferred.resolve():
                        preferred.parent.mkdir(parents=True, exist_ok=True)
                        preferred.write_text(plan_content, encoding="utf-8")
                        plan_path = preferred
                except Exception as e:
                    logger.warning(f"Could not normalize plan to {preferred}: {e}")

            # B3: durable copy before releasing temp clone
            durable = self._persist_plan(state.issue_key, plan_content)
            if durable is None:
                self._fail_issue(
                    state.issue_key,
                    "Plan was generated but could not be saved to the durable plans directory.",
                    suggestion="Check disk permissions on the plans dir, then re-queue.",
                )
                self._release_context(state.issue_key, success=False)
                return

            if self._is_aborted(state.issue_key):
                self._release_context(state.issue_key, success=False)
                return

            # B6: CAS — do not overwrite CANCELLED/ERROR
            updated = self.state_manager.update_state_if(
                state.issue_key,
                expected_statuses={TaskStatus.PLANNING},
                status=TaskStatus.PLAN_READY,
                plan_path=str(durable),
                completed_at=completed_at,
                execution_duration_seconds=duration,
            )
            if updated is None:
                logger.info(
                    f"Planning success ignored for {state.issue_key} "
                    f"(status no longer PLANNING)"
                )
                self._release_context(state.issue_key, success=False)
                return

            self._finish_job_record(
                state.issue_key, status="plan_ready", progress_percentage=100
            )
            # Release clone after durable plan is saved (plan mode never pushes)
            self._release_context(state.issue_key, success=True)

            ready_state = self.state_manager.get_state(state.issue_key)
            if ready_state:
                try:
                    self.reporter.append_plan_to_description(ready_state, plan_content)
                except Exception as e:
                    logger.warning(
                        f"Could not append plan to description for {state.issue_key}: {e}"
                    )
                try:
                    self.reporter.post_plan_summary(ready_state, plan_content)
                except Exception as e:
                    logger.warning(
                        f"Could not post plan summary for {state.issue_key}: {e}"
                    )

            # Do not auto-start build: operator must set Mode: build and move
            # the issue back to To Do (board poller). Dashboard Start is disabled.

        else:
            # Planning failed — finish job + requeue_eligible via _fail_issue
            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
            )
            self._fail_issue(
                state.issue_key,
                result.get("stderr") or "Planning agent failed",
                suggestion="Check agent/session logs, then move the issue back to To Do to retry.",
            )
            self._release_context(state.issue_key, success=False)

    async def _start_execution_workflow(self, state: JiraAgentState):
        logger.info(f"Starting execution (build) workflow for {state.issue_key}")
        workflow_start_time = datetime.now()

        # Resolve durable plan path before clone so we can materialize into workspace
        durable_plan = self._durable_plan_path(state.issue_key)
        plan_for_prompt = (
            str(durable_plan)
            if durable_plan and durable_plan.exists()
            else (state.plan_path or "")
        )

        # Create task first
        task = AgentTask(
            description=f"Execute: {state.issue_key}",
            prompt=PromptBuilder.build_atlas_prompt(
                issue_key=state.issue_key,
                plan_path=plan_for_prompt,
            ),
            agent=settings.orchestrator_agent,
            issue_key=state.issue_key,
        )

        # Claim in-flight before git clone (archives prior task/session/job ids)
        self._begin_workflow_run(
            state,
            status=TaskStatus.EXECUTING,
            task=task,
            workflow_type="execution",
            agent=settings.orchestrator_agent,
            job_status="executing",
            started_at=workflow_start_time,
        )
        self._mark_jira_in_progress(state.issue_key)

        git = self._prepare_git_workspace(state)
        if git is None:
            return
        # Materialize durable plan into the fresh clone for Atlas
        in_workspace = self._materialize_plan_into_workspace(state.issue_key)
        if in_workspace:
            task.prompt = PromptBuilder.build_atlas_prompt(
                issue_key=state.issue_key,
                plan_path=str(in_workspace),
            )
            self.state_manager.update_state(
                state.issue_key,
                plan_path=str(in_workspace),
            )
        runner = self._runner_for(state.issue_key)
        assert runner is not None, "AgentRunner not initialized"

        # Run agent with progress tracking and retry logic
        def on_progress(percentage: int, message: str):
            """Update progress in state - suppress from console."""
            # Progress is tracked in state file, don't print to console
            self.state_manager.update_state(
                state.issue_key,
                progress_percentage=percentage,
            )
            jid = self._active_jobs.get(state.issue_key)
            if jid:
                self.job_store.update_job(jid, progress_percentage=percentage)

        def on_output(stream: str, line: str):
            """Handle output from agent - suppress all output to console.
            
            All output is already logged to session file by agent_runner.
            """
            # Suppress all agent output from console - logs go to file only
            pass

        def on_retry(
            attempt_number: int,
            delay_seconds: float,
            reason: str,
            session_file: Optional[str] = None,
            error_message: Optional[str] = None,
            return_code: Optional[int] = None,
            session_id: Optional[str] = None,
            new_task_id: Optional[str] = None,
        ):
            self._record_agent_retry(
                state.issue_key,
                attempt_number=attempt_number,
                delay_seconds=delay_seconds,
                reason=reason,
                session_file=session_file,
                error_message=error_message,
                return_code=return_code,
                session_id=session_id,
                new_task_id=new_task_id,
                progress_percentage=state.progress_percentage,
            )

        result = await runner.run_agent_with_retry(
            task,
            on_output=on_output,
            on_progress=on_progress,
            on_retry=on_retry,
            on_session_file=lambda sp, pp=None: self._link_job_session_paths(
                state.issue_key, sp, pp
            ),
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
            should_abort=lambda: self._is_aborted(state.issue_key),
        )
        self._apply_agent_result_session(state.issue_key, result)

        if self._is_aborted(state.issue_key) or result.get("aborted"):
            logger.info(f"Execution aborted for {state.issue_key}; skipping success path")
            self._release_context(state.issue_key, success=False)
            return

        # Update state with retry info
        if result.get("retry_info"):
            retry_info = result["retry_info"]
            update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
            if result.get("timed_out"):
                update_data["timed_out"] = True
            self.state_manager.update_state(state.issue_key, **update_data)

        # Calculate duration from workflow start to final completion (including all retries)
        completed_at = datetime.now()
        duration = (completed_at - workflow_start_time).total_seconds()

        # Check result — B4: exit 0 alone is not enough
        if result["returncode"] == 0:
            delivery_err = self._assert_build_delivery(state.issue_key)
            if delivery_err:
                self._fail_issue(
                    state.issue_key,
                    delivery_err,
                    suggestion=(
                        "Ensure the agent commits on the work branch and leaves "
                        "commits ahead of the target, then re-queue from To Do."
                    ),
                )
                self._release_context(state.issue_key, success=False)
                return

            push_ok = await self._push_and_create_mr(state)
            if not push_ok:
                self._fail_issue(
                    state.issue_key,
                    "Agent finished but git push failed; work was not delivered to remote.",
                    suggestion="Check GitLab remote/credentials, then re-queue from To Do.",
                )
                self._release_context(state.issue_key, success=False)
                return

            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
            )

            await self._complete_work(
                self.state_manager.get_state(state.issue_key),
                execution_summary="All tasks completed successfully.",
            )
        else:
            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
            )
            self._fail_issue(
                state.issue_key,
                result.get("stderr") or "Execution agent failed",
                suggestion="Check agent/session logs, then move the issue back to To Do to retry.",
            )
            self._release_context(state.issue_key, success=False)

    async def _complete_work(
        self, state: JiraAgentState, execution_summary: str = ""
    ) -> None:
        """Mark work completed and notify Jira (no automated code-review phase)."""
        if state is None:
            return
        if self._is_aborted(state.issue_key):
            logger.info(f"Skipping completion for aborted {state.issue_key}")
            self._release_context(state.issue_key, success=False)
            return

        completed_at = datetime.now()
        # B6: CAS — cancel/watchdog terminal must win over late success
        updated = self.state_manager.update_state_if(
            state.issue_key,
            expected_statuses={TaskStatus.EXECUTING},
            reject_statuses=self.ABORTED_STATUSES,
            status=TaskStatus.COMPLETED,
            completed_at=completed_at,
            progress_percentage=100,
        )
        if updated is None:
            logger.info(
                f"Skipping completion for {state.issue_key}: "
                f"status no longer EXECUTING (aborted or raced)"
            )
            self._release_context(state.issue_key, success=False)
            return

        self._finish_job_record(
            state.issue_key, status="completed", progress_percentage=100
        )
        logger.info(f"State updated to COMPLETED for {state.issue_key}")

        summary = (execution_summary or "").strip() or (
            "All tasks completed successfully."
        )
        try:
            self.reporter.post_completion(
                self.state_manager.get_state(state.issue_key),
                summary=summary,
            )
        except Exception as e:
            logger.error(f"Failed to post completion for {state.issue_key}: {e}")

        self._release_context(state.issue_key, success=True)
        logger.info(f"Work completed for {state.issue_key}")

    def _assert_build_delivery(self, issue_key: str) -> Optional[str]:
        """B4/B5: require commits on prepared work_branch before push/complete.

        Returns an error message, or None when delivery looks valid.
        """
        git = self._git_for(issue_key)
        if not git:
            return "No git workspace available after agent run."
        work = (getattr(git, "work_branch", None) or "").strip()
        if not work:
            return "Work branch was not prepared; refusing to treat the run as successful."
        if not git.ensure_on_work_branch():
            current = git.get_current_branch()
            return (
                f"Agent left HEAD on `{current}` instead of work branch `{work}`. "
                "Refusing to push a drifted branch."
            )
        ahead = 0
        if hasattr(git, "commits_ahead_of_target"):
            try:
                ahead = int(git.commits_ahead_of_target(work))
            except Exception:
                ahead = 0
        if ahead < 1:
            return (
                f"No commits on `{work}` ahead of the target branch. "
                "Agent exit code was 0 but nothing was delivered."
            )
        return None

    def _durable_plan_path(self, issue_key: str) -> Path:
        """Host-side plan path that survives temp-clone cleanup."""
        return settings.full_plans_dir / f"{issue_key}.md"

    def _persist_plan(self, issue_key: str, content: str) -> Optional[Path]:
        """Write plan to durable plans dir. Returns path or None on failure."""
        text = (content or "").strip()
        if not text:
            return None
        dest = self._durable_plan_path(issue_key)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
            logger.info(f"Persisted durable plan for {issue_key} at {dest}")
            return dest
        except Exception as e:
            logger.error(f"Failed to persist plan for {issue_key}: {e}")
            return None

    def _materialize_plan_into_workspace(self, issue_key: str) -> Optional[Path]:
        """Copy durable plan into the issue temp clone for Atlas. Returns in-workspace path."""
        durable = self._durable_plan_path(issue_key)
        if not durable.exists():
            # Fall back to any resolved existing plan (e.g. still in old path)
            existing = self._resolve_plan_path(issue_key, require_exists=True)
            if existing and existing.exists():
                try:
                    content = existing.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    return existing if existing.exists() else None
                self._persist_plan(issue_key, content)
                durable = self._durable_plan_path(issue_key)
            else:
                return None
        git = self._git_for(issue_key)
        working = git.get_working_directory() if git else None
        if not working:
            return durable if durable.exists() else None
        try:
            content = durable.read_text(encoding="utf-8", errors="replace")
            rel = Path(settings.sisyphus_plans_dir) / f"{issue_key}.md"
            dest = Path(working) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            logger.info(f"Materialized plan into workspace: {dest}")
            return dest
        except Exception as e:
            logger.warning(f"Could not materialize plan into workspace for {issue_key}: {e}")
            return durable if durable.exists() else None

    async def _push_and_create_mr(self, state: JiraAgentState) -> bool:
        """Push prepared work_branch and open MR.

        Returns True if the branch was pushed (MR is best-effort after push).
        Returns False on missing git manager, protected branch, or push failure.
        Always pushes ``git.work_branch`` (not drifted HEAD) — B5.
        """
        git = self._git_for(state.issue_key)
        if not git:
            logger.warning(f"No git manager for {state.issue_key}")
            try:
                self.reporter.post_progress_update(
                    state,
                    "No git workspace available; cannot push or open a merge request.",
                )
            except Exception:
                pass
            return False

        logger.info(f"Starting push and MR creation for {state.issue_key}")

        # B5: force work_branch, never drift HEAD
        if not git.ensure_on_work_branch():
            msg = (
                f"Refusing to push: could not checkout prepared work branch "
                f"`{getattr(git, 'work_branch', None)}`."
            )
            logger.error(msg)
            try:
                self.reporter.post_progress_update(state, msg)
            except Exception:
                pass
            return False

        branch_name = (getattr(git, "work_branch", None) or "").strip() or git.get_current_branch()

        # Refuse to push protected bases / MR target / release/*
        target = (getattr(git, "target_branch", None) or "").strip().lower()
        protected = {"main", "master", "develop", "trunk", "dev"}
        if target:
            protected.add(target)
        bl = (branch_name or "").lower()
        if not branch_name or bl in protected or bl.startswith("release/"):
            msg = (
                f"Refusing to push protected branch '{branch_name}'. "
                f"Agent must work on a feature/work branch "
                f"(MR source → target `{getattr(git, 'target_branch', '')}`)."
            )
            logger.error(msg)
            try:
                self.reporter.post_progress_update(state, msg)
            except Exception:
                pass
            return False

        # Always record branch for completion messages (even if push fails later)
        self.state_manager.update_state(
            state.issue_key,
            metadata={"feature_branch": branch_name},
        )

        push_success = git.push(branch_name)
        if not push_success:
            logger.warning(f"Push failed or remote not configured for {state.issue_key}")
            try:
                self.reporter.post_progress_update(
                    state,
                    (
                        f"Git push failed for branch `{branch_name or 'unknown'}`. "
                        "Local work may still exist in the agent temp workspace; "
                        "no merge request was created. Check GitLab credentials "
                        "(GITLAB_PAT) and remote access, then re-queue from To Do."
                    ),
                )
            except Exception:
                pass
            return False

        commit_subject = git.get_last_commit_subject()
        commit_body = git.get_last_commit_message()

        if commit_subject:
            mr_title = commit_subject
            mr_body = commit_body if commit_body else commit_subject
        else:
            mr_title = f"[{state.issue_key}] {state.issue_summary}"
            mr_body = state.description or f"Implemented solution for {state.issue_key}"

        target_branch = (
            (getattr(git, "target_branch", None) or getattr(git, "source_branch", None) or "")
            .strip()
            or None
        )
        mr_url = git.create_merge_request(
            title=mr_title,
            body=mr_body,
            target_branch=target_branch,
        )

        if mr_url:
            logger.info(f"Merge request created: {mr_url}")
            # Merge into existing metadata (do not wipe workflow_type, etc.)
            self.state_manager.update_state(
                state.issue_key,
                metadata={
                    "merge_request_url": mr_url,
                    "feature_branch": branch_name,
                },
            )
            try:
                self.reporter.post_progress_update(
                    state,
                    (
                        f"Branch `{branch_name}` pushed and merge request opened:\n"
                        f"{mr_url}"
                    ),
                )
            except Exception:
                pass
        else:
            logger.warning(f"Could not create merge request for {state.issue_key}")
            try:
                self.reporter.post_progress_update(
                    state,
                    (
                        f"Branch `{branch_name}` was pushed to the remote, but a merge "
                        f"request could not be created (target branch may be "
                        f"`{target_branch}`, or `glab` may be missing/misconfigured). "
                        "Open an MR manually in GitLab if needed."
                    ),
                )
            except Exception:
                pass
        # Push succeeded; MR is best-effort
        return True

    def _resolve_plan_path(
        self, issue_key: str, *, require_exists: bool = False
    ) -> Optional[Path]:
        """Locate plan file: durable dir, workspace sisyphus/omo paths.

        When ``require_exists`` is True, never return a missing path (B2).
        Preference order: durable host → ``.sisyphus/plans`` → ``.omo/plans``
        → non-empty ``.omo/drafts`` (Prometheus ULW draft fallback).
        """
        candidates: list[Path] = []
        # Prefer durable host path (survives temp cleanup)
        candidates.append(settings.full_plans_dir / f"{issue_key}.md")
        git = self._git_for(issue_key)
        working = git.get_working_directory() if git else None
        if working:
            base = Path(working)
            candidates.append(base / settings.sisyphus_plans_dir / f"{issue_key}.md")
            candidates.append(base / ".sisyphus" / "plans" / f"{issue_key}.md")
            candidates.append(base / ".omo" / "plans" / f"{issue_key}.md")
            candidates.append(base / ".omo" / "drafts" / f"{issue_key}.md")
        for path in candidates:
            if path.is_file():
                try:
                    if path.read_text(encoding="utf-8", errors="replace").strip():
                        return path
                except Exception:
                    continue
        if require_exists:
            # Last resort: any non-empty markdown under workspace plans/omo
            if working:
                base = Path(working)
                for sub in (
                    base / ".sisyphus" / "plans",
                    base / ".omo" / "plans",
                    base / ".omo" / "drafts",
                ):
                    if not sub.is_dir():
                        continue
                    try:
                        for path in sorted(sub.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                            try:
                                if path.read_text(encoding="utf-8", errors="replace").strip():
                                    logger.info(
                                        f"Resolved plan for {issue_key} via scan: {path}"
                                    )
                                    return path
                            except Exception:
                                continue
                    except Exception:
                        continue
            return None
        # Prefer durable / sisyphus path even if missing (executor materializes later)
        if working:
            return Path(working) / settings.sisyphus_plans_dir / f"{issue_key}.md"
        return candidates[0] if candidates else None

    def _ensure_agent_runner(self, issue_key: str) -> AgentRunner:
        """Ensure an AgentRunner exists for lightweight paths (oracle/comments).

        Always binds to the given issue_key (never reuses another issue's runner).
        Prefers a full git workspace when possible; falls back to an empty
        sandbox under ``.temp/`` — never the daemon project_root.
        """
        existing = self._contexts.get(issue_key)
        if existing and existing.get("runner") is not None:
            self.agent_runner = existing["runner"]
            self.git_manager = existing.get("git")
            return existing["runner"]

        # Adopt a test/pre-set runner only when no other issue contexts exist
        # (avoids cross-issue reuse under concurrency).
        if (
            self.agent_runner is not None
            and not self._contexts
        ):
            self._contexts[issue_key] = {
                "git": self.git_manager,
                "runner": self.agent_runner,
            }
            return self.agent_runner

        try:
            self._init_git_manager(issue_key)
        except Exception as e:
            logger.warning(
                f"Git workspace init failed for {issue_key}, "
                f"using isolated sandbox (not project_root): {e}"
            )
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in (issue_key or "unknown")
            )[:80]
            sandbox = (
                Path.cwd()
                / settings.temp_dir_base
                / f"sandbox_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            sandbox.mkdir(parents=True, exist_ok=True)
            runner = AgentRunner(working_directory=sandbox)
            self._contexts[issue_key] = {"git": None, "runner": runner}
            self.agent_runner = runner
        assert self.agent_runner is not None
        return self.agent_runner

    async def _start_oracle_consultation(self, state: JiraAgentState):
        """Start Oracle consultation."""
        logger.info(f"Starting Oracle consultation for {state.issue_key}")
        success: Optional[bool] = False
        try:
            runner = self._ensure_agent_runner(state.issue_key)

            prompt = PromptBuilder.build_oracle_consult_prompt(
                question=state.description or state.issue_summary or "",
                issue_key=state.issue_key,
                summary=state.issue_summary or "",
            )

            task = AgentTask(
                description=f"Consult: {state.issue_key}",
                prompt=prompt,
                agent="oracle",
                issue_key=state.issue_key,
            )

            self._begin_workflow_run(
                state,
                status=TaskStatus.EXECUTING,
                task=task,
                workflow_type="oracle",
                agent="oracle",
                job_status="executing",
            )
            self._mark_jira_in_progress(state.issue_key)

            result = await runner.run_agent(
                task,
                on_session_file=lambda sp, pp=None: self._link_job_session_paths(
                    state.issue_key, sp, pp
                ),
            )
            self._apply_agent_result_session(state.issue_key, result)

            if self._is_aborted(state.issue_key):
                logger.info(f"Oracle aborted for {state.issue_key}")
                return

            if result["returncode"] == 0:
                self.reporter.post_oracle_response(
                    state.issue_key,
                    question=state.description,
                    answer=result["stdout"],
                )
                updated = self.state_manager.update_state_if(
                    state.issue_key,
                    expected_statuses={TaskStatus.EXECUTING},
                    reject_statuses=self.ABORTED_STATUSES,
                    status=TaskStatus.COMPLETED,
                    completed_at=datetime.now(),
                    progress_percentage=100,
                    current_task_id=None,
                )
                if updated is None:
                    logger.info(
                        f"Oracle completion ignored for {state.issue_key} (aborted)"
                    )
                    success = False
                else:
                    self._finish_job_record(
                        state.issue_key, status="completed", progress_percentage=100
                    )
                    success = True
            else:
                self._fail_issue(
                    state.issue_key,
                    result.get("stderr") or "Oracle consultation failed",
                    suggestion="Rephrase the architecture question or check agent logs.",
                )
        finally:
            self._release_context(state.issue_key, success=success)
    
    async def _handle_direct_request(self, issue_key: str, request: str):
        """Handle a direct request from comment (does not flip whole-issue ERROR)."""
        state = self.state_manager.get_state(issue_key)
        # Only release a context we create here (do not drop an in-flight workflow)
        created_context = issue_key not in self._contexts

        try:
            try:
                runner = self._ensure_agent_runner(issue_key)
            except Exception as e:
                self.reporter.post_comment_response(
                    issue_key,
                    f"Could not start agent for comment: {e}",
                )
                return

            # If runner was adopted into an existing live context, do not release it
            if not created_context:
                created_context = False
            else:
                created_context = issue_key in self._contexts

            # Free-form @mention: execution kit (direct workflow removed)
            summary = (state.issue_summary if state else "") or ""
            prompt = PromptBuilder.build_atlas_prompt(
                issue_key=issue_key,
                plan_path="",
                previous_learnings=[f"Free-form request: {request}", f"Summary: {summary}"],
            )

            task = AgentTask(
                description=f"Comment request: {issue_key}",
                prompt=prompt,
                agent=settings.orchestrator_agent,
                issue_key=issue_key,
            )

            result = await runner.run_agent(task)

            if result["returncode"] == 0:
                self.reporter.post_comment_response(issue_key, result["stdout"])
            else:
                err = result.get("stderr") or "Agent failed for free-form request"
                self.reporter.post_comment_response(
                    issue_key,
                    f"Could not complete request:\n{{code}}\n{err[:1500]}\n{{code}}\n"
                    "Retry or check agent logs.",
                )
        finally:
            if created_context:
                self._release_context(issue_key, success=None)