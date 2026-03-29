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
    model: Optional[str] = None  # Model override (e.g. for code review with a free model)
    
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
    
    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or settings.project_root
        self.opencode_cli = settings.opencode_cli
        self._running_tasks: Dict[str, asyncio.Task] = {}
    
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
        # Use configured timeout if not overridden
        effective_timeout = timeout_seconds or settings.agent_task_timeout_seconds
        start_time = asyncio.get_event_loop().time()

        # Create session file for this task with naming convention: JIRAID_DATETIME_RETRYCOUNT
        session_file = self._get_session_file(
            task.task_id,
            issue_key=task.issue_key,
            attempt_number=attempt_number,
        )
        session_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Build the command as a list (cross-platform)
        cmd_list = self._build_command(task, session_file)
        
        # Open session file for writing output
        with open(session_file, 'w', encoding='utf-8') as session_fh:
            # Run the process using exec (no shell) for cross-platform compatibility
            # On Windows, we need to use shell=False and handle the command differently
            if IS_WINDOWS:
                # Windows: use CREATE_NEW_PROCESS_GROUP for proper termination
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.project_root,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
                )
            else:
                # Unix/Linux/Mac
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.project_root,
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
                # Wait for completion with timeout
                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(process.stdout, stdout_lines, "stdout", session_fh),
                        read_stream(process.stderr, stderr_lines, "stderr", session_fh),
                    ),
                    timeout=effective_timeout
                )
                returncode = await process.wait()
            except asyncio.TimeoutError:
                # Kill the process on timeout
                process.kill()
                await process.wait()
                # Extract session ID from output collected so far
                all_output_lines = stdout_lines + stderr_lines
                session_id = self._parse_session_id(all_output_lines)
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

        result = {
            "task_id": task.task_id,
            "returncode": returncode,
            "stdout": "\n".join(stdout_lines),
            "stderr": "\n".join(stderr_lines),
            "session_file": str(session_file),
            "opencode_session_id": session_id,  # opencode session ID from CLI output
            "progress": 100 if returncode == 0 else last_progress,
        }

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
        # Build base command
        cmd_parts = self.opencode_cli.split() + ["run"]
        
        # Add agent option
        cmd_parts.extend(["--agent", task.agent])
        
        # Use task-specific model if provided, otherwise default
        effective_model = task.model or "opencode/big-pickle"
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
    ) -> Path:
        """Get path to session output file.

        Args:
            task_id: The task ID
            issue_key: The JIRA issue key (e.g., "PROJ-123")
            attempt_number: The retry attempt number (0 = first attempt)

        Returns:
            Path to the session log file

        Naming convention:
            - First attempt: PROJ-123_20240327_143052_0.log
            - Retry 1: PROJ-123_20240327_143052_1.log
            - Retry 2: PROJ-123_20240327_143052_2.log
        """
        # Ensure directory exists
        sessions_dir = (self.project_root / ".jira-agent" / "sessions").resolve()
        sessions_dir.mkdir(parents=True, exist_ok=True)

        if issue_key:
            # Format: JIRAID_DATETIME_RETRYCOUNT.log
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{issue_key}_{timestamp}_{attempt_number}.log"
        else:
            # Fallback to task_id if no issue_key provided
            filename = f"{task_id}.log"

        path = sessions_dir / filename
        return path
    
    async def run_background_agent(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
    ) -> str:
        """Start a background agent and return task ID."""
        # Similar to run_agent but non-blocking
        # Returns immediately with task ID for polling
        
        session_file = self._get_session_file(task.task_id)
        session_file.parent.mkdir(parents=True, exist_ok=True)
        
        cmd_list = self._build_command(task, session_file)
        
        # Start process without waiting (cross-platform)
        if IS_WINDOWS:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.project_root,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.project_root,
            )
        
        # Store for later monitoring
        self._running_tasks[task.task_id] = process
        
        return task.task_id
    
    async def check_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Check status of a running background task."""
        process = self._running_tasks.get(task_id)
        if not process:
            return None
        
        # Check if process has completed
        if process.returncode is not None:
            del self._running_tasks[task_id]
            return {
                "task_id": task_id,
                "status": "completed" if process.returncode == 0 else "error",
                "returncode": process.returncode,
            }
        
        return {
            "task_id": task_id,
            "status": "running",
        }
    
    def read_session_output(self, task_id: str) -> str:
        """Read the output from a session file."""
        session_file = self._get_session_file(task_id)
        if session_file.exists():
            return session_file.read_text()
        return ""
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        process = self._running_tasks.get(task_id)
        if process and process.returncode is None:
            if IS_WINDOWS:
                # On Windows, terminate() sends CTRL_BREAK_EVENT to the process group
                # when CREATE_NEW_PROCESS_GROUP was used
                try:
                    process.terminate()
                except Exception:
                    # Fallback: kill if terminate doesn't work
                    try:
                        process.kill()
                    except Exception:
                        pass
            else:
                # Unix/Linux/Mac: terminate() sends SIGTERM
                process.terminate()
            return True
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

        last_result = None
        last_session_id = None
        attempt = 0
        all_session_files = []

        while attempt <= effective_max_retries:
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
            if result.get("opencode_session_id"):
                last_session_id = result["opencode_session_id"]

            # Check if successful
            if result.get("returncode") == 0:
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
            elif retry_on_error and attempt < effective_max_retries:
                should_retry = True
                retry_reason = "error"

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
                print(f"[AgentRunner] {retry_reason.capitalize()} on attempt {attempt}/{effective_max_retries} for {task.task_id}, retrying in {delay:.1f}s...")

                # Wait before retry
                await asyncio.sleep(delay)

                # Create new task ID for retry
                task.task_id = f"task_{uuid.uuid4().hex[:8]}"

                last_result = result
            else:
                # No more retries - include session ID from last attempt
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
        if last_result:
            last_result["retry_info"] = {
                "attempts": attempt + 1,
                "max_retries": effective_max_retries,
                "retried": True,
                "final_failure": True,
                "all_session_files": all_session_files,
                "last_opencode_session_id": last_session_id,
            }
            return last_result

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
