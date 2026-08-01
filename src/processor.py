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
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, RetryAttempt, TaskStatus


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
        """Mark issue ERROR and always attempt to notify the user via Jira."""
        error_text = (error_message or "Unknown error")[:2000]
        try:
            updated = self.state_manager.update_state(
                issue_key,
                status=TaskStatus.ERROR,
                error_message=error_text,
                completed_at=datetime.now(),
            )
            state = updated or self.state_manager.get_state(issue_key)
            if state is None:
                # State missing — still try a bare comment so the user is not blind
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

    def _reset_for_reprocess(self, issue_key: str) -> None:
        """Clear runtime fields before restarting work on an issue."""
        self.state_manager.update_state(
            issue_key,
            status=TaskStatus.PENDING,
            progress_percentage=0,
            error_message=None,
            current_task_id=None,
            current_opencode_session_id=None,
            timed_out=False,
            completed_at=None,
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
        """Write a terminal status and notify Jira; clear runtime task fields."""
        text = (message or "Work interrupted")[:2000]
        try:
            updated = self.state_manager.update_state(
                issue_key,
                status=status,
                error_message=text,
                completed_at=datetime.now(),
                current_task_id=None,
                current_opencode_session_id=None,
            )
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
            self._job_semaphore = asyncio.Semaphore(limit)
        
        try:
            async with self._job_semaphore:
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

        # Terminal → reprocess only when Jira is To Do (user reopened / left in todo)
        if state.status in self.TERMINAL_STATUSES:
            if is_todo:
                logger.info(
                    f"Reprocessing {issue_key} from terminal state {state.status.value} "
                    f"(Jira status '{status_name}' is To Do)"
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
                self.state_manager.update_state(issue_key, status=TaskStatus.CANCELLED)
                self.reporter.post_comment_response(
                    issue_key,
                    "Work cancelled. The agent process was signalled to stop and "
                    "local status is set to *cancelled*. Move the issue back to "
                    "To Do if you want to re-queue later.",
                )
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
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.PLANNING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
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
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

        # Aborted while agent ran (cancel / stuck watchdog) — do not overwrite
        if self._is_aborted(state.issue_key):
            logger.info(f"Planning aborted for {state.issue_key}; skipping success path")
            self._release_context(state.issue_key, success=False)
            return

        # Update state with retry info and final opencode session ID
        if result.get("retry_info"):
            retry_info = result["retry_info"]
            update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
            if result.get("timed_out"):
                update_data["timed_out"] = True
            # Update current_opencode_session_id to the last opencode session ID from retry_info
            last_session_id = retry_info.get("last_opencode_session_id")
            if last_session_id:
                update_data["current_opencode_session_id"] = last_session_id
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
            # Planning failed
            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.ERROR,
                error_message=result["stderr"],
                completed_at=completed_at,
                execution_duration_seconds=duration,
                # current_opencode_session_id keeps the last retry's session ID (even on failure)
            )
            self.reporter.post_error(
                self.state_manager.get_state(state.issue_key),
                result["stderr"],
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

        # Claim in-flight before git clone
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,  # Will be set from agent output
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
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
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

        if self._is_aborted(state.issue_key):
            logger.info(f"Execution aborted for {state.issue_key}; skipping success path")
            self._release_context(state.issue_key, success=False)
            return

        # Update state with retry info and final opencode session ID
        if result.get("retry_info"):
            retry_info = result["retry_info"]
            update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
            if result.get("timed_out"):
                update_data["timed_out"] = True
            # Update current_opencode_session_id to the last opencode session ID from retry_info
            last_session_id = retry_info.get("last_opencode_session_id")
            if last_session_id:
                update_data["current_opencode_session_id"] = last_session_id
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
                status=TaskStatus.ERROR,
                error_message=result["stderr"],
                completed_at=completed_at,
                execution_duration_seconds=duration,
                # current_opencode_session_id keeps the last retry's session ID (even on failure)
            )
            self.reporter.post_error(
                self.state_manager.get_state(state.issue_key),
                result["stderr"],
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

        # Claim in-flight before git clone
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,  # Will be set from agent output
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
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
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

        if self._is_aborted(state.issue_key):
            logger.info(f"Direct execution aborted for {state.issue_key}; skipping success path")
            self._release_context(state.issue_key, success=False)
            return

        # Update state with retry info and final opencode session ID
        if result.get("retry_info"):
            retry_info = result["retry_info"]
            update_data = {"retry_count": retry_info.get("attempts", 0) - 1}
            if result.get("timed_out"):
                update_data["timed_out"] = True
            # Update current_opencode_session_id to the last opencode session ID from retry_info
            last_session_id = retry_info.get("last_opencode_session_id")
            if last_session_id:
                update_data["current_opencode_session_id"] = last_session_id
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
            updated_state = self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.ERROR,
                error_message=result["stderr"],
                completed_at=completed_at,
                execution_duration_seconds=duration,
                token_usage_input=cost_data["input_tokens"],
                token_usage_output=cost_data["output_tokens"],
                estimated_cost=cost_data["estimated_cost"],
                # current_opencode_session_id keeps the last retry's session ID (even on failure)
            )

            # Use updated state or fall back to original state
            state_to_use = updated_state or state
            self.reporter.post_error(
                state_to_use,
                result["stderr"],
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
        Prefers a full git workspace when possible; falls back to project_root
        so comment/oracle paths never crash with agent_runner is None.
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
                f"using project_root for agent: {e}"
            )
            # Lightweight comment/oracle path — do not use another issue's temp clone
            runner = AgentRunner(working_directory=settings.project_root)
            self._contexts[issue_key] = {"git": None, "runner": runner}
            self.agent_runner = runner
        assert self.agent_runner is not None
        return self.agent_runner

    async def _start_oracle_consultation(self, state: JiraAgentState):
        """Start Oracle consultation."""
        logger.info(f"Starting Oracle consultation for {state.issue_key}")

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

        # Same in-flight fields as plan/execute so /cancel and stuck watchdog
        # can resolve and kill the live agent via current_task_id.
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
            current_task_id=task.task_id,
            current_opencode_session_id=None,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )
        self._mark_jira_in_progress(state.issue_key)

        result = await runner.run_agent(task)

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
        else:
            self._fail_issue(
                state.issue_key,
                result.get("stderr") or "Oracle consultation failed",
                suggestion="Rephrase the architecture question or check agent logs.",
            )
    
    async def _handle_direct_request(self, issue_key: str, request: str):
        """Handle a direct request from comment (does not flip whole-issue ERROR)."""
        state = self.state_manager.get_state(issue_key)

        try:
            runner = self._ensure_agent_runner(issue_key)
        except Exception as e:
            # Soft failure — do not wipe COMPLETED / PLAN_READY / in-flight
            self.reporter.post_comment_response(
                issue_key,
                f"Could not start agent for comment: {e}",
            )
            return
        
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
            # Soft failure: leave issue status unchanged; only reply on the ticket
            err = result.get("stderr") or "Comment response agent failed"
            self.reporter.post_comment_response(
                issue_key,
                f"Could not complete comment request:\n{{code}}\n{err[:1500]}\n{{code}}\n"
                "Retry the @mention or check agent logs.",
            )