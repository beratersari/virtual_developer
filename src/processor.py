"""Job processor for handling JIRA events."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings
from src.git_manager import GitManager
from src.jira.client import JiraClient, create_jira_client
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, RetryAttempt, TaskStatus


class JobProcessor:
    """Processes JIRA events and manages agent workflows."""
    
    def __init__(self):
        self.state_manager = JiraStateManager()
        self.reporter = JiraReporter()
        self.agent_runner = AgentRunner()
        self.git_manager = GitManager()
        # Use simulated client if JIRA not properly configured
        use_simulated = not settings.is_configured() or settings.jira_host in ['', 'a', 'https://yourcompany.atlassian.net']
        if use_simulated:
            print("[Processor] Using simulated JIRA client")
        self.jira_client = create_jira_client(simulated=use_simulated)
    
    async def process_event(self, event: Dict[str, Any]):
        """Process a JIRA webhook/poll event."""
        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")
        
        # Process event (details logged to state file)
        if event_type == "jira:issue_created":
            await self._handle_issue_created(event)
        elif event_type == "jira:issue_updated":
            await self._handle_issue_updated(event)
        elif event_type == "comment_created":
            await self._handle_comment_created(event)
    
    async def _handle_issue_created(self, event: Dict[str, Any]):
        """Handle new issue creation."""
        issue = event.get("issue", {})
        issue_key = issue.get("key", "unknown")
        fields = issue.get("fields", {})
        
        # Check if already processing
        existing = self.state_manager.get_state(issue_key)
        if existing:
            # Allow reprocessing if previous run completed, failed, or was cancelled
            terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
            if existing.status in terminal_states:
                # Reset state for reprocessing instead of deleting
                self.state_manager.update_state(
                    issue_key,
                    status=TaskStatus.PENDING,
                    progress_percentage=0,
                    error_message=None,
                    current_task_id=None,
                    current_opencode_session_id=None,
                )
            else:
                return
        
        # Extract issue details
        summary = fields.get("summary", "")
        description = fields.get("description", "") or ""
        assignee_data = fields.get("assignee")
        assignee = assignee_data.get("displayName") if assignee_data else None
        
        # Determine workflow
        workflow_type = WorkflowRouter.route_issue(issue_key, summary, description)
        
        # Create state
        state = self.state_manager.create_state(
            issue_key=issue_key,
            issue_summary=summary,
            description=description,
            triggered_by="webhook",
            jira_assignee=assignee,
        )
        state.metadata["workflow_type"] = workflow_type.value
        self.state_manager.set_state(state)
        
        # Post acknowledgment
        try:
            self.reporter.post_initial_acknowledgment(state)
        except Exception as e:
            pass
        
        # Route to appropriate handler
        if workflow_type == WorkflowType.PLANNING:
            await self._start_planning_workflow(state)
        elif workflow_type == WorkflowType.DIRECT_EXECUTION:
            await self._start_direct_execution(state)
        elif workflow_type == WorkflowType.ORACLE_CONSULT:
            await self._start_oracle_consultation(state)
    
    async def _handle_issue_updated(self, event: Dict[str, Any]):
        """Handle issue updates (labels, assignee changes)."""
        issue = event["issue"]
        issue_key = issue["key"]
        
        # Check if we should start processing
        state = self.state_manager.get_state(issue_key)
        if not state:
            # New trigger - treat as creation
            await self._handle_issue_created(event)
    
    async def _handle_comment_created(self, event: Dict[str, Any]):
        """Handle new comments (for @mentions)."""
        issue = event.get("issue", {})
        comment = event.get("comment", {})
        
        issue_key = issue.get("key")
        comment_body = comment.get("body", "")
        
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
                await self._start_execution_workflow(state)
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
            # Cancel current work
            if state and state.current_task_id:
                self.agent_runner.cancel_task(state.current_task_id)
                self.state_manager.update_state(issue_key, status=TaskStatus.CANCELLED)
                self.reporter.post_comment_response(issue_key, "Work cancelled.")
        
        else:
            # Treat as a direct request
            await self._handle_direct_request(issue_key, command)
    
    async def _start_planning_workflow(self, state: JiraAgentState):
        """Start Prometheus planning workflow."""
        # Workflow started (logs go to state file)

        # Ensure we're on a feature branch before making changes
        self.git_manager.ensure_feature_branch(state.issue_key)

        # Track actual workflow start time (before any retries)
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
            # Plan created successfully — commit it
            self.git_manager.commit_changes(
                issue_key=state.issue_key,
                summary=f"Plan for {state.issue_key}",
                description=state.description,
            )

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
        """Start Atlas execution workflow."""
        # Execution workflow started (logs go to state file)

        # Ensure we're on a feature branch before making changes
        self.git_manager.ensure_feature_branch(state.issue_key)

        # Track actual workflow start time (before any retries)
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
            # Commit changes made by Atlas
            self.git_manager.commit_changes(
                issue_key=state.issue_key,
                summary=f"Execution for {state.issue_key}",
                description=state.description,
            )

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
        """Start direct Sisyphus execution."""
        # Direct execution started (logs go to state file)

        # Ensure we're on a feature branch before making changes
        self.git_manager.ensure_feature_branch(state.issue_key)

        # Track actual workflow start time (before any retries)
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
            # Commit changes to the feature branch
            self.git_manager.commit_changes(
                issue_key=state.issue_key,
                summary=state.issue_summary,
                description=state.description,
            )

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

        print(f"[CodeReview] Starting code review for {state.issue_key} using model={review_model}, agent={review_agent}")

        # Transition to CODE_REVIEW state
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.CODE_REVIEW,
            code_review_model=review_model,
        )

        # Build the code review prompt
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
            model=review_model,  # Use the free model for review
        )

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
            print(f"[CodeReview] Review failed for {state.issue_key}, proceeding to completion anyway")

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

        # Post review results to JIRA
        try:
            self.reporter.post_code_review(
                self.state_manager.get_state(state.issue_key),
                review_result=review_text,
                review_model=review_model,
            )
        except Exception as e:
            print(f"[CodeReview] Failed to post review to JIRA: {e}")

        # Post final completion
        self.reporter.post_completion(
            self.state_manager.get_state(state.issue_key),
            summary=execution_summary,
        )

        print(f"[CodeReview] Code review completed for {state.issue_key}")

    async def _start_oracle_consultation(self, state: JiraAgentState):
        """Start Oracle consultation."""
        print(f"Starting Oracle consultation for {state.issue_key}")
        
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
            )
    
    async def _handle_direct_request(self, issue_key: str, request: str):
        """Handle a direct request from comment."""
        state = self.state_manager.get_state(issue_key)
        
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
