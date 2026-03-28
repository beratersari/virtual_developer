"""Agent runner that interfaces with Oh My OpenAgent via CLI."""

import asyncio
import json
import platform
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
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
    ) -> Dict[str, Any]:
        """Run an agent task asynchronously.
        
        Args:
            task: The agent task to run
            on_output: Callback for output lines (stream, line)
            on_complete: Callback when complete (result)
            on_progress: Callback for progress updates (percentage, message)
        """
        
        # Create session file for this task
        session_file = self._get_session_file(task.task_id)
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
            
            # Read output streams with progress tracking
            async def read_stream(stream, lines, callback_name, file_handle):
                nonlocal last_progress
                while True:
                    line = await stream.readline()
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
            
            # Wait for completion
            await asyncio.gather(
                read_stream(process.stdout, stdout_lines, "stdout", session_fh),
                read_stream(process.stderr, stderr_lines, "stderr", session_fh),
            )
            
            returncode = await process.wait()
        
        result = {
            "task_id": task.task_id,
            "returncode": returncode,
            "stdout": "\n".join(stdout_lines),
            "stderr": "\n".join(stderr_lines),
            "session_file": str(session_file),
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
        
        # Add model fallback for testing without API keys
        cmd_parts.extend(["--model", "opencode/big-pickle"])
        
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
    
    def _get_session_file(self, task_id: str) -> Path:
        """Get path to session output file."""
        # Use absolute path to ensure shell can find it
        path = (self.project_root / ".jira-agent" / "sessions" / f"{task_id}.log").resolve()
        # Ensure directory exists
        path.parent.mkdir(parents=True, exist_ok=True)
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
