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
        self.git_manager: Optional[GitManager] = None
        self.state_manager = JiraStateManager()
        self.reporter = JiraReporter()
        self.agent_runner: Optional[AgentRunner] = None
        
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
        TaskStatus.CODE_REVIEW,
    }
    TERMINAL_STATUSES = {
        TaskStatus.COMPLETED,
        TaskStatus.ERROR,
        TaskStatus.CANCELLED,
    }

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

    async def process_event(self, event: Dict[str, Any]):
        """Process a JIRA webhook/poll event."""
        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")
        
        logger.info(f"Processing event: {event_type} for issue: {issue_key}")
        logger.debug(f"Event data keys: {list(event.keys())}")
        
        try:
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
                triggered_by="webhook",
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
        
        state = self.state_manager.get_state(issue_key)
        
        logger.debug(f"Issue {issue_key} - Event status: '{status_name}', State status: {state.status.value if state else 'NO_STATE'}")

        if not state:
            await self._handle_issue_created(event)
            return

        # Never interrupt or restart in-flight agent work from poll/webhook noise
        if state.status in self.IN_FLIGHT_STATUSES:
            logger.info(
                f"{issue_key} is in-flight ({state.status.value}); ignoring update event"
            )
            return

        # Terminal → reprocess only when Jira status is explicitly TO DO (user reopened)
        if state.status in self.TERMINAL_STATUSES:
            if status_name and status_name.upper() == "TO DO":
                logger.info(
                    f"Reprocessing {issue_key} from terminal state {state.status.value} "
                    f"(Jira status TO DO)"
                )
                self._reset_for_reprocess(issue_key)
                await self._handle_issue_created(event)
            else:
                logger.debug(
                    f"{issue_key} is {state.status.value}; Jira status "
                    f"'{status_name}' is not TO DO — not reprocessing"
                )
            return

        # Non-terminal waiting states (PENDING, PLAN_READY): only restart from TO DO
        # if somehow stuck outside normal paths (PENDING with no active work is rare)
        if status_name and status_name.upper() == "TO DO":
            if state.status == TaskStatus.PENDING:
                # Allow re-kick of PENDING if a create path never started work
                logger.info(f"{issue_key} is PENDING and still TO DO, starting work...")
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
                    "No plan ready for execution. Please wait for planning to complete or create a plan first.",
                )
        
        elif cmd_lower.startswith("/status"):
            # Report current status
            if state:
                status_msg = f"Current status: {state.status.value}\nProgress: {state.progress_percentage}%"
                self.reporter.post_comment_response(issue_key, status_msg)
            else:
                self.reporter.post_comment_response(issue_key, "No active work found for this issue.")
        
        elif cmd_lower.startswith("/cancel"):
            # Always cancel state and notify Jira, even if no live process to kill
            if state and state.current_task_id and self.agent_runner:
                self.agent_runner.cancel_task(state.current_task_id)
            if state:
                self.state_manager.update_state(issue_key, status=TaskStatus.CANCELLED)
                self.reporter.post_comment_response(issue_key, "Work cancelled.")
            else:
                self.reporter.post_comment_response(
                    issue_key, "No active work found to cancel."
                )
        
        else:
            # Treat as a direct request
            await self._handle_direct_request(issue_key, command)
    
    def _init_git_manager(self, issue_key: str) -> GitManager:
        logger.info(f"Initializing git manager for {issue_key}")
        self.git_manager = GitManager(issue_key=issue_key)
        working_dir = self.git_manager.get_working_directory()
        logger.debug(f"Working directory: {working_dir}")
        self.agent_runner = AgentRunner(working_directory=working_dir)
        logger.debug(f"AgentRunner initialized with working directory: {working_dir}")
        return self.git_manager

    async def _start_planning_workflow(self, state: JiraAgentState):
        logger.info(f"Starting planning workflow for {state.issue_key}")
        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")

        assert self.agent_runner is not None, "AgentRunner not initialized"

        workflow_start_time = datetime.now()

        # Create task first
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

        # Update state with task configuration and tracking info
        # current_opencode_session_id will be set when agent outputs it
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.PLANNING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,  # Will be set from agent output
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

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

        def on_retry(attempt_number: int, delay_seconds: float, reason: str, session_file: Optional[str] = None, error_message: Optional[str] = None, return_code: Optional[int] = None, session_id: Optional[str] = None):
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
                self.state_manager.set_state(current_state)

            self.reporter.post_progress_update(
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
                progress_percentage=state.progress_percentage,
            )

        result = await self.agent_runner.run_agent_with_retry(
            task,
            on_output=on_output,
            on_progress=on_progress,
            on_retry=on_retry,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

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

            plan_path = settings.full_plans_dir / f"{state.issue_key}.md"
            plan_content = ""
            if plan_path.exists():
                plan_content = plan_path.read_text()

            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.PLAN_READY,
                plan_path=str(plan_path),
                completed_at=completed_at,
                execution_duration_seconds=duration,
                # current_opencode_session_id keeps the last retry's session ID
            )

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
    
    async def _start_execution_workflow(self, state: JiraAgentState):
        logger.info(f"Starting execution workflow for {state.issue_key}")
        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")

        assert self.agent_runner is not None, "AgentRunner not initialized"

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

        # Update state with task configuration and tracking info
        # current_opencode_session_id will be set when agent outputs it
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,  # Will be set from agent output
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

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

        def on_retry(attempt_number: int, delay_seconds: float, reason: str, session_file: Optional[str] = None, error_message: Optional[str] = None, return_code: Optional[int] = None, session_id: Optional[str] = None):
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
                self.state_manager.set_state(current_state)

            self.reporter.post_progress_update(
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
                progress_percentage=state.progress_percentage,
            )

        result = await self.agent_runner.run_agent_with_retry(
            task,
            on_output=on_output,
            on_progress=on_progress,
            on_retry=on_retry,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

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
            # Push branch to remote and create merge request
            await self._push_and_create_mr(state)

            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
            )

            # Proceed to code review instead of marking completed directly
            await self._start_code_review(
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
    
    async def _start_direct_execution(self, state: JiraAgentState):
        logger.info(f"Starting direct execution workflow for {state.issue_key}")
        logger.debug(f"Using agent: {settings.default_agent}, category: {settings.execution_category}")
        
        git = self._init_git_manager(state.issue_key)
        branch_name = git.ensure_feature_branch(state.issue_key)
        logger.info(f"Feature branch ready: {branch_name}")

        assert self.agent_runner is not None, "AgentRunner not initialized"

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

        # Update state with task configuration and tracking info
        # current_opencode_session_id will be set when agent outputs it
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=workflow_start_time,
            current_task_id=task.task_id,
            current_opencode_session_id=None,  # Will be set from agent output
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )
        
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

        def on_retry(attempt_number: int, delay_seconds: float, reason: str, session_file: Optional[str] = None, error_message: Optional[str] = None, return_code: Optional[int] = None, session_id: Optional[str] = None):
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
                self.state_manager.set_state(current_state)

            self.reporter.post_progress_update(
                self.state_manager.get_state(state.issue_key),
                f"Retrying after {reason} (attempt {attempt_number}/{settings.agent_task_max_retries})",
                progress_percentage=state.progress_percentage,
            )

        result = await self.agent_runner.run_agent_with_retry(
            task,
            on_output=on_output,
            on_progress=on_progress,
            on_retry=on_retry,
            timeout_seconds=settings.agent_task_timeout_seconds,
            max_retries=settings.agent_task_max_retries,
        )

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
            await self._push_and_create_mr(state)

            self.state_manager.update_state(
                state.issue_key,
                execution_duration_seconds=duration,
                token_usage_input=cost_data["input_tokens"],
                token_usage_output=cost_data["output_tokens"],
                estimated_cost=cost_data["estimated_cost"],
            )

            # Proceed to code review instead of marking completed directly
            await self._start_code_review(
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
    
    async def _start_code_review(self, state: JiraAgentState, execution_summary: str = ""):
        """Start code review using oh-my-openagent with a free model.

        This runs after successful execution (EXECUTING → CODE_REVIEW → COMPLETED).
        Uses a different, free model (configured via CODE_REVIEW_MODEL) to review
        the code changes made during execution.
        """
        review_model = settings.code_review_model
        review_agent = settings.code_review_agent

        logger.info(f"Starting code review for {state.issue_key} using model={review_model}, agent={review_agent}")

        # Transition to CODE_REVIEW state
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.CODE_REVIEW,
            code_review_model=review_model,
        )

        # Build the code review prompt
        logger.debug(f"Building code review prompt for {state.issue_key}")
        review_prompt = PromptBuilder.build_code_review_prompt(
            issue_key=state.issue_key,
            summary=state.issue_summary,
            description=state.description,
            review_model=review_model,
        )

        # Create review task with the free model
        review_task = AgentTask(
            description=f"Code Review: {state.issue_key}",
            prompt=review_prompt,
            agent=review_agent,
            issue_key=state.issue_key,
            model=review_model,
            task_type="review",
        )
        logger.debug(f"Review task created: {review_task.description}")

        # Run the review agent (no retry — review is best-effort)
        def on_output(stream: str, line: str):
            """Suppress review output from console — logs go to session file."""
            pass

        review_result = await self.agent_runner.run_agent(
            review_task,
            on_output=on_output,
            timeout_seconds=settings.agent_task_timeout_seconds,
        )

        # Extract review content from the result
        review_text = review_result.get("stdout", "")
        review_succeeded = review_result.get("returncode") == 0

        if not review_succeeded:
            # Review agent failed — log but don't block completion
            review_text = (
                f"Code review could not be completed (return code: {review_result.get('returncode')}).\n"
                f"Error: {review_result.get('stderr', 'unknown error')[:500]}"
            )
            logger.warning(f"Review failed for {state.issue_key}, proceeding to completion anyway")

        # Store review result and transition to COMPLETED
        completed_at = datetime.now()
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.COMPLETED,
            completed_at=completed_at,
            progress_percentage=100,
            code_review_result=review_text[:5000],  # Cap stored review text
            code_review_model=review_model,
        )
        logger.info(f"State updated to COMPLETED for {state.issue_key}")

        # Post review results to JIRA
        try:
            logger.debug(f"Posting code review results to JIRA for {state.issue_key}")
            self.reporter.post_code_review(
                self.state_manager.get_state(state.issue_key),
                review_result=review_text,
                review_model=review_model,
            )
        except Exception as e:
            logger.error(f"Failed to post review to JIRA: {e}")
        
        try:
            mr_url = state.metadata.get("merge_request_url")

            if not mr_url and self.git_manager:
                mr_url = self.git_manager.get_mr_url()

            if mr_url and self.git_manager:
                mr_comment = f"""## Automated Code Review

**Model**: `{review_model}`

{review_text[:2000]}

---
*Code review performed automatically by AI agent. Please verify findings before merging.*
"""
                logger.info(f"Adding review comment to MR: {mr_url}")
                self.git_manager.add_mr_comment(mr_url, mr_comment)
            else:
                logger.warning(f"No MR found for {state.issue_key}, skipping MR comment")
        except Exception as e:
            logger.error(f"Failed to post review to MR: {e}")

        # Post final completion
        logger.info(f"Posting final completion for {state.issue_key}")
        self.reporter.post_completion(
            self.state_manager.get_state(state.issue_key),
            summary=execution_summary,
        )

        logger.info(f"Code review completed for {state.issue_key}")

    async def _push_and_create_mr(self, state: JiraAgentState):
        if not self.git_manager:
            logger.warning(f"No git manager for {state.issue_key}")
            return

        logger.info(f"Starting push and MR creation for {state.issue_key}")

        branch_name = self.git_manager.get_current_branch()

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
            return

        push_success = self.git_manager.push(branch_name)
        if not push_success:
            logger.warning(f"Push failed or remote not configured for {state.issue_key}")
            try:
                self.reporter.post_progress_update(
                    state,
                    "Git push failed or remote is not configured. "
                    "Local work may exist in the temp workspace; no merge request was created.",
                )
            except Exception:
                pass
            return

        commit_subject = self.git_manager.get_last_commit_subject()
        commit_body = self.git_manager.get_last_commit_message()

        if commit_subject:
            mr_title = commit_subject
            mr_body = commit_body if commit_body else commit_subject
        else:
            mr_title = f"[{state.issue_key}] {state.issue_summary}"
            mr_body = state.description or f"Implemented solution for {state.issue_key}"

        target_branch = settings.default_branch.strip() if settings.default_branch else "main"
        mr_url = self.git_manager.create_merge_request(
            title=mr_title,
            body=mr_body,
            target_branch=target_branch,
        )

        if mr_url:
            logger.info(f"Merge request created: {mr_url}")
            # Merge into existing metadata (do not wipe workflow_type, etc.)
            self.state_manager.update_state(
                state.issue_key,
                metadata={"merge_request_url": mr_url},
            )
        else:
            logger.warning(f"Could not create merge request for {state.issue_key}")
            try:
                self.reporter.post_progress_update(
                    state,
                    "Could not create a merge request. The branch may be pushed without an MR link.",
                )
            except Exception:
                pass

    def _ensure_agent_runner(self, issue_key: str) -> AgentRunner:
        """Ensure an AgentRunner exists for lightweight paths (oracle/comments).

        Prefers a full git workspace when possible; falls back to project_root
        so comment/oracle paths never crash with agent_runner is None.
        """
        if self.agent_runner is not None:
            return self.agent_runner
        try:
            self._init_git_manager(issue_key)
        except Exception as e:
            logger.warning(
                f"Git workspace init failed for {issue_key}, "
                f"using project_root for agent: {e}"
            )
            self.agent_runner = AgentRunner(working_directory=settings.project_root)
        assert self.agent_runner is not None
        return self.agent_runner

    async def _start_oracle_consultation(self, state: JiraAgentState):
        """Start Oracle consultation."""
        logger.info(f"Starting Oracle consultation for {state.issue_key}")

        self._ensure_agent_runner(state.issue_key)
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
        )
        
        prompt = PromptBuilder.build_oracle_consult_prompt(
            question=state.description,
        )
        
        task = AgentTask(
            description=f"Consult: {state.issue_key}",
            prompt=prompt,
            agent="oracle",
            issue_key=state.issue_key,
        )
        
        result = await self.agent_runner.run_agent(task)
        
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
            )
        else:
            self._fail_issue(
                state.issue_key,
                result.get("stderr") or "Oracle consultation failed",
                suggestion="Rephrase the architecture question or check agent logs.",
            )
    
    async def _handle_direct_request(self, issue_key: str, request: str):
        """Handle a direct request from comment."""
        state = self.state_manager.get_state(issue_key)

        try:
            self._ensure_agent_runner(issue_key)
        except Exception as e:
            self._fail_issue(issue_key, f"Could not start agent for comment: {e}")
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
        
        result = await self.agent_runner.run_agent(task)
        
        if result["returncode"] == 0:
            self.reporter.post_comment_response(issue_key, result["stdout"])
        else:
            self._fail_issue(
                issue_key,
                result.get("stderr") or "Comment response agent failed",
                suggestion="Retry the @mention command or check agent logs.",
            )