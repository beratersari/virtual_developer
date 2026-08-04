"""Job processor for handling JIRA events."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        logger.debug(
            f"JobProcessor initialized - default_agent: {settings.default_agent}"
        )
    
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

    def _mark_jira_in_progress(self, issue_key: str) -> bool:
        """Move the Jira issue to an In Progress-like status when work starts.

        Soft-fails: a missing transition must not block agent work. Also updates
        the poller status tracker so leave→return To Do still requeues.
        (Poller may also transition when dispatching; both paths are safe.)

        Returns True only when Jira accepted an In Progress transition (so the
        poller tracker may honestly record ``in progress``).
        """
        try:
            client = self.jira_client
            if client is None:
                return False
            if hasattr(client, "transition_to_in_progress"):
                ok = client.transition_to_in_progress(issue_key)
                if ok:
                    logger.info(f"{issue_key} transitioned to In Progress on Jira")
                    poller = getattr(self, "_poller", None)
                    if poller is not None and hasattr(poller, "_last_jira_status"):
                        poller._last_jira_status[issue_key] = "in progress"
                    return True
                logger.warning(
                    f"{issue_key}: could not transition to In Progress "
                    f"(no matching transition or already in progress)"
                )
                return False
            return False
        except Exception as e:
            logger.warning(f"{issue_key}: In Progress transition failed: {e}")
            return False

    def _poller_tracks_in_progress(self, issue_key: str) -> bool:
        """True when the poller last recorded a real In Progress-like board status."""
        poller = getattr(self, "_poller", None)
        if poller is None or not hasattr(poller, "_last_jira_status"):
            return False
        prev = (poller._last_jira_status.get(issue_key) or "").strip().lower()
        return prev in {"in progress", "in_progress", "doing", "wip"}

    def _ensure_job_for_failure(self, issue_key: str) -> None:
        """Create a job row if validation failed before ``_begin_workflow_run``.

        Config errors (missing Mode / {params}) never start a workflow, so they
        used to leave no Jobs-tab entry while Jira still got an error comment.
        """
        from src.log_context import set_issue_key, set_job_id

        set_issue_key(issue_key)
        if self._active_jobs.get(issue_key):
            set_job_id(self._active_jobs[issue_key])
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
                set_job_id(cur)
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
        """Mark issue ERROR, finish job record, allow re-queue, notify Jira.

        Uses compare-and-swap so a late fail/watchdog cannot overwrite
        COMPLETED or CANCELLED.

        **Intentional fail UX (see AGENTS.md §2):** best-effort move Jira to
        *In Progress*, post a clear error comment, and (only when the board
        really left To Do / tracker already records In Progress) set the
        poller tracker so a later operator move **back to To Do** requeues.
        Do **not** invent tracker ``in progress`` when the transition failed —
        that would make ``force_after_in_progress`` re-fire every poll while
        the ticket never left To Do.
        """
        error_text = (error_message or "Unknown error")[:2000]
        try:
            # Leave To Do when work fails (missing Mode / {params}, agent crash).
            # Poller + workflow also try this; fail path is the last guarantee.
            moved_ip = self._mark_jira_in_progress(issue_key)
            # process_issue may already have transitioned + set tracker before
            # the workflow failed; keep that real IP marker for leave→return.
            already_tracked_ip = self._poller_tracks_in_progress(issue_key)

            # Always surface a job on the dashboard (incl. template/Mode failures)
            self._ensure_job_for_failure(issue_key)
            meta_patch = self._archive_run_identifiers(issue_key)
            meta_patch["requeue_eligible"] = True
            # Fingerprint current text so poller only reprocesses when user edits
            # summary/description while staying on To Do. Store full + light
            # (summary-only) so light board scans do not false-match every poll.
            st0 = self.state_manager.get_state(issue_key)
            if st0 is not None:
                from src.jira.poller import JiraPoller

                meta_patch.update(
                    JiraPoller.text_fingerprints_from_state(
                        st0.issue_summary, st0.description
                    )
                )
            # CAS: never clobber success or operator cancel
            updated = self.state_manager.update_state_if(
                issue_key,
                reject_statuses={TaskStatus.COMPLETED, TaskStatus.CANCELLED},
                status=TaskStatus.ERROR,
                error_message=error_text,
                completed_at=datetime.now(),
                current_task_id=None,
                metadata=meta_patch,
            )
            if updated is None:
                cur = self.state_manager.get_state(issue_key)
                if cur is None:
                    # No local state file — still surface the error on Jira
                    self.reporter.post_comment_response(
                        issue_key,
                        f"An error occurred while processing this issue:\n\n"
                        f"{{code}}\n{error_text}\n{{code}}",
                    )
                    return
                logger.info(
                    f"_fail_issue CAS skip for {issue_key}: "
                    f"status={cur.status.value} "
                    f"(not overwriting COMPLETED/CANCELLED)"
                )
                return
            self._finish_job_record(
                issue_key, status="error", error_message=error_text, progress_percentage=0
            )
            # Only force tracker In Progress when the board actually left To Do
            # (or process_issue already recorded a successful transition).
            if moved_ip or already_tracked_ip:
                self._nudge_poller_after_terminal(issue_key, marker="in progress")
            state = updated
            # Default suggestion for config errors if caller did not pass one
            effective_suggestion = suggestion
            if not effective_suggestion:
                if moved_ip or already_tracked_ip:
                    effective_suggestion = (
                        "Fix the issue description, then move the issue back to "
                        "*To Do* to re-queue."
                    )
                else:
                    effective_suggestion = (
                        "Fix the issue description (and ensure a transition to "
                        "*In Progress* exists on this workflow). Edit the "
                        "description while still on *To Do*, or move the issue "
                        "away and back to *To Do*, to re-queue."
                    )
            comment_id = self.reporter.post_error(
                state, error_text, suggestion=effective_suggestion
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
            force=True,  # intentional reopen: terminal → PENDING for reprocess
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

        - ``marker="in progress"`` (fail path): force tracker to In Progress so
          a later real return to To Do requeues (template/config errors).
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
            marker_l = (marker or "").strip().lower()
            # Explicit: failures must not leave the tracker stuck on "to do"
            # after we moved (or intended to move) Jira to In Progress.
            if marker_l in {"in progress", "in_progress", "doing", "wip"}:
                poller._last_jira_status[issue_key] = "in progress"
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
    ) -> Optional[str]:
        """Archive previous run ids, claim in-flight fields, create a new job.

        Call this instead of writing ``current_task_id`` then starting a job
        separately — archive must run **before** the previous task id is
        overwritten.

        Uses CAS so a concurrent cancel/fail/complete (which does **not** take
        the issue lock) cannot be overwritten by a late begin. Returns the new
        job_id, or ``None`` when the claim was rejected (caller must abort).
        """
        archive = self._archive_run_identifiers(state.issue_key)
        archive["requeue_eligible"] = False
        # Reject terminal statuses: cancel/fail can land between accept and begin
        # without holding the issue lock.
        claimed = self.state_manager.update_state_if(
            state.issue_key,
            reject_statuses=self.TERMINAL_STATUSES,
            status=status,
            started_at=started_at or datetime.now(),
            current_task_id=task.task_id,
            current_opencode_session_id=None,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
            metadata=archive,
        )
        if claimed is None:
            logger.info(
                f"_begin_workflow_run refused for {state.issue_key}: "
                f"status is terminal (cancel/fail/complete won the race)"
            )
            return None
        state.current_task_id = task.task_id
        state.current_opencode_session_id = None
        state.status = claimed.status
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
    ) -> Optional[str]:
        """Create a **new** job history row for this run; returns job_id.

        Never reuses or overwrites a previous job file. Each run gets a unique
        ``job_*`` record with its own task_id / session_id fields.

        If the issue is already terminal (cancel/fail won the race after CAS
        begin), either refuse to create a live row or immediately finish it so
        the Jobs UI never shows a permanent running/planning ghost.
        """
        # If a previous run left an active job pointer, finish it first
        if self._active_jobs.get(state.issue_key):
            self._finish_job_record(
                state.issue_key,
                status="superseded",
                error_message="Superseded by new job start",
            )

        # Cancel/fail can land between CAS claim and job create without the
        # issue lock — re-read disk and refuse a live row for terminal issues.
        live_now = self.state_manager.get_state(state.issue_key)
        if live_now and live_now.status in self.TERMINAL_STATUSES:
            logger.info(
                f"_start_job_record refused for {state.issue_key}: "
                f"already terminal ({live_now.status.value})"
            )
            # Still create a history row marked terminal so operators see the
            # aborted attempt, then leave no live job pointer.
            term = live_now.status.value
            job = self.job_store.create_job(
                issue_key=state.issue_key,
                summary=state.issue_summary or "",
                description=state.description or "",
                workflow_type=workflow_type,
                agent=agent,
                task_id=task_id,
                status=term,
            )
            job_id = job["job_id"]
            self._active_jobs[state.issue_key] = job_id
            self._finish_job_record(
                state.issue_key,
                status=term,
                error_message=(
                    live_now.error_message
                    or f"Not started — issue already {term}"
                ),
            )
            # Keep job_ids history without treating this as a live current job
            st = self.state_manager.get_state(state.issue_key)
            meta = dict((st.metadata if st else None) or {})
            job_ids = list(meta.get("job_ids") or [])
            if job_id not in job_ids:
                job_ids.append(job_id)
            self.state_manager.update_state(
                state.issue_key,
                metadata={
                    "job_ids": job_ids[-200:],
                    # Clear live pointer if finish left it
                    "current_job_id": meta.get("current_job_id"),
                },
            )
            return job_id

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
        try:
            from src.log_context import set_issue_key, set_job_id

            set_issue_key(state.issue_key)
            set_job_id(job_id)
        except Exception:
            pass

        # Re-check after create: cancel may have won during create_job
        st = self.state_manager.get_state(state.issue_key)
        if st and st.status in self.TERMINAL_STATUSES:
            logger.info(
                f"_start_job_record finishing immediately for {state.issue_key}: "
                f"became terminal ({st.status.value}) during job create"
            )
            self._finish_job_record(
                state.issue_key,
                status=st.status.value,
                error_message=st.error_message
                or f"Aborted — issue became {st.status.value}",
            )
            meta = dict(st.metadata or {})
            job_ids = list(meta.get("job_ids") or [])
            if job_id not in job_ids:
                job_ids.append(job_id)
            task_ids = list(meta.get("task_ids") or [])
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
            patch: Dict[str, Any] = {"job_ids": job_ids[-200:]}
            if task_id:
                patch["task_ids"] = task_ids[-100:]
                patch["last_task_id"] = task_id
            self.state_manager.update_state(state.issue_key, metadata=patch)
            return job_id

        meta = dict((st.metadata if st else None) or {})
        job_ids = list(meta.get("job_ids") or [])
        if job_id not in job_ids:
            job_ids.append(job_id)
        task_ids = list(meta.get("task_ids") or [])
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        patch = {
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

        Does **not** wait on the per-issue workflow lock (which may be held for
        the entire agent run). Kill children immediately, then CAS to CANCELLED
        so a late success path cannot overwrite cancel (and fail/watchdog cannot
        overwrite COMPLETED). Returns a status dict for the dashboard API.
        """
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

        # Kill first — never block behind process_event's long-held issue lock
        killed = False
        try:
            runner = self._runner_for(issue_key)
            if runner and state.current_task_id:
                killed = bool(runner.cancel_task(state.current_task_id))
            if runner and hasattr(runner, "cancel_all_tasks"):
                n = runner.cancel_all_tasks()
                if n:
                    killed = True
            self._kill_children_for_issue(issue_key)
        except Exception as e:
            logger.warning(f"cancel_job kill failed for {issue_key}: {e}")

        cancelled = self._cancel_issue_state(
            issue_key,
            message=reason,
            status=TaskStatus.CANCELLED,
        )
        try:
            self._release_context(issue_key, success=False)
        except Exception as e:
            logger.warning(f"cancel_job context release failed for {issue_key}: {e}")

        refreshed = self.state_manager.get_state(issue_key)
        if not cancelled and refreshed and refreshed.status in self.TERMINAL_STATUSES:
            # CAS lost to COMPLETED (or already terminal) — report honestly
            if refreshed.status == TaskStatus.COMPLETED:
                return {
                    "ok": False,
                    "error": "Issue completed before cancel could apply",
                    "issue_key": issue_key,
                    "status": refreshed.status.value,
                    "process_signalled": killed,
                }
            return {
                "ok": False,
                "error": f"Issue is already terminal ({refreshed.status.value})",
                "issue_key": issue_key,
                "status": refreshed.status.value,
                "process_signalled": killed,
            }

        logger.info(f"Job cancelled via API: {issue_key} killed={killed}")
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
        """Start execution for a plan_ready issue (explicit label / internal path).

        Plans never auto-start. Operators either add ``ai-start-work`` /
        ``ai-execute`` (poller) or open a separate issue with ``Mode: build``.
        Dashboard HTTP Start is disabled (410).

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
    ) -> bool:
        """Write a terminal status and notify Jira; clear live task id only.

        Compare-and-swap: never overwrites COMPLETED. When writing CANCELLED,
        also rejects existing ERROR so a concurrent fail does not thrash.
        When writing ERROR (orphan recovery), rejects COMPLETED and CANCELLED.

        Preserves OpenCode session id and records last_task_id in metadata so
        the dashboard can still show identifiers after cancel.

        Returns True when the terminal write applied, False if CAS skipped.
        """
        text = (message or "Work interrupted")[:2000]
        try:
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

            if status == TaskStatus.CANCELLED:
                reject = {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.ERROR}
            else:
                # ERROR path (orphan recovery / interrupt as error)
                reject = {TaskStatus.COMPLETED, TaskStatus.CANCELLED}

            updated = self.state_manager.update_state_if(
                issue_key,
                reject_statuses=reject,
                **update_kwargs,
            )
            if updated is None:
                cur = self.state_manager.get_state(issue_key)
                if cur is None:
                    self.reporter.post_comment_response(
                        issue_key,
                        f"Work interrupted:\n\n{{code}}\n{text}\n{{code}}",
                    )
                    return False
                logger.info(
                    f"_cancel_issue_state CAS skip for {issue_key}: "
                    f"wanted {status.value}, have {cur.status.value}"
                )
                return False

            self._finish_job_record(
                issue_key,
                status=status.value,
                error_message=text,
            )

            self._nudge_poller_after_terminal(issue_key, marker="__cancelled__")

            state = updated
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
            return True
        except Exception as e:
            logger.exception(f"Failed to finalise interrupted state for {issue_key}: {e}", e)
            return False

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

    @staticmethod
    def _unpack_handler_result(result: Any) -> tuple[bool, Optional[str]]:
        """Normalize handler returns for schedule outcome bookkeeping.

        Real handlers return ``(work_started, skip_reason)``. Unit tests often
        patch handlers with bare ``AsyncMock`` (MagicMock return) — treat those
        as ``work_started=True`` so process_event paths keep working.
        """
        if isinstance(result, tuple) and len(result) >= 2:
            skipped = result[1]
            return bool(result[0]), (str(skipped) if skipped else None)
        if isinstance(result, tuple) and len(result) == 1:
            return bool(result[0]), None
        if isinstance(result, bool):
            return result, None
        if result is None:
            return False, "handler returned no result"
        # MagicMock / unexpected objects from tests
        return True, None

    async def process_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Process a JIRA poll (or CLI) event.

        Events use a ``webhookEvent`` key for historical compatibility with
        the poller envelope (``jira:issue_created`` / ``jira:issue_updated``).
        HTTP webhooks are not supported.

        Returns a small outcome dict so schedule dispatch can tell a real start
        from a deliberate no-op (e.g. plan_ready without an explicit start).
        """
        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")
        outcome: Dict[str, Any] = {
            "ok": True,
            "issue_key": issue_key,
            "work_started": False,
            "skipped": None,
        }

        from src.log_context import clear_log_context, set_issue_key, set_job_id

        # Tag all logs for this event with the issue (job_id set when a job starts)
        if issue_key and issue_key != "unknown":
            set_issue_key(issue_key)
            # Resume active job tag if we already hold one (retry / nested)
            active = self._active_jobs.get(issue_key)
            if active:
                set_job_id(active)

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
                        started, skip_reason = self._unpack_handler_result(
                            await self._handle_issue_created(event)
                        )
                        outcome["work_started"] = started
                        outcome["skipped"] = skip_reason
                    elif event_type == "jira:issue_updated":
                        logger.info(f"Handling issue updated event for {issue_key}")
                        started, skip_reason = self._unpack_handler_result(
                            await self._handle_issue_updated(event)
                        )
                        outcome["work_started"] = started
                        outcome["skipped"] = skip_reason
                    elif event_type in ("comment_created", "jira:issue_commented"):
                        logger.info(f"Handling comment created event for {issue_key}")
                        await self._handle_comment_created(event)
                        # Comments may start work; report conservatively via status.
                        st = self.state_manager.get_state(issue_key)
                        if st and st.status in self.IN_FLIGHT_STATUSES:
                            outcome["work_started"] = True
                    else:
                        logger.debug(f"Unknown event type: {event_type}, ignoring")
                        outcome["skipped"] = f"unknown event type: {event_type}"
        except Exception as e:
            logger.exception(f"Unhandled error processing event for {issue_key}: {e}", e)
            outcome["ok"] = False
            outcome["skipped"] = str(e)
            if issue_key and issue_key != "unknown":
                self._fail_issue(
                    issue_key,
                    f"Unhandled error while processing event: {e}",
                    suggestion="Check daemon logs and re-queue the issue if needed.",
                )
                self._release_context(issue_key, success=False)
        finally:
            clear_log_context()
        return outcome
    
    async def _handle_issue_created(
        self, event: Dict[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """Handle create-style events.

        Returns ``(work_started, skip_reason)``. ``skip_reason`` is set when no
        workflow was started (caller may mark a schedule as failed).
        """
        issue = event.get("issue", {})
        issue_key = issue.get("key", "unknown")
        fields = issue.get("fields", {})
        scheduled_job = bool(event.get("scheduled_job"))
        
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
            return False, "already live in processing cache"

        existing = self.state_manager.get_state(issue_key)
        if existing and existing.status in self.IN_FLIGHT_STATUSES:
            logger.info(f"Issue {issue_key} already in progress (status: {existing.status.value}), skipping")
            return False, f"already in progress ({existing.status.value})"
        # PLAN_READY: do not re-plan from a create event.
        # Plans never auto-start (intentional). Explicit start only via
        # ai-start-work / ai-execute labels on issue_updated, a scheduled_job
        # fire (dashboard schedule), or a new Mode: build issue.
        if existing and existing.status == TaskStatus.PLAN_READY:
            if scheduled_job:
                # Schedule fire is an explicit operator start signal.
                if summary:
                    existing.issue_summary = summary
                if description:
                    existing.description = description
                self.state_manager.update_state(
                    issue_key,
                    issue_summary=existing.issue_summary,
                    description=existing.description,
                    metadata={"workflow_type": WorkflowType.EXECUTION.value},
                )
                state = self.state_manager.get_state(issue_key) or existing
                logger.info(
                    f"Issue {issue_key} plan_ready + scheduled_job; "
                    f"beginning execution (schedule_id={event.get('schedule_id')})"
                )
                self._mark_jira_in_progress(issue_key)
                try:
                    await self._start_execution_workflow(state)
                    return True, None
                except Exception as e:
                    logger.exception(
                        f"Scheduled plan start failed for {issue_key}: {e}", e
                    )
                    self._fail_issue(
                        issue_key,
                        f"Failed to start scheduled plan execution: {e}",
                        suggestion=(
                            "Check logs. Re-schedule the issue or add "
                            "ai-start-work / open Mode: build."
                        ),
                    )
                    self._release_context(issue_key, success=False)
                    # Work was attempted (workflow entry); not a silent no-op.
                    return True, None
            logger.info(
                f"Issue {issue_key} has plan ready; no auto-start "
                f"(add label ai-start-work / ai-execute, schedule a run, "
                f"or open Mode: build issue)"
            )
            return False, "plan_ready; no auto-start without schedule or start label"
        
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
                triggered_by="scheduled" if scheduled_job else "poller",
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

        # Claim the board column as soon as we accept the issue — before
        # acknowledgment / git template validation — so incomplete {params}
        # still leave the ticket In Progress for the operator to fix.
        self._mark_jira_in_progress(issue_key)

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
            return True, None
        except Exception as e:
            logger.exception(f"Workflow {workflow_type.value} crashed for {issue_key}: {e}", e)
            self._fail_issue(
                issue_key,
                f"Workflow failed: {e}",
                suggestion="Check agent/session logs, then move the issue back to TO DO to retry.",
            )
            self._release_context(issue_key, success=False)
            # Entry was attempted; schedule should not look like a silent success skip.
            return True, None
    
    async def _handle_issue_updated(
        self, event: Dict[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """Handle update-style events. Returns ``(work_started, skip_reason)``."""
        issue = event.get("issue") or {}
        issue_key = issue.get("key")
        if not issue_key:
            logger.warning("issue_updated event missing issue key, ignoring")
            return False, "missing issue key"
        fields = issue.get("fields", {})
        status_data = fields.get("status", {})
        status_name = status_data.get("name", "")
        
        if self._is_live_processing(issue_key):
            logger.info(f"{issue_key} is live in processing cache; ignoring update event")
            return False, "already live in processing cache"

        state = self.state_manager.get_state(issue_key)

        logger.debug(f"Issue {issue_key} - Event status: '{status_name}', State status: {state.status.value if state else 'NO_STATE'}")

        if not state:
            return await self._handle_issue_created(event)

        # Never interrupt or restart in-flight agent work from poll noise
        if state.status in self.IN_FLIGHT_STATUSES:
            logger.info(
                f"{issue_key} is in-flight ({state.status.value}); ignoring update event"
            )
            return False, f"already in progress ({state.status.value})"

        # Locale-safe To Do (English "To Do", Turkish "Yapılacaklar", statusCategory=new)
        from src.jira.poller import JiraPoller

        is_todo = JiraPoller._is_todo_status(fields)

        # Terminal → reprocess only when Jira is To Do (operator reopen).
        # ERROR/CANCELLED require requeue_eligible (set by cancel/fail).
        # COMPLETED may reprocess when poller detected a real To Do return.
        # Do NOT auto-reprocess while board is still In Progress (that caused
        # infinite "no new commits" job loops every poll).
        if state.status in self.TERMINAL_STATUSES:
            if is_todo:
                meta = state.metadata or {}
                if state.status in (TaskStatus.ERROR, TaskStatus.CANCELLED):
                    if not meta.get("requeue_eligible"):
                        logger.debug(
                            f"{issue_key} is {state.status.value} without "
                            f"requeue_eligible; ignoring update event"
                        )
                        return False, f"{state.status.value} without requeue_eligible"
                logger.info(
                    f"Reprocessing {issue_key} from terminal state {state.status.value} "
                    f"(Jira status '{status_name}' is To Do, requeue_eligible="
                    f"{bool(meta.get('requeue_eligible'))})"
                )
                self._reset_for_reprocess(issue_key)
                return await self._handle_issue_created(event)
            logger.debug(
                f"{issue_key} is {state.status.value}; Jira status "
                f"'{status_name}' is not To Do — not reprocessing"
            )
            return False, f"terminal {state.status.value}; Jira not To Do"

        # Non-terminal waiting: re-kick PENDING if still To Do
        if is_todo and state.status == TaskStatus.PENDING:
            logger.info(f"{issue_key} is PENDING and still To Do, starting work...")
            return await self._handle_issue_created(event)

        # plan_ready → execute only on explicit start labels (ai-start-work /
        # ai-execute). Mode: build alone does NOT auto-start (intentional product
        # choice — no plan→build autostart). Open a new Mode: build issue to
        # implement, or add a start label on this ticket while it is To Do.
        # (scheduled_job create events are handled in _handle_issue_created.)
        if state.status == TaskStatus.PLAN_READY:
            if self._is_live_processing(issue_key):
                logger.info(f"{issue_key} plan_ready but already live; skip start")
                return False, "plan_ready but already live"

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

            if summary:
                state.issue_summary = summary
            if description:
                state.description = description

            if is_todo and has_start_label:
                self.state_manager.update_state(
                    issue_key,
                    issue_summary=state.issue_summary,
                    description=state.description,
                    metadata={"workflow_type": WorkflowType.EXECUTION.value},
                )
                state = self.state_manager.get_state(issue_key) or state
                logger.info(
                    f"{issue_key} plan_ready + To Do + start label; "
                    f"beginning execution"
                )
                try:
                    await self._start_execution_workflow(state)
                    return True, None
                except Exception as e:
                    logger.exception(
                        f"Plan start from label failed for {issue_key}: {e}", e
                    )
                    self._fail_issue(
                        issue_key,
                        f"Failed to start plan execution: {e}",
                        suggestion=(
                            "Check logs. To run build: open a new issue with "
                            "Mode: build, or re-queue with label ai-start-work."
                        ),
                    )
                    self._release_context(issue_key, success=False)
                    return True, None
            if is_todo and not has_start_label:
                logger.info(
                    f"{issue_key} plan_ready and To Do; no auto-start "
                    f"(add ai-start-work / ai-execute, or open Mode: build issue)"
                )
                return False, "plan_ready; no start label"
            logger.debug(
                f"{issue_key} plan_ready; waiting for explicit start "
                f"(jira status '{status_name}')"
            )
            return False, f"plan_ready; jira status '{status_name}'"

        return False, f"no action for status {state.status.value}"
    
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
    ) -> Optional[GitManager]:
        """Clone from the issue template (Repository + Source + Target).

        Always re-reads summary/description from Jira when possible so
        ``{params}`` matches the current ticket, not a stale state snapshot.

        Returns ``None`` when the issue was cancelled/errored during clone
        (workspace discarded; no live context registered).

        Raises IssueGitConfigError / GitCloneError / GitSourceBranchError /
        GitTargetBranchError with user-facing messages suitable for Jira comments.
        """
        logger.info(f"Initializing git manager for {issue_key}")
        if self._is_aborted(issue_key):
            logger.info(f"{issue_key}: aborted before git init; skipping clone")
            return None
        st = state or self.state_manager.get_state(issue_key)
        summary, description = self._refresh_issue_text_from_jira(issue_key, st)
        # Re-load state after refresh (update may have persisted)
        st = self.state_manager.get_state(issue_key) or st
        if self._is_aborted(issue_key):
            logger.info(f"{issue_key}: aborted after Jira refresh; skipping clone")
            return None
        spec = require_issue_git_spec(summary=summary, description=description)
        logger.info(
            f"{issue_key} git from issue: repo={spec.repository_url} "
            f"source_branch={spec.source_branch} target_branch={spec.target_branch} "
            f"(MR plan: work branch from target, then MR source → target)"
        )
        if st and not self._is_aborted(issue_key):
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
        # Cancel may have won while clone ran (no runner registered yet).
        # Do not re-arm live context after terminal status.
        if self._is_aborted(issue_key):
            logger.info(
                f"{issue_key}: aborted during/after clone; discarding workspace"
            )
            try:
                git.cleanup(success=False)
            except Exception as e:
                logger.warning(f"{issue_key}: cleanup after abort failed: {e}")
            return None

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

    def _prepare_git_workspace_blocking(
        self, state: JiraAgentState
    ) -> Optional[GitManager]:
        """Sync clone + work-branch setup (may take minutes on large repos).

        Must not run on the asyncio event loop — use
        :meth:`_prepare_git_workspace` which offloads via ``asyncio.to_thread``.

        Returns ``None`` on hard failure (``_fail_issue`` already called) **or**
        when the issue was cancelled/errored mid-setup (no fail — already terminal).
        """
        try:
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"{state.issue_key}: aborted before git workspace prep"
                )
                return None
            git = self._init_git_manager(state.issue_key, state)
            if git is None:
                # Cancel/fail during clone — do not overwrite terminal with ERROR
                logger.info(
                    f"{state.issue_key}: git init returned None (aborted); "
                    f"skipping agent start"
                )
                return None
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"{state.issue_key}: aborted after git init; releasing context"
                )
                self._release_context(state.issue_key, success=False)
                return None
            branch_name = git.ensure_feature_branch(state.issue_key)
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"{state.issue_key}: aborted after branch setup; releasing context"
                )
                self._release_context(state.issue_key, success=False)
                return None
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
                    "Source branch, Target branch, and Mode (plan or build). "
                    "The issue was moved to *In Progress* — after fixing the "
                    "description, move it back to *To Do* to re-queue."
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

    async def _prepare_git_workspace(
        self, state: JiraAgentState
    ) -> Optional[GitManager]:
        """Init git + work branch without blocking the asyncio event loop.

        Large clones previously froze the ops dashboard (same process/loop as
        uvicorn). Offload clone/checkout to a worker thread.
        """
        return await asyncio.to_thread(self._prepare_git_workspace_blocking, state)

    async def _start_planning_workflow(self, state: JiraAgentState):
        logger.info(f"Starting planning workflow for {state.issue_key}")
        workflow_start_time = datetime.now()

        # Claim in-flight BEFORE slow git clone so poll cannot double-start
        task = AgentTask(
            description=f"Plan: {state.issue_key}",
            prompt=PromptBuilder.build_plan_prompt(
                issue_key=state.issue_key,
                summary=state.issue_summary or "",
                description=state.description or "",
            ),
            agent=settings.default_agent,
            issue_key=state.issue_key,
        )
        job_id = self._begin_workflow_run(
            state,
            status=TaskStatus.PLANNING,
            task=task,
            workflow_type="planning",
            agent=settings.default_agent,
            job_status="planning",
            started_at=workflow_start_time,
        )
        if job_id is None:
            logger.info(
                f"Planning not started for {state.issue_key}: begin claim rejected"
            )
            return
        self._mark_jira_in_progress(state.issue_key)

        git = await self._prepare_git_workspace(state)
        if git is None:
            return
        if self._is_aborted(state.issue_key):
            logger.info(
                f"Planning aborted after clone for {state.issue_key}; "
                f"not starting agent"
            )
            self._release_context(state.issue_key, success=False)
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

            # Do not auto-start build (intentional). Explicit start labels or a
            # new Mode: build issue only. Dashboard Start is disabled.

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

        # Create task first (rebuild prompt after clone with work_branch)
        task = AgentTask(
            description=f"Execute: {state.issue_key}",
            prompt=PromptBuilder.build_build_prompt(
                issue_key=state.issue_key,
                summary=state.issue_summary or "",
                description=state.description or "",
                plan_path=plan_for_prompt or None,
            ),
            agent=settings.default_agent,
            issue_key=state.issue_key,
        )

        # Claim in-flight before git clone (archives prior task/session/job ids)
        job_id = self._begin_workflow_run(
            state,
            status=TaskStatus.EXECUTING,
            task=task,
            workflow_type="execution",
            agent=settings.default_agent,
            job_status="executing",
            started_at=workflow_start_time,
        )
        if job_id is None:
            logger.info(
                f"Execution not started for {state.issue_key}: begin claim rejected"
            )
            return
        self._mark_jira_in_progress(state.issue_key)

        git = await self._prepare_git_workspace(state)
        if git is None:
            return
        if self._is_aborted(state.issue_key):
            logger.info(
                f"Execution aborted after clone for {state.issue_key}; "
                f"not starting agent"
            )
            self._release_context(state.issue_key, success=False)
            return
        # Materialize durable plan into the fresh clone for Atlas
        in_workspace = self._materialize_plan_into_workspace(state.issue_key)
        plan_path_for_agent = (
            str(in_workspace) if in_workspace else plan_for_prompt
        )
        if in_workspace:
            self.state_manager.update_state(
                state.issue_key,
                plan_path=str(in_workspace),
            )
        # Rebuild prompt after workspace prep so work_branch (may differ from
        # issue key) and commit policy use the real checked-out source.
        # Always include Jira title + description (build path).
        raw_wb = getattr(git, "work_branch", None)
        work_branch = raw_wb.strip() if isinstance(raw_wb, str) and raw_wb.strip() else None
        task.prompt = PromptBuilder.build_build_prompt(
            issue_key=state.issue_key,
            summary=state.issue_summary or "",
            description=state.description or "",
            plan_path=plan_path_for_agent or None,
            work_branch=work_branch,
        )
        if work_branch:
            try:
                self.state_manager.update_state(
                    state.issue_key,
                    metadata={"feature_branch": work_branch},
                )
            except Exception:
                pass
        # Snapshot HEAD before the agent runs so re-queues on an existing
        # source branch cannot claim older commits / an old MR as this job's delivery.
        self._snapshot_delivery_baseline(state.issue_key, git)
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

        # Check result — B4: exit 0 alone is not enough for a *new* delivery
        if result["returncode"] == 0:
            delivery_err = self._assert_build_delivery(state.issue_key)
            if delivery_err:
                # Soft success: agent OK but no new commits this run (e.g. re-queue
                # on an already-delivered branch). Complete with note, do not ERROR
                # and do not attribute prior MR/commits to this job.
                if self._is_noop_delivery_message(delivery_err):
                    logger.info(
                        f"{state.issue_key}: completing without new delivery — "
                        f"{delivery_err[:160]}"
                    )
                    self.state_manager.update_state(
                        state.issue_key,
                        execution_duration_seconds=duration,
                        metadata={
                            "delivery_status": "no_new_commits",
                            "delivery_note": delivery_err[:2000],
                        },
                    )
                    jid = self._active_jobs.get(state.issue_key)
                    if jid:
                        try:
                            self.job_store.update_job(
                                jid,
                                delivery_status="no_new_commits",
                                delivery_note=delivery_err[:2000],
                                # Explicitly clear any stale MR/commit attribution
                                merge_request_url=None,
                                commit_sha=None,
                                commit_subject=None,
                                commit_url=None,
                            )
                        except Exception:
                            pass
                    await self._complete_work(
                        self.state_manager.get_state(state.issue_key),
                        execution_summary=(
                            "Agent finished successfully with **no new git delivery** "
                            "for this run.\n\n"
                            f"{delivery_err}\n\n"
                            "Prior branch commits / an existing merge request were "
                            "*not* re-attributed to this job."
                        ),
                    )
                    return

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

            # Cancel/watchdog after agent success: never start delivery
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"Execution aborted for {state.issue_key} before push; "
                    "skipping delivery"
                )
                self._release_context(state.issue_key, success=False)
                return

            push_ok = await self._push_and_create_mr(state)
            # Abort wins over delivery bookkeeping even if push already ran
            if self._is_aborted(state.issue_key):
                logger.info(
                    f"Execution aborted for {state.issue_key} during/after push; "
                    "not marking delivered or completed"
                )
                self._release_context(state.issue_key, success=False)
                return
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
                metadata={
                    "delivery_status": "delivered",
                    "delivery_note": None,
                },
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

    def _snapshot_delivery_baseline(self, issue_key: str, git: Any) -> Optional[str]:
        """Record HEAD SHA at job start (before agent) for delivery attribution.

        Stored on the git manager, active job JSON, and issue metadata so a
        re-run on an existing source branch cannot attribute prior commits/MR
        to the new job.
        """
        sha: Optional[str] = None
        try:
            if hasattr(git, "get_last_commit_sha"):
                raw = git.get_last_commit_sha()
                if raw is not None:
                    sha = str(raw).strip() or None
        except Exception:
            sha = None
        try:
            git.delivery_baseline_sha = sha
        except Exception:
            pass
        jid = self._active_jobs.get(issue_key)
        if jid:
            try:
                self.job_store.update_job(jid, delivery_baseline_sha=sha)
            except Exception:
                pass
        try:
            self.state_manager.update_state(
                issue_key,
                metadata={"delivery_baseline_sha": sha},
            )
        except Exception:
            pass
        if sha:
            logger.info(
                f"{issue_key} delivery baseline HEAD={sha[:12]} "
                f"(job will only count newer commits)"
            )
        else:
            logger.warning(f"{issue_key} could not snapshot delivery baseline SHA")
        return sha

    def _resolve_delivery_baseline(self, issue_key: str, git: Any) -> Optional[str]:
        """Best-effort baseline SHA from git object, job, or issue metadata."""
        for candidate in (
            getattr(git, "delivery_baseline_sha", None),
            None,
        ):
            if candidate:
                return str(candidate).strip() or None
        jid = self._active_jobs.get(issue_key)
        if jid:
            try:
                job = self.job_store.get_job(jid) or {}
                b = job.get("delivery_baseline_sha")
                if b:
                    return str(b).strip() or None
            except Exception:
                pass
        try:
            st = self.state_manager.get_state(issue_key)
            b = (st.metadata or {}).get("delivery_baseline_sha") if st else None
            if b:
                return str(b).strip() or None
        except Exception:
            pass
        return None

    @staticmethod
    def _is_noop_delivery_message(message: str) -> bool:
        """True when delivery failed only because HEAD did not move this job."""
        text = (message or "").lower()
        return (
            "no new commits" in text
            and ("unchanged since job start" in text or "for this job" in text)
        )

    def _assert_build_delivery(self, issue_key: str) -> Optional[str]:
        """Require *new* commits on the work branch for this job before push/complete.

        Returns a message if delivery is not ready, or None when valid.

        ``commits_ahead_of_target`` alone is not enough: a re-queue on an
        existing source branch may already be ahead from a *previous* job.
        We require HEAD to differ from the job-start baseline snapshot.

        Callers treat the “no new commits this job” message as a *soft*
        completion (status completed + note), not an ERROR.
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

        head: Optional[str] = None
        try:
            if hasattr(git, "get_last_commit_sha"):
                raw = git.get_last_commit_sha()
                if raw is not None:
                    head = str(raw).strip() or None
        except Exception:
            head = None
        if not head:
            return (
                f"Could not read HEAD on `{work}` after the agent run; "
                "refusing to treat the run as successful."
            )

        baseline = self._resolve_delivery_baseline(issue_key, git)
        if baseline and head == baseline:
            short = head[:12]
            return (
                f"No new commits on `{work}` for this job "
                f"(HEAD still `{short}`, unchanged since job start). "
                "Agent exit code was 0 but nothing was delivered for this run. "
                "Prior branch commits / an existing merge request are not "
                "attributed to this job."
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

        Checks cancel/watchdog abort before expensive git work and before
        recording delivery so cancel after agent success does not stamp
        ``delivery_status=delivered`` or open an MR after terminal cancel.
        """
        if self._is_aborted(state.issue_key):
            logger.info(
                f"Skipping push/MR for aborted {state.issue_key}"
            )
            return False

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

        # B5: force work_branch, never drift HEAD (off event loop — git I/O)
        on_work = await asyncio.to_thread(git.ensure_on_work_branch)
        if self._is_aborted(state.issue_key):
            logger.info(
                f"Abort after work-branch checkout for {state.issue_key}; "
                "skipping push"
            )
            return False
        if not on_work:
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

        branch_name = (getattr(git, "work_branch", None) or "").strip()
        if not branch_name:
            branch_name = await asyncio.to_thread(git.get_current_branch)

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
        if not self._is_aborted(state.issue_key):
            self.state_manager.update_state(
                state.issue_key,
                metadata={"feature_branch": branch_name},
            )

        if self._is_aborted(state.issue_key):
            logger.info(
                f"Abort before push for {state.issue_key}; skipping remote delivery"
            )
            return False

        push_success = await asyncio.to_thread(git.push, branch_name)
        if self._is_aborted(state.issue_key):
            # Push may have already completed; do not open MR or stamp delivery.
            logger.warning(
                f"{state.issue_key}: abort after push attempt — "
                "not creating MR or recording delivery"
            )
            return False
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
        if commit_subject is not None:
            commit_subject = str(commit_subject).strip() or None
        commit_sha = None
        commit_url = None
        try:
            raw_sha = git.get_last_commit_sha()
            if raw_sha is not None:
                commit_sha = str(raw_sha).strip() or None
            if commit_sha and hasattr(git, "build_commit_url"):
                raw_url = git.build_commit_url(commit_sha)
                if raw_url is not None:
                    commit_url = str(raw_url).strip() or None
        except Exception:
            commit_sha = None
            commit_url = None

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
        if self._is_aborted(state.issue_key):
            logger.warning(
                f"{state.issue_key}: abort before MR — not recording delivery"
            )
            return False
        mr_url = await asyncio.to_thread(
            git.create_merge_request,
            title=mr_title,
            body=mr_body,
            target_branch=target_branch,
        )
        if self._is_aborted(state.issue_key):
            logger.warning(
                f"{state.issue_key}: abort after MR attempt — not recording delivery"
            )
            return False

        # Only attribute MR/commit to *this* job when HEAD moved past baseline
        baseline = self._resolve_delivery_baseline(state.issue_key, git)
        if baseline and commit_sha and commit_sha == baseline:
            logger.warning(
                f"{state.issue_key}: refusing to record git delivery — "
                f"commit {commit_sha[:12]} is the job-start baseline (no new work)"
            )
            return False

        # Persist delivery on this job + issue metadata history (multi-run safe)
        self._record_git_delivery(
            state,
            feature_branch=branch_name,
            merge_request_url=mr_url,
            commit_sha=commit_sha,
            commit_subject=commit_subject,
            commit_url=commit_url,
        )

        if mr_url:
            logger.info(f"Merge request created: {mr_url}")
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
                commit_line = (
                    f"\nCommit: `{commit_sha[:12]}`" if commit_sha else ""
                )
                if commit_url:
                    commit_line = f"\nCommit: {commit_url}"
                self.reporter.post_progress_update(
                    state,
                    (
                        f"Branch `{branch_name}` was pushed to the remote, but a merge "
                        f"request could not be created (target branch may be "
                        f"`{target_branch}`, or `glab` may be missing/misconfigured). "
                        "Open an MR manually in GitLab if needed."
                        f"{commit_line}"
                    ),
                )
            except Exception:
                pass
        # Push succeeded; MR is best-effort
        return True

    def _record_git_delivery(
        self,
        state: JiraAgentState,
        *,
        feature_branch: Optional[str] = None,
        merge_request_url: Optional[str] = None,
        commit_sha: Optional[str] = None,
        commit_subject: Optional[str] = None,
        commit_url: Optional[str] = None,
    ) -> None:
        """Store push/MR/commit on the active job and append issue delivery history.

        Latest ``merge_request_url`` / ``feature_branch`` remain on issue metadata
        for reporters; full history lives in ``metadata.git_deliveries`` and each
        job JSON so the dashboard can show every run for a re-triggered task.
        """
        job_id = self._active_jobs.get(state.issue_key)
        now = datetime.now().isoformat(timespec="seconds")
        delivery = {
            "job_id": job_id,
            "feature_branch": feature_branch or None,
            "merge_request_url": merge_request_url or None,
            "commit_sha": commit_sha or None,
            "commit_subject": commit_subject or None,
            "commit_url": commit_url or None,
            "created_at": now,
        }

        if job_id:
            try:
                self.job_store.update_job(
                    job_id,
                    feature_branch=feature_branch or None,
                    merge_request_url=merge_request_url or None,
                    commit_sha=commit_sha or None,
                    commit_subject=commit_subject or None,
                    commit_url=commit_url or None,
                )
            except Exception as e:
                logger.warning(
                    f"Could not persist git delivery on job {job_id}: {e}"
                )

        # Append history (newest last); keep latest MR/branch as top-level keys
        history: List[Any] = []
        try:
            st = self.state_manager.get_state(state.issue_key)
            prev = (st.metadata or {}).get("git_deliveries") if st else None
            if isinstance(prev, list):
                history = list(prev)
        except Exception:
            history = []
        history.append(delivery)
        # Cap history to avoid unbounded metadata growth
        if len(history) > 50:
            history = history[-50:]

        meta_patch: Dict[str, Any] = {
            "feature_branch": feature_branch or None,
            "git_deliveries": history,
        }
        if merge_request_url:
            meta_patch["merge_request_url"] = merge_request_url
        if commit_sha:
            meta_patch["last_commit_sha"] = commit_sha
        if commit_url:
            meta_patch["last_commit_url"] = commit_url
        if commit_subject:
            meta_patch["last_commit_subject"] = commit_subject

        try:
            self.state_manager.update_state(state.issue_key, metadata=meta_patch)
        except Exception as e:
            logger.warning(
                f"Could not persist git delivery metadata for {state.issue_key}: {e}"
            )

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
            git = self._init_git_manager(issue_key)
            if git is not None:
                runner = self._runner_for(issue_key)
                if runner is not None:
                    return runner
            # Aborted during clone (None) or context not registered — sandbox below
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
        return runner

    async def _start_oracle_consultation(self, state: JiraAgentState):
        """Start Oracle consultation."""
        logger.info(f"Starting Oracle consultation for {state.issue_key}")
        success: Optional[bool] = False
        try:
            runner = self._ensure_agent_runner(state.issue_key)

            prompt = PromptBuilder.build_plan_prompt(
                issue_key=state.issue_key,
                summary=state.issue_summary or "",
                description=state.description or state.issue_summary or "",
            )

            task = AgentTask(
                description=f"Consult: {state.issue_key}",
                prompt=prompt,
                agent="oracle",
                issue_key=state.issue_key,
            )

            job_id = self._begin_workflow_run(
                state,
                status=TaskStatus.EXECUTING,
                task=task,
                workflow_type="oracle",
                agent="oracle",
                job_status="executing",
            )
            if job_id is None:
                logger.info(
                    f"Oracle not started for {state.issue_key}: begin claim rejected"
                )
                return
            self._mark_jira_in_progress(state.issue_key)

            if self._is_aborted(state.issue_key):
                logger.info(f"Oracle aborted before agent for {state.issue_key}")
                self._release_context(state.issue_key, success=False)
                return

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

            # Free-form @mention: same build path (system + title + description)
            summary = (state.issue_summary if state else "") or ""
            git = self._git_for(issue_key)
            work_branch = (
                (getattr(git, "work_branch", None) or "").strip() if git else ""
            ) or None
            desc = (request or "").strip() or (state.description if state else "") or ""
            prompt = PromptBuilder.build_build_prompt(
                issue_key=issue_key,
                summary=summary,
                description=desc,
                work_branch=work_branch,
            )

            task = AgentTask(
                description=f"Comment request: {issue_key}",
                prompt=prompt,
                agent=settings.default_agent,
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