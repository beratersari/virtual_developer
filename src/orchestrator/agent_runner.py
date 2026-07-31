"""Agent runner that interfaces with Oh My OpenAgent via CLI."""

import asyncio
import json
import platform
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.logger import logger


# Check if running on Windows
IS_WINDOWS = platform.system() == "Windows"


@dataclass
class AgentTask:
    """Represents an agent task to be executed."""
    description: str
    prompt: str
    agent: str
    category: Optional[str] = None
    issue_key: Optional[str] = None
    session_id: Optional[str] = None
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    skills: List[str] = field(default_factory=list)
    model: Optional[str] = None
    task_type: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "agent": self.agent,
            "category": self.category,
            "prompt": self.prompt,
            "skills": self.skills,
            "session_id": self.session_id,
        }


class AgentRunner:
    """Runs agents using Oh My OpenAgent CLI."""

    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory
        self.opencode_cli = settings.opencode_cli
        self._running_tasks: Dict[str, asyncio.Task] = {}
        logger.debug(f"AgentRunner initialized with working_directory={working_directory}")
    
    async def run_agent(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
        timeout_seconds: Optional[int] = None,
        attempt_number: int = 0,
    ) -> Dict[str, Any]:
        """Run an agent task asynchronously.

        Args:
            task: The agent task to run
            on_output: Callback for output lines (stream, line)
            on_complete: Callback when complete (result)
            on_progress: Callback for progress updates (percentage, message)
            timeout_seconds: Override timeout from settings (None uses config default)
            attempt_number: The retry attempt number (0 = first attempt)
        """
        logger.info(f"Starting agent task: task_id={task.task_id}, agent={task.agent}, attempt={attempt_number}")
        
        # Use configured timeout if not overridden
        effective_timeout = timeout_seconds or settings.agent_task_timeout_seconds
        start_time = asyncio.get_event_loop().time()
        logger.debug(f"Effective timeout: {effective_timeout}s")

        # Create session file for this task with naming convention: JIRAID_DATETIME_RETRYCOUNT
        session_file = self._get_session_file(
            task.task_id,
            issue_key=task.issue_key,
            attempt_number=attempt_number,
            task_type=task.task_type,
        )
        session_file.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Session file created: {session_file}")
        
        # Build the command as a list (cross-platform)
        cmd_list = self._build_command(task, session_file)
        logger.debug(f"Command built with {len(cmd_list)} parts: {' '.join(cmd_list[:3])}...")
        
        # Open session file for writing output
        with open(session_file, 'w', encoding='utf-8') as session_fh:
            # Run the process using exec (no shell) for cross-platform compatibility
            # On Windows, we need to use shell=False and handle the command differently
            if IS_WINDOWS:
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                )
            
            stdout_lines = []
            stderr_lines = []
            last_progress = 0
            
            # Read output streams with progress tracking and timeout check
            async def read_stream(stream, lines, callback_name, file_handle):
                nonlocal last_progress
                while True:
                    # Check timeout
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > effective_timeout:
                        raise asyncio.TimeoutError(
                            f"Task exceeded timeout of {effective_timeout} seconds"
                        )

                    try:
                        line = await asyncio.wait_for(
                            stream.readline(),
                            timeout=1.0  # 1 second check interval
                        )
                    except asyncio.TimeoutError:
                        # No data available, check timeout and continue
                        continue

                    if not line:
                        break
                    decoded = line.decode('utf-8', errors='replace').rstrip()
                    lines.append(decoded)

                    # Write to session file
                    file_handle.write(decoded + '\n')
                    file_handle.flush()

                    # Parse progress from output
                    progress = self._parse_progress(decoded)
                    if progress and progress != last_progress:
                        last_progress = progress
                        if on_progress:
                            on_progress(progress, decoded[:100])

                    if on_output:
                        on_output(callback_name, decoded)

            try:
                logger.info(f"Waiting for agent process to complete, timeout={effective_timeout}s")
                # Wait for completion with timeout
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(process.stdout, stdout_lines, "stdout", session_fh),
                        read_stream(process.stderr, stderr_lines, "stderr", session_fh),
                    ),
                    timeout=effective_timeout
                )
                returncode = await process.wait()
                elapsed = asyncio.get_event_loop().time() - start_time
                logger.info(f"Agent process completed: returncode={returncode}, elapsed={elapsed:.2f}s, stdout_lines={len(stdout_lines)}, stderr_lines={len(stderr_lines)}")
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - start_time
                logger.error(f"Agent task timed out after {elapsed:.2f}s (limit={effective_timeout}s)")
                # Kill the process on timeout
                process.kill()
                await process.wait()
                logger.info(f"Killed timed out process: task_id={task.task_id}")
                # Extract session ID from output collected so far
                all_output_lines = stdout_lines + stderr_lines
                session_id = self._parse_session_id(all_output_lines)
                logger.debug(f"Extracted session ID from partial output: {session_id}")
                return {
                    "task_id": task.task_id,
                    "returncode": -1,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": f"\n[TIMEOUT] Task exceeded {effective_timeout} seconds",
                    "session_file": str(session_file),
                    "opencode_session_id": session_id,  # Include session ID even on timeout
                    "progress": last_progress,
                    "timed_out": True,
                }
        
        # Extract session ID from output
        all_output_lines = stdout_lines + stderr_lines
        session_id = self._parse_session_id(all_output_lines)
        logger.debug(f"Extracted session ID: {session_id}")

        elapsed = asyncio.get_event_loop().time() - start_time
        result = {
            "task_id": task.task_id,
            "returncode": returncode,
            "stdout": "\n".join(stdout_lines),
            "stderr": "\n".join(stderr_lines),
            "session_file": str(session_file),
            "opencode_session_id": session_id,  # opencode session ID from CLI output
            "progress": 100 if returncode == 0 else last_progress,
        }
        
        if returncode == 0:
            logger.info(f"Agent task completed successfully: task_id={task.task_id}, duration={elapsed:.2f}s, progress=100%")
        else:
            logger.warning(f"Agent task failed: task_id={task.task_id}, returncode={returncode}, duration={elapsed:.2f}s")

        if on_complete:
            on_complete(result)

        return result
    
    def _parse_progress(self, line: str) -> Optional[int]:
        """Parse progress percentage from agent output.

        Looks for patterns like:
        - "Progress: 75%"
        - "[███████░░░] 70%"
        - "Completed: 8/10 tasks (80%)"
        """
        import re

        # Pattern 1: Direct percentage (e.g., "Progress: 75%" or "75%")
        match = re.search(r'(\d+)%', line)
        if match:
            return int(match.group(1))

        # Pattern 2: Progress bar blocks
        # Count filled vs empty blocks
        filled_blocks = line.count('█') + line.count('▓') + line.count('■')
        empty_blocks = line.count('░') + line.count('▒') + line.count(' ')
        total_blocks = filled_blocks + empty_blocks
        if total_blocks >= 5 and filled_blocks > 0:
            return int((filled_blocks / total_blocks) * 100)

        # Pattern 3: Completed tasks (e.g., "Completed: 8/10 tasks")
        match = re.search(r'(\d+)\s*/\s*(\d+)\s*(?:tasks|steps|items)', line, re.IGNORECASE)
        if match:
            completed, total = int(match.group(1)), int(match.group(2))
            if total > 0:
                return int((completed / total) * 100)

        return None

    def _parse_session_id(self, lines: List[str]) -> Optional[str]:
        """Parse opencode session ID from output lines.

        Looks for patterns like:
        - "Session: ses_abc123" (actual format from oh-my-opencode)
        - "Session ID: ses_abc123"
        - "session: ses_abc123"
        """
        import re

        # Actual format seen in logs: "Session: ses_2c996b381ffe22SXIwktVa9kc7"
        patterns = [
            r'Session[:\s]+(ses_[a-zA-Z0-9_-]+)',
            r'Session\s*ID[:\s]+(ses_[a-zA-Z0-9_-]+)',
            r'session[:\s]+(ses_[a-zA-Z0-9_-]+)',
        ]

        for line in lines:
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    return match.group(1)

        return None
    
    def _build_command(self, task: AgentTask, session_file: Path) -> List[str]:
        """Build the opencode CLI command as a list (cross-platform).
        
        Command format: bunx oh-my-opencode run [options] <message>
        The message must be the last argument.
        
        Returns:
            List of command arguments for use with subprocess (no shell needed)
        """
        logger.debug(f"Building command for task: agent={task.agent}, model={task.model or settings.default_model}, session_id={task.session_id}")
        
        # Build base command
        cmd_parts = self.opencode_cli.split() + ["run"]
        
        # Add agent option
        cmd_parts.extend(["--agent", task.agent])
        
        # Use task-specific model if provided, otherwise use configured default
        effective_model = task.model or settings.default_model
        cmd_parts.extend(["--model", effective_model])
        
        # Add session continuation if specified
        if task.session_id:
            cmd_parts.extend(["--session-id", task.session_id])
        
        # Add the prompt as the final argument
        cmd_parts.append(task.prompt)
        
        return cmd_parts
    
    def _build_shell_command(self, task: AgentTask, session_file: Path) -> str:
        """Build shell command with redirection (fallback for compatibility).
        
        Note: This method is kept for backwards compatibility but _build_command
        is preferred for cross-platform support.
        """
        cmd_list = self._build_command(task, session_file)
        
        if IS_WINDOWS:
            # Windows shell escaping
            escaped_parts = []
            for part in cmd_list:
                if ' ' in part or '"' in part:
                    # Escape quotes and wrap in quotes
                    escaped = part.replace('"', '"""')
                    escaped_parts.append(f'"{escaped}"')
                else:
                    escaped_parts.append(part)
            cmd_str = ' '.join(escaped_parts)
            # Windows redirection
            session_file_str = str(session_file).replace('"', '"""')
            return f'{cmd_str} > "{session_file_str}" 2>&1'
        else:
            # Unix shell escaping using shlex
            cmd_str = ' '.join(shlex.quote(part) for part in cmd_list)
            session_file_str = shlex.quote(str(session_file))
            return f'{cmd_str} > {session_file_str} 2>&1'
    
    def _get_session_file(
        self,
        task_id: str,
        issue_key: Optional[str] = None,
        attempt_number: int = 0,
        task_type: Optional[str] = None,
    ) -> Path:
        """Get path to session output file.

        Args:
            task_id: The task ID
            issue_key: The JIRA issue key (e.g., "PROJ-123")
            attempt_number: The retry attempt number (0 = first attempt)
            task_type: Optional task type prefix (e.g., "review" for code review)

        Returns:
            Path to the session log file

        Naming convention:
            - Normal task: PROJ-123_20240327_143052_0.log
            - Code review: PROJ-123_review_20240327_143052_0.log
            - Retry 1: PROJ-123_20240327_143052_1.log
        """
        # Ensure directory exists
        sessions_dir = (Path.cwd() / ".jira-agent" / "sessions").resolve()
        sessions_dir.mkdir(parents=True, exist_ok=True)
        
        logger.debug(f"Generating session file path: task_id={task_id}, issue_key={issue_key}, attempt={attempt_number}, task_type={task_type}")

        if issue_key:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if task_type:
                # Format: JIRAID_TYPE_DATETIME_RETRYCOUNT.log
                filename = f"{issue_key}_{task_type}_{timestamp}_{attempt_number}.log"
            else:
                # Format: JIRAID_DATETIME_RETRYCOUNT.log
                filename = f"{issue_key}_{timestamp}_{attempt_number}.log"
        else:
            # Fallback to task_id if no issue_key provided
            filename = f"{task_id}.log"

        path = sessions_dir / filename
        logger.debug(f"Generated session file path: {path}")
        return path
    
    async def run_background_agent(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
    ) -> str:
        """Start a background agent and return task ID."""
        # Similar to run_agent but non-blocking
        # Returns immediately with task ID for polling
        logger.info(f"Starting background agent: task_id={task.task_id}, agent={task.agent}")
        
        session_file = self._get_session_file(task.task_id)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Background agent session file: {session_file}")
        
        cmd_list = self._build_command(task, session_file)
        
        if IS_WINDOWS:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.working_directory,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.working_directory,
            )
        
        # Store for later monitoring
        self._running_tasks[task.task_id] = process
        logger.info(f"Background agent started: task_id={task.task_id}, pid={process.pid}")
        
        return task.task_id
    
    async def check_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Check status of a running background task."""
        process = self._running_tasks.get(task_id)
        if not process:
            logger.debug(f"Task not found in running tasks: {task_id}")
            return None
        
        # Check if process has completed
        if process.returncode is not None:
            del self._running_tasks[task_id]
            status = "completed" if process.returncode == 0 else "error"
            logger.info(f"Background task completed: task_id={task_id}, status={status}, returncode={process.returncode}")
            return {
                "task_id": task_id,
                "status": status,
                "returncode": process.returncode,
            }
        
        logger.debug(f"Background task still running: task_id={task_id}")
        return {
            "task_id": task_id,
            "status": "running",
        }
    
    def read_session_output(self, task_id: str) -> str:
        """Read the output from a session file."""
        session_file = self._get_session_file(task_id)
        logger.debug(f"Reading session output: task_id={task_id}, file={session_file}")
        if session_file.exists():
            content = session_file.read_text()
            logger.debug(f"Read {len(content)} chars from session file: {session_file}")
            return content
        logger.debug(f"Session file not found: {session_file}")
        return ""
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        process = self._running_tasks.get(task_id)
        if process and process.returncode is None:
            logger.info(f"Cancelling running task: task_id={task_id}")
            if IS_WINDOWS:
                # On Windows, terminate() sends CTRL_BREAK_EVENT to the process group
                # when CREATE_NEW_PROCESS_GROUP was used
                try:
                    process.terminate()
                    logger.info(f"Task terminated: task_id={task_id}")
                except Exception:
                    # Fallback: kill if terminate doesn't work
                    try:
                        process.kill()
                        logger.warning(f"Task killed after terminate failed: task_id={task_id}")
                    except Exception:
                        logger.error(f"Failed to cancel task: task_id={task_id}")
                        pass
            else:
                # Unix/Linux/Mac: terminate() sends SIGTERM
                process.terminate()
                logger.info(f"Task terminated: task_id={task_id}")
            return True
        logger.debug(f"Task not found or already completed: task_id={task_id}")
        return False

    async def run_agent_with_retry(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
        on_retry: Optional[callable] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run an agent task with automatic retry on failure.

        Args:
            task: The agent task to run
            on_output: Callback for output lines (stream, line)
            on_complete: Callback when complete (result)
            on_progress: Callback for progress updates (percentage, message)
            on_retry: Callback when a retry is attempted (attempt_number, delay_seconds, reason, session_file)
            timeout_seconds: Override timeout from settings
            max_retries: Override max retries from settings

        Returns:
            Dict with task result including retry information and all session files
        """
        effective_max_retries = max_retries or settings.agent_task_max_retries
        retry_delay = settings.agent_task_retry_delay_seconds
        backoff_multiplier = settings.agent_task_retry_backoff_multiplier
        retry_on_timeout = settings.agent_task_retry_on_timeout
        retry_on_error = settings.agent_task_retry_on_error
        
        logger.info(f"Starting agent with retry: task_id={task.task_id}, max_retries={effective_max_retries}")

        last_result = None
        last_session_id = None
        attempt = 0
        all_session_files = []

        while attempt <= effective_max_retries:
            logger.info(f"Agent attempt {attempt + 1}/{effective_max_retries + 1}: task_id={task.task_id}")
            
            # Run the task with attempt number (0 = first attempt, 1+ = retries)
            result = await self.run_agent(
                task,
                on_output=on_output,
                on_complete=on_complete,
                on_progress=on_progress,
                timeout_seconds=timeout_seconds,
                attempt_number=attempt,
            )

            # Track session file and opencode session ID for this attempt
            if result.get("session_file"):
                all_session_files.append(result["session_file"])
                logger.debug(f"Added session file to tracking: {result['session_file']}")
            if result.get("opencode_session_id"):
                last_session_id = result["opencode_session_id"]

            # Check if successful
            if result.get("returncode") == 0:
                logger.info(f"Agent succeeded on attempt {attempt + 1}: task_id={task.task_id}")
                result["retry_info"] = {
                    "attempts": attempt + 1,
                    "max_retries": effective_max_retries,
                    "retried": attempt > 0,
                    "all_session_files": all_session_files,
                    "last_opencode_session_id": last_session_id,  # opencode session ID from last attempt
                }
                return result

            # Determine if we should retry
            should_retry = False
            retry_reason = ""

            if result.get("timed_out"):
                if retry_on_timeout and attempt < effective_max_retries:
                    should_retry = True
                    retry_reason = "timeout"
                    logger.warning(f"Agent timed out on attempt {attempt + 1}, will retry: task_id={task.task_id}")
            elif retry_on_error and attempt < effective_max_retries:
                should_retry = True
                retry_reason = "error"
                logger.warning(f"Agent failed with error on attempt {attempt + 1}, will retry: task_id={task.task_id}, returncode={result.get('returncode')}")
            else:
                logger.error(f"Agent failed and no more retries allowed: task_id={task.task_id}, attempt={attempt + 1}")

            if should_retry:
                attempt += 1

                # Calculate delay with exponential backoff
                delay = retry_delay * (backoff_multiplier ** (attempt - 1))

                # Extract error details from the failed attempt
                error_message = result.get("stderr", "") if result.get("returncode") != 0 else None
                return_code = result.get("returncode")

                if on_retry:
                    on_retry(attempt, delay, retry_reason, result.get("session_file"), error_message, return_code, result.get("opencode_session_id"))

                # Log retry attempt
                logger.warning(f"{retry_reason.capitalize()} on attempt {attempt}/{effective_max_retries} for {task.task_id}, retrying in {delay:.1f}s...")

                # Wait before retry
                await asyncio.sleep(delay)

                # Create new task ID for retry
                old_task_id = task.task_id
                task.task_id = f"task_{uuid.uuid4().hex[:8]}"
                logger.debug(f"Created new task ID for retry: old={old_task_id}, new={task.task_id}")

                last_result = result
            else:
                # No more retries - include session ID from last attempt
                logger.info(f"All retry attempts exhausted: task_id={task.task_id}, total_attempts={attempt + 1}")
                result["retry_info"] = {
                    "attempts": attempt + 1,
                    "max_retries": effective_max_retries,
                    "retried": attempt > 0,
                    "final_failure": True,
                    "all_session_files": all_session_files,
                    "last_opencode_session_id": last_session_id,  # opencode session ID from last attempt
                }
                return result

        # Should not reach here, but just in case
        logger.error(f"Unexpected fallback reached in run_agent_with_retry: task_id={task.task_id}")
        if last_result:
            last_result["retry_info"] = {
                "attempts": attempt + 1,
                "max_retries": effective_max_retries,
                "retried": True,
                "final_failure": True,
                "all_session_files": all_session_files,
                "last_opencode_session_id": last_session_id,
            }
            logger.warning(f"Returning last result due to unexpected state: task_id={task.task_id}")
            return last_result

        logger.error(f"Max retries exceeded with no last result: task_id={task.task_id}")
        return {
            "task_id": task.task_id,
            "returncode": -1,
            "stdout": "",
            "stderr": "Max retries exceeded",
            "session_file": all_session_files[-1] if all_session_files else None,
            "opencode_session_id": last_session_id,
            "retry_info": {
                "attempts": attempt + 1,
                "max_retries": effective_max_retries,
                "retried": True,
                "final_failure": True,
                "all_session_files": all_session_files,
                "last_opencode_session_id": last_session_id,
            },
        }
