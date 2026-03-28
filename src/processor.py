"""Job processor for handling JIRA events."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings
from src.jira.client import JiraClient, create_jira_client
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.prompt_builder import PromptBuilder
from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
from src.reporter.jira_reporter import JiraReporter
from src.state.manager import JiraStateManager
from src.state.models import JiraAgentState, TaskStatus


class JobProcessor:
    """Processes JIRA events and manages agent workflows."""
    
    def __init__(self):
        self.state_manager = JiraStateManager()
        self.reporter = JiraReporter()
        self.agent_runner = AgentRunner()
        # Use simulated client if JIRA not properly configured
        use_simulated = not settings.is_configured() or settings.jira_host in ['', 'a', 'https://yourcompany.atlassian.net']
        if use_simulated:
            print("[Processor] Using simulated JIRA client")
        self.jira_client = create_jira_client(simulated=use_simulated)
    
    async def process_event(self, event: Dict[str, Any]):
        """Process a JIRA webhook/poll event."""
        event_type = event.get("webhookEvent", "")
        issue_key = event.get("issue", {}).get("key", "unknown")
        
        print(f"[Processor] Received event: {event_type} for issue {issue_key}")
        
        if event_type == "jira:issue_created":
            await self._handle_issue_created(event)
        elif event_type == "jira:issue_updated":
            await self._handle_issue_updated(event)
        elif event_type == "comment_created":
            await self._handle_comment_created(event)
        else:
            print(f"[Processor] Unknown event type: {event_type}")
    
    async def _handle_issue_created(self, event: Dict[str, Any]):
        """Handle new issue creation."""
        print(f"[Processor] Handling issue created event")
        
        issue = event.get("issue", {})
        issue_key = issue.get("key", "unknown")
        fields = issue.get("fields", {})
        
        print(f"[Processor] Issue key: {issue_key}")
        print(f"[Processor] Fields: {list(fields.keys())}")
        
        # Check if already processing
        existing = self.state_manager.get_state(issue_key)
        if existing:
            # Allow reprocessing if previous run completed, failed, or was cancelled
            terminal_states = {TaskStatus.COMPLETED, TaskStatus.ERROR, TaskStatus.CANCELLED}
            if existing.status in terminal_states:
                print(f"[Processor] Issue {issue_key} was {existing.status.value}, will reprocess")
                # Reset state for reprocessing instead of deleting
                self.state_manager.update_state(
                    issue_key,
                    status=TaskStatus.PENDING,
                    progress_percentage=0,
                    error_message=None,
                    current_task_id=None,
                    current_session_id=None,
                )
            else:
                print(f"[Processor] Issue {issue_key} already being processed (status: {existing.status.value})")
                return
        
        # Extract issue details
        summary = fields.get("summary", "")
        description = fields.get("description", "") or ""
        assignee_data = fields.get("assignee")
        assignee = assignee_data.get("displayName") if assignee_data else None
        
        print(f"[Processor] Summary: {summary[:50]}...")
        print(f"[Processor] Assignee: {assignee}")
        
        # Determine workflow
        workflow_type = WorkflowRouter.route_issue(issue_key, summary, description)
        print(f"[Processor] Workflow type: {workflow_type.value}")
        
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
        print(f"[Processor] State created for {issue_key}")
        
        # Post acknowledgment
        try:
            self.reporter.post_initial_acknowledgment(state)
            print(f"[Processor] Acknowledgment posted for {issue_key}")
        except Exception as e:
            print(f"[Processor] Error posting acknowledgment: {e}")
        
        # Route to appropriate handler
        try:
            if workflow_type == WorkflowType.PLANNING:
                await self._start_planning_workflow(state)
            elif workflow_type == WorkflowType.DIRECT_EXECUTION:
                await self._start_direct_execution(state)
            elif workflow_type == WorkflowType.ORACLE_CONSULT:
                await self._start_oracle_consultation(state)
        except Exception as e:
            print(f"[Processor] Error in workflow: {e}")
            import traceback
            traceback.print_exc()
    
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
        print(f"Starting planning workflow for {state.issue_key}")
        
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.PLANNING,
            started_at=datetime.now(),
        )
        
        # Build prompt for Prometheus
        prompt = PromptBuilder.build_prometheus_prompt(
            issue_key=state.issue_key,
            summary=state.issue_summary,
            description=state.description,
        )
        
        # Create task
        task = AgentTask(
            description=f"Plan: {state.issue_key}",
            prompt=prompt,
            agent=settings.planning_agent,
            issue_key=state.issue_key,
        )
        
        # Run agent with progress tracking
        def on_progress(percentage: int, message: str):
            """Update progress in state."""
            print(f"[Progress] {percentage}% - {message[:50]}...")
            self.state_manager.update_state(
                state.issue_key,
                progress_percentage=percentage,
            )
        
        result = await self.agent_runner.run_agent(
            task,
            on_output=lambda stream, line: print(f"[{stream}] {line}"),
            on_progress=on_progress,
        )
        
        # Check result
        if result["returncode"] == 0:
            # Plan created successfully
            plan_path = settings.full_plans_dir / f"{state.issue_key}.md"
            plan_content = ""
            if plan_path.exists():
                plan_content = plan_path.read_text()
            
            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.PLAN_READY,
                plan_path=str(plan_path),
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
            )
            self.reporter.post_error(
                self.state_manager.get_state(state.issue_key),
                result["stderr"],
            )
    
    async def _start_execution_workflow(self, state: JiraAgentState):
        """Start Atlas execution workflow."""
        print(f"Starting execution workflow for {state.issue_key}")
        
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
        )
        
        # Build prompt for Atlas
        prompt = PromptBuilder.build_atlas_prompt(
            issue_key=state.issue_key,
            plan_path=state.plan_path or "",
        )
        
        # Create task
        task = AgentTask(
            description=f"Execute: {state.issue_key}",
            prompt=prompt,
            agent=settings.orchestrator_agent,
            issue_key=state.issue_key,
        )
        
        # Run agent with progress tracking
        def on_progress(percentage: int, message: str):
            """Update progress in state."""
            print(f"[Progress] {percentage}% - {message[:50]}...")
            self.state_manager.update_state(
                state.issue_key,
                progress_percentage=percentage,
            )
        
        result = await self.agent_runner.run_agent(
            task,
            on_output=lambda stream, line: print(f"[{stream}] {line}"),
            on_progress=on_progress,
        )
        
        # Check result
        if result["returncode"] == 0:
            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.COMPLETED,
                completed_at=datetime.now(),
                progress_percentage=100,
            )
            
            self.reporter.post_completion(
                self.state_manager.get_state(state.issue_key),
                summary="All tasks completed successfully.",
            )
        else:
            self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.ERROR,
                error_message=result["stderr"],
            )
            self.reporter.post_error(
                self.state_manager.get_state(state.issue_key),
                result["stderr"],
            )
    
    async def _start_direct_execution(self, state: JiraAgentState):
        """Start direct Sisyphus execution."""
        print(f"Starting direct execution for {state.issue_key}")
        
        self.state_manager.update_state(
            state.issue_key,
            status=TaskStatus.EXECUTING,
            started_at=datetime.now(),
        )
        
        # Build prompt
        prompt = PromptBuilder.build_sisyphus_prompt(
            issue_key=state.issue_key,
            task_description=state.description,
        )
        
        # Create task with category
        task = AgentTask(
            description=f"Direct: {state.issue_key}",
            prompt=prompt,
            agent=settings.default_agent,
            category=settings.execution_category,
            issue_key=state.issue_key,
        )
        
        # Run agent with progress tracking
        def on_progress(percentage: int, message: str):
            """Update progress in state."""
            print(f"[Progress] {percentage}% - {message[:50]}...")
            self.state_manager.update_state(
                state.issue_key,
                progress_percentage=percentage,
            )
        
        result = await self.agent_runner.run_agent(
            task,
            on_output=lambda stream, line: print(f"[{stream}] {line}"),
            on_progress=on_progress,
        )
        
        # Calculate cost and timing
        from src.shared.cost_calculator import calculate_cost, format_cost_report
        
        duration = (datetime.now() - state.started_at).total_seconds() if state.started_at else 0
        cost_data = calculate_cost(
            input_text=task.prompt,
            output_text=result.get("stdout", ""),
            model=settings.execution_category,
        )
        
        print(f"\n[Timing] Execution took {duration:.1f} seconds")
        print(format_cost_report(cost_data))
        
        # Update state with cost info
        self.state_manager.update_state(
            state.issue_key,
            token_usage_input=cost_data["input_tokens"],
            token_usage_output=cost_data["output_tokens"],
            estimated_cost=cost_data["estimated_cost"],
            execution_duration_seconds=duration,
        )
        
        # Handle result
        if result["returncode"] == 0:
            updated_state = self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.COMPLETED,
                completed_at=datetime.now(),
                progress_percentage=100,
            )
            
            # Use updated state or fall back to original state
            state_to_use = updated_state or state
            self.reporter.post_completion(
                state_to_use,
                summary=result["stdout"][:1000],  # First 1000 chars
            )
        else:
            updated_state = self.state_manager.update_state(
                state.issue_key,
                status=TaskStatus.ERROR,
                error_message=result["stderr"],
            )
            
            # Use updated state or fall back to original state
            state_to_use = updated_state or state
            self.reporter.post_error(
                state_to_use,
                result["stderr"],
            )
    
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
