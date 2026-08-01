"""Job processor for handling JIRA events."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings
from src.git_manager import GitManager
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

    def resize(self, limit: int) -> None:
        self._limit = max(1, int(limit))

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

    def _mark_jira_in_progress(self, issue_key: str) -> None:
        """Move the Jira issue to an In Progress-like status when work starts.

        Called from the processor (not only the poller) so ``cli.py process``
        also transitions the board column.
        Soft-fails: a missing transition must not block agent work.
        """
        try:
            client = self.jira_client
            if client is None:
                return
            if hasattr(client, "transition_to_in_progress"):
                ok = client.transition_to_in_progress(issue_key)
                if ok:
                    logger.info(f"{issue_key} transitioned to In Progress on Jira")
                else:
                    logger.warning(
                        f"{issue_key}: could not transition to In Progress "
                        f"(no matching transition or already in progress)"
                    )
        except Exception as e:
            logger.warning(f"{issue_key}: In Progress transition failed: {e}")

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
            meta_patch = self._archive_run_identifiers(issue_key)
            meta_patch["requeue_eligible"] = True
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
        """Return the agent runner bound to this issue (isolation-safe)."""
        ctx = self._contexts.get(issue_key)
        if ctx and ctx.get("runner") is not None:
            return ctx["runner"]
        return self.agent_runner

    def _git_for(self, issue_key: str) -> Optional[GitManager]:
        """Return the git manager bound to this issue (isolation-safe)."""
        ctx = self._contexts.get(issue_key)
        if ctx and ctx.get("git") is not None:
            return ctx["git"]
        return self.git_manager

    def _is_aborted(self, issue_key: str) -> bool:
        """True if issue was cancelled/errored while work was still running."""
        state = self.state_manager.get_state(issue_key)
        return bool(state and state.status in self.ABORTED_STATUSES)

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

    def cancel_job(self, issue_key: str, *, reason: str = "Cancelled from dashboard") -> dict:
        """Cancel a job: kill agent children, set CANCELLED, notify Jira.

        Returns a status dict for the dashboard API.
        """
        state = self.state_manager.get_state(issue_key)
        if not state:
            return {"ok": False, "error": "No local state for this issue", "issue_key": issue_key}

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
        # Also skip PLAN_READY — waiting for /start-work or auto-start; do not re-plan
        if existing and existing.status == TaskStatus.PLAN_READY:
            logger.info(f"Issue {issue_key} has plan ready, skipping re-create")
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
            workflow_type = WorkflowRouter.route_issue(issue_key, state.issue_summary, state.description)
            state.metadata["workflow_type"] = workflow_type.value
            self.state_manager.set_state(state)
            logger.info(f"Determined workflow type: {workflow_type.value} for existing issue {issue_key}")
        else:
            assignee_data = fields.get("assignee")
            assignee = assignee_data.get("displayName") if assignee_data else None
            
            workflow_type = WorkflowRouter.route_issue(issue_key, summary, description)
            logger.info(f"Determined workflow type: {workflow_type.value} for new issue {issue_key}")
            
            state = self.state_manager.create_state(
                issue_key=issue_key,
                issue_summary=summary,
                description=description,
                triggered_by="poller",
                jira_assignee=assignee,
            )
            state.metadata["workflow_type"] = workflow_type.value
            self.state_manager.set_state(state)
            logger.info(f"Created new state for {issue_key} with workflow type: {workflow_type.value}")
        
        try:
            logger.debug(f"Posting initial acknowledgment for {issue_key}")
            self.reporter.post_initial_acknowledgment(state)
        except Exception as e:
            logger.warning(f"Failed to post initial acknowledgment for {issue_key}: {e}")
        
        logger.info(f"Starting {workflow_type.value} workflow for {issue_key}")
        try:
            if workflow_type == WorkflowType.PLANNING:
                await self._start_planning_workflow(state)
            elif workflow_type == WorkflowType.DIRECT_EXECUTION:
                await self._start_direct_execution(state)
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

        # Non-terminal waiting states (PENDING, PLAN_READY): re-kick PENDING if still To Do
        if is_todo and state.status == TaskStatus.PENDING:
            logger.info(f"{issue_key} is PENDING and still To Do, starting work...")
            await self._handle_issue_created(event)
    
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
    
    def _init_git_manager(self, issue_key: str) -> GitManager:
        logger.info(f"Initializing git manager for {issue_key}")
        git = GitManager(issue_key=issue_key)
        working_dir = git.get_working_directory()
        logger.debug(f"Working directory: {working_dir}")
        runner = AgentRunner(working_directory=working_dir)
        logger.debug(f"AgentRunner initialized with working directory: {working_dir}")
        # Per-issue context so concurrent jobs do not clobber each other
        self._contexts[issue_key] = {"git": git, "runner": runner}
        # Keep legacy mirrors for tests/call sites that still use the fields
        self.git_manager = git
        self.agent_runner = runner
        return git

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

        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")
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
            """Handle retry notification - AgentRunner already prints this."""
            # AgentRunner prints retry messages, we just update state here

            # Create retry attempt record with error details and opencode session ID
            retry_attempt = RetryAttempt(
                attempt_number=attempt_number,
                timestamp=datetime.now(),
                reason=reason,
                delay_seconds=delay_seconds,
                session_log_path=session_file,
                error_message=error_message,
                return_code=return_code,
                opencode_session_id=session_id,  # Store opencode session ID
            )

            # Get current state and add retry attempt
            current_state = self.state_manager.get_state(state.issue_key)
            if current_state:
                current_state.add_retry_attempt(retry_attempt)
                # Update current_opencode_session_id to the opencode session ID from this retry
                if session_id:
                    current_state.current_opencode_session_id = session_id
                # Keep cancel/watchdog pointed at the live retry process
                if new_task_id:
                    current_state.current_task_id = new_task_id
                self.state_manager.set_state(current_state)
            if session_id:
                self._record_opencode_session(
                    state.issue_key, session_id, session_file=session_file
                )
            # Keep job record in sync with live retry ids
            jid = self._active_jobs.get(state.issue_key)
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
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
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
        )
        self._apply_agent_result_session(state.issue_key, result)

        # Aborted while agent ran (cancel / stuck watchdog) — do not overwrite
        if self._is_aborted(state.issue_key):
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

        # Check result
        if result["returncode"] == 0:
            # Agent should have already committed the plan
            # Push branch to remote and create merge request for the plan
            await self._push_and_create_mr(state)

            plan_path = self._resolve_plan_path(state.issue_key)
            plan_content = ""
            if plan_path and plan_path.exists():
                plan_content = plan_path.read_text()

            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.PLAN_READY,
                plan_path=str(plan_path) if plan_path else None,
                completed_at=completed_at,
                execution_duration_seconds=duration,
                # current_opencode_session_id keeps the last retry's session ID
            )
            self._finish_job_record(
                state.issue_key, status="plan_ready", progress_percentage=100
            )
            self._release_context(state.issue_key, success=True)

            # Post plan summary
            self.reporter.post_plan_summary(
                self.state_manager.get_state(state.issue_key),
                plan_content,
            )

            # Auto-start if configured
            if settings.auto_start_plans:
                await self._start_execution_workflow(
                    self.state_manager.get_state(state.issue_key)
                )
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
        logger.info(f"Starting execution workflow for {state.issue_key}")
        workflow_start_time = datetime.now()

        # Create task first
        task = AgentTask(
            description=f"Execute: {state.issue_key}",
            prompt=PromptBuilder.build_atlas_prompt(
                issue_key=state.issue_key,
                plan_path=state.plan_path or "",
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

        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")
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
            """Handle retry notification - AgentRunner already prints this."""
            # AgentRunner prints retry messages, we just update state here

            # Create retry attempt record with error details and opencode session ID
            retry_attempt = RetryAttempt(
                attempt_number=attempt_number,
                timestamp=datetime.now(),
                reason=reason,
                delay_seconds=delay_seconds,
                session_log_path=session_file,
                error_message=error_message,
                return_code=return_code,
                opencode_session_id=session_id,  # Store opencode session ID
            )

            # Get current state and add retry attempt
            current_state = self.state_manager.get_state(state.issue_key)
            if current_state:
                current_state.add_retry_attempt(retry_attempt)
                # Update current_opencode_session_id to the opencode session ID from this retry
                if session_id:
                    current_state.current_opencode_session_id = session_id
                # Keep cancel/watchdog pointed at the live retry process
                if new_task_id:
                    current_state.current_task_id = new_task_id
                self.state_manager.set_state(current_state)
            if session_id:
                self._record_opencode_session(
                    state.issue_key, session_id, session_file=session_file
                )
            # Keep job record in sync with live retry ids
            jid = self._active_jobs.get(state.issue_key)
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
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
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
        )
        self._apply_agent_result_session(state.issue_key, result)

        if self._is_aborted(state.issue_key):
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

        # Check result
        if result["returncode"] == 0:
            # Agent should have already committed changes
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
    
    async def _start_direct_execution(self, state: JiraAgentState):
        logger.info(f"Starting direct execution workflow for {state.issue_key}")
        logger.debug(f"Using agent: {settings.default_agent}, category: {settings.execution_category}")

        workflow_start_time = datetime.now()

        # Create task with category
        task = AgentTask(
            description=f"Direct: {state.issue_key}",
            prompt=PromptBuilder.build_sisyphus_prompt(
                issue_key=state.issue_key,
                task_description=state.description,
            ),
            agent=settings.default_agent,
            category=settings.execution_category,
            issue_key=state.issue_key,
        )

        # Claim in-flight before git clone (archives prior task/session/job ids)
        self._begin_workflow_run(
            state,
            status=TaskStatus.EXECUTING,
            task=task,
            workflow_type="direct",
            agent=settings.default_agent,
            job_status="executing",
            started_at=workflow_start_time,
        )
        self._mark_jira_in_progress(state.issue_key)

        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")
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
            """Handle retry notification - AgentRunner already prints this."""
            # AgentRunner prints retry messages, we just update state here

            # Create retry attempt record with error details and opencode session ID
            retry_attempt = RetryAttempt(
                attempt_number=attempt_number,
                timestamp=datetime.now(),
                reason=reason,
                delay_seconds=delay_seconds,
                session_log_path=session_file,
                error_message=error_message,
                return_code=return_code,
                opencode_session_id=session_id,  # Store opencode session ID
            )

            # Get current state and add retry attempt
            current_state = self.state_manager.get_state(state.issue_key)
            if current_state:
                current_state.add_retry_attempt(retry_attempt)
                # Update current_opencode_session_id to the opencode session ID from this retry
                if session_id:
                    current_state.current_opencode_session_id = session_id
                # Keep cancel/watchdog pointed at the live retry process
                if new_task_id:
                    current_state.current_task_id = new_task_id
                self.state_manager.set_state(current_state)
            if session_id:
                self._record_opencode_session(
                    state.issue_key, session_id, session_file=session_file
                )
            # Keep job record in sync with live retry ids
            jid = self._active_jobs.get(state.issue_key)
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
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
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
        )
        self._apply_agent_result_session(state.issue_key, result)

        if self._is_aborted(state.issue_key):
            logger.info(f"Direct execution aborted for {state.issue_key}; skipping success path")
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

        # Calculate cost and timing
        from src.shared.cost_calculator import calculate_cost, format_cost_report

        cost_data = calculate_cost(
            input_text=task.prompt,
            output_text=result.get("stdout", ""),
            model=settings.execution_category,
        )

        # Timing and cost info is in state file, suppress from console
        
        # Handle result
        if result["returncode"] == 0:
            # Agent has already committed changes
            # Now push to remote and create merge request
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
                token_usage_input=cost_data["input_tokens"],
                token_usage_output=cost_data["output_tokens"],
                estimated_cost=cost_data["estimated_cost"],
            )

            await self._complete_work(
                self.state_manager.get_state(state.issue_key),
                execution_summary=result["stdout"][:1000],
            )
        else:
            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
                token_usage_input=cost_data["input_tokens"],
                token_usage_output=cost_data["output_tokens"],
                estimated_cost=cost_data["estimated_cost"],
            )
            self._fail_issue(
                state.issue_key,
                result.get("stderr") or "Direct execution agent failed",
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
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.COMPLETED,
            completed_at=completed_at,
            progress_percentage=100,
        )
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

    async def _push_and_create_mr(self, state: JiraAgentState) -> bool:
        """Push feature branch and open MR.

        Returns True if the branch was pushed (MR is best-effort after push).
        Returns False on missing git manager, protected branch, or push failure.
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

        branch_name = git.get_current_branch()

        # Refuse to push protected default branches
        protected = {
            "main",
            "master",
            (settings.default_branch or "").strip().lower(),
        }
        if branch_name and branch_name.lower() in protected:
            msg = (
                f"Refusing to push protected branch '{branch_name}'. "
                f"Agent must work on a feature branch."
            )
            logger.error(msg)
            try:
                self.reporter.post_progress_update(state, msg)
            except Exception:
                pass
            return False

        # Always record branch for completion messages (even if push fails later)
        if branch_name:
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

        target_branch = settings.default_branch.strip() if settings.default_branch else "main"
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

    def _resolve_plan_path(self, issue_key: str) -> Optional[Path]:
        """Locate plan file in agent workspace first, then daemon CWD."""
        candidates: list[Path] = []
        git = self._git_for(issue_key)
        working = git.get_working_directory() if git else None
        if working:
            base = Path(working)
            candidates.append(base / settings.sisyphus_plans_dir / f"{issue_key}.md")
            candidates.append(base / ".sisyphus" / "plans" / f"{issue_key}.md")
        candidates.append(settings.full_plans_dir / f"{issue_key}.md")
        for path in candidates:
            if path.exists():
                return path
        # Prefer workspace path even if missing (executor looks relative to clone)
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
                question=state.description,
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
                self.state_manager.update_state(
                    state.issue_key,
                    status=TaskStatus.COMPLETED,
                    completed_at=datetime.now(),
                    progress_percentage=100,
                    current_task_id=None,
                )
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

            prompt = PromptBuilder.build_comment_response_prompt(
                issue_key=issue_key,
                comment_text=request,
                current_state=state.status.value if state else None,
            )

            task = AgentTask(
                description=f"Comment response: {issue_key}",
                prompt=prompt,
                agent=settings.default_agent,
                issue_key=issue_key,
            )

            result = await runner.run_agent(task)

            if result["returncode"] == 0:
                self.reporter.post_comment_response(issue_key, result["stdout"])
            else:
                err = result.get("stderr") or "Comment response agent failed"
                self.reporter.post_comment_response(
                    issue_key,
                    f"Could not complete comment request:\n{{code}}\n{err[:1500]}\n{{code}}\n"
                    "Retry the @mention or check agent logs.",
                )
        finally:
            if created_context:
                self._release_context(issue_key, success=None)