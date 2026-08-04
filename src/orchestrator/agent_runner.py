"""Agent runner that interfaces with Oh My OpenAgent via CLI."""

import asyncio
import json
import os
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

# oh-my-openagent registers agents under display names, not short keys.
# Map config/short names so --agent resolves without "not found" fallback.
OPENCODE_AGENT_ALIASES: Dict[str, str] = {
    "sisyphus": "Sisyphus - ultraworker",
    "prometheus": "Prometheus - Plan Builder",
    "atlas": "Atlas - Plan Executor",
    "oracle": "oracle",
    "explore": "explore",
    "librarian": "librarian",
    "metis": "Metis - Plan Consultant",
    "momus": "Momus - Plan Critic",
    "sisyphus-junior": "Sisyphus-Junior",
    "hephaestus": "Hephaestus",
    "multimodal-looker": "multimodal-looker",
}


def _default_sessions_dir() -> Path:
    """Session logs root. Tests patch this so nothing lands in the real repo tree."""
    return (Path.cwd() / ".jira-agent" / "sessions").resolve()


def resolve_opencode_agent_name(agent: str) -> str:
    """Map short agent keys to OpenCode agent IDs registered by oh-my-openagent."""
    if not agent:
        return agent
    key = agent.strip()
    # Exact display-name passthrough
    if key in OPENCODE_AGENT_ALIASES.values():
        return key
    mapped = OPENCODE_AGENT_ALIASES.get(key.lower())
    if mapped:
        return mapped
    # Title-case single tokens often still fail; try lower map again
    return OPENCODE_AGENT_ALIASES.get(key.lower().replace("_", "-"), key)

# B10: allowlist child env (not denylist). Host Jira/GitLab push creds stay out.
# Model provider keys OpenCode needs are explicitly allowed.
_AGENT_ENV_ALLOW_EXACT = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PWD",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "ALL_PROXY",
        "all_proxy",
        # OpenCode / Bun / Node runtime
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "BUN_INSTALL",
        "NODE_ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "npm_config_cache",
        # Model providers the agent needs to call LLMs (not Jira/GitLab)
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOGETHER_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "OLLAMA_HOST",
        "OLLAMA_API_KEY",
    }
)
_AGENT_ENV_ALLOW_PREFIXES = (
    "LC_",
    "OPENCODE_",
    "BUN_",
    "npm_config_",
)


def _agent_subprocess_env() -> Dict[str, str]:
    """Minimal env for agent children: allowlist + no host git credentials.

    Never pass Jira/GitLab tokens, SSH agent, or credential helpers.
    Push/auth remains host-side via GitManager askpass only.
    """
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if value is None:
            continue
        if key in _AGENT_ENV_ALLOW_EXACT:
            env[key] = value
            continue
        if any(key.startswith(p) for p in _AGENT_ENV_ALLOW_PREFIXES):
            env[key] = value
    # Harden git so the agent cannot push with host credentials
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env.pop("SSH_AUTH_SOCK", None)
    env.pop("SSH_AGENT_PID", None)
    env.pop("GIT_ASKPASS", None)
    env.pop("GIT_SSH_COMMAND", None)
    env.pop("VD_GIT_PASSWORD", None)
    env.pop("GITLAB_PAT", None)
    env.pop("GITLAB_TOKEN", None)
    env.pop("JIRA_API_TOKEN", None)
    env.pop("JIRA_PASSWORD", None)
    return env


@dataclass
class AgentTask:
    """Represents an agent task to be executed.

    On construction, any Jira ``{params}`` git template is stripped from
    ``prompt`` so the model never sees repository/branch metadata (even if a
    caller forgets to sanitize). Git clone still uses the raw issue text.
    """
    description: str
    prompt: str
    agent: str
    issue_key: Optional[str] = None
    session_id: Optional[str] = None
    task_id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    skills: List[str] = field(default_factory=list)
    model: Optional[str] = None
    task_type: Optional[str] = None

    def __post_init__(self) -> None:
        from src.issue_git_spec import strip_params_block

        object.__setattr__(self, "prompt", strip_params_block(self.prompt or ""))
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "agent": self.agent,
            "prompt": self.prompt,
            "skills": self.skills,
            "session_id": self.session_id,
        }


class AgentRunner:
    """Runs agents using Oh My OpenAgent CLI."""

    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory
        self.opencode_cli = settings.opencode_cli
        # task_id -> asyncio subprocess (Process), for cancel_task / watchdog
        self._running_tasks: Dict[str, Any] = {}
        # Maps task_id -> last session log path (run_agent naming may include issue_key)
        self._session_files: Dict[str, Path] = {}
        logger.debug(f"AgentRunner initialized with working_directory={working_directory}")
    
    async def run_agent(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
        on_session_file: Optional[callable] = None,
        timeout_seconds: Optional[int] = None,
        attempt_number: int = 0,
    ) -> Dict[str, Any]:
        """Run an agent task asynchronously.

        Args:
            task: The agent task to run
            on_output: Callback for output lines (stream, line)
            on_complete: Callback when complete (result)
            on_progress: Callback for progress updates (percentage, message)
            on_session_file: Callback (session_path, prompt_path) when log/prompt files are created
            timeout_seconds: Override timeout from settings (None uses config default)
            attempt_number: The retry attempt number (0 = first attempt)
        """
        logger.info(f"Starting agent task: task_id={task.task_id}, agent={task.agent}, attempt={attempt_number}")
        
        # Use configured timeout if not overridden (allow explicit 0)
        effective_timeout = (
            settings.agent_task_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
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
        self._session_files[task.task_id] = session_file
        logger.info(f"Session file created: {session_file}")

        # Persist full agent prompt for dashboard task detail (params already stripped)
        prompt_path: Optional[Path] = None
        try:
            from src.issue_git_spec import strip_params_block

            prompt_path = session_file.with_suffix(".prompt.txt")
            prompt_path.write_text(
                strip_params_block(task.prompt or ""), encoding="utf-8"
            )
            logger.debug(f"Prompt file written: {prompt_path}")
        except Exception as e:
            logger.warning(f"Could not write prompt file for {task.task_id}: {e}")
            prompt_path = None

        # Link JobStore immediately so dashboard does not invent a legacy_* twin
        if on_session_file is not None:
            try:
                on_session_file(
                    str(session_file),
                    str(prompt_path) if prompt_path is not None else None,
                )
            except Exception as e:
                logger.debug(f"on_session_file callback failed: {e}")

        # Build the command as a list (cross-platform)
        cmd_list = self._build_command(task, session_file)
        logger.debug(f"Command built with {len(cmd_list)} parts: {' '.join(cmd_list[:3])}...")
        
        # Open session file for writing output
        with open(session_file, 'w', encoding='utf-8') as session_fh:
            # Run the process using exec (no shell) for cross-platform compatibility
            # On Windows, we need to use shell=False and handle the command differently
            child_env = _agent_subprocess_env()
            if IS_WINDOWS:
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=child_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.working_directory,
                    env=child_env,
                    start_new_session=True,  # own process group for killpg on cancel/timeout
                )

            # Register so /cancel and stuck-watchdog can terminate foreground agents
            self._running_tasks[task.task_id] = process
            
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
                logger.info(
                    f"Waiting for agent/opencode process to complete, "
                    f"timeout={effective_timeout}s"
                )
                # Wall-clock budget covers BOTH stream reads and process.wait().
                # Previously wait() ran unguarded after stdout/stderr EOF, so a
                # hung OpenCode child that closed pipes never timed out.
                async def _drain_and_wait() -> int:
                    await asyncio.gather(
                        read_stream(
                            process.stdout, stdout_lines, "stdout", session_fh
                        ),
                        read_stream(
                            process.stderr, stderr_lines, "stderr", session_fh
                        ),
                    )
                    remaining = effective_timeout - (
                        asyncio.get_event_loop().time() - start_time
                    )
                    if remaining <= 0:
                        raise asyncio.TimeoutError(
                            f"Task exceeded timeout of {effective_timeout} seconds"
                        )
                    return await asyncio.wait_for(
                        process.wait(), timeout=remaining
                    )

                returncode = await asyncio.wait_for(
                    _drain_and_wait(),
                    timeout=max(0.01, float(effective_timeout)),
                )
                elapsed = asyncio.get_event_loop().time() - start_time
                logger.info(
                    f"Agent process completed: returncode={returncode}, "
                    f"elapsed={elapsed:.2f}s, stdout_lines={len(stdout_lines)}, "
                    f"stderr_lines={len(stderr_lines)}"
                )
            except asyncio.TimeoutError:
                elapsed = asyncio.get_event_loop().time() - start_time
                logger.error(
                    f"Agent/opencode task timed out after {elapsed:.2f}s "
                    f"(limit={effective_timeout}s)"
                )
                await self._kill_process_tree_escalating(process, task.task_id)
                logger.info(f"Killed timed out process: task_id={task.task_id}")
                # Extract session ID from output collected so far
                all_output_lines = stdout_lines + stderr_lines
                session_id = self._resolve_session_id(
                    task, all_output_lines, session_file=session_file
                )
                logger.debug(f"Extracted session ID from partial output: {session_id}")
                return {
                    "task_id": task.task_id,
                    "returncode": -1,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": f"\n[TIMEOUT] Task exceeded {effective_timeout} seconds",
                    "session_file": str(session_file),
                    "opencode_session_id": session_id,
                    "progress": last_progress,
                    "timed_out": True,
                }
            finally:
                self._running_tasks.pop(task.task_id, None)
        
        # Extract session ID from output / OpenCode DB
        all_output_lines = stdout_lines + stderr_lines
        session_id = self._resolve_session_id(
            task, all_output_lines, session_file=session_file
        )
        logger.debug(f"Extracted session ID: {session_id}")

        elapsed = asyncio.get_event_loop().time() - start_time
        result = {
            "task_id": task.task_id,
            "returncode": returncode,
            "stdout": "\n".join(stdout_lines),
            "stderr": "\n".join(stderr_lines),
            "session_file": str(session_file),
            "opencode_session_id": session_id,
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

        def _clamp(pct: int) -> int:
            return max(0, min(100, pct))

        # Pattern 1: Direct percentage (e.g., "Progress: 75%" or "75%")
        match = re.search(r'(\d+)%', line)
        if match:
            return _clamp(int(match.group(1)))

        # Pattern 2: Progress bar blocks
        # Count filled vs empty blocks
        filled_blocks = line.count('█') + line.count('▓') + line.count('■')
        empty_blocks = line.count('░') + line.count('▒') + line.count(' ')
        total_blocks = filled_blocks + empty_blocks
        if total_blocks >= 5 and filled_blocks > 0:
            return _clamp(int((filled_blocks / total_blocks) * 100))

        # Pattern 3: Completed tasks (e.g., "Completed: 8/10 tasks")
        match = re.search(r'(\d+)\s*/\s*(\d+)\s*(?:tasks|steps|items)', line, re.IGNORECASE)
        if match:
            completed, total = int(match.group(1)), int(match.group(2))
            if total > 0:
                return _clamp(int((completed / total) * 100))

        return None

    def _parse_session_id(self, lines: List[str]) -> Optional[str]:
        """Parse opencode session ID from output lines.

        Prefers labeled patterns; takes the **last** match so early noise
        (parent session echoes) does not win over the real session.
        """
        import re

        labeled = [
            r'Session[:\s]+(ses_[a-zA-Z0-9_-]+)',
            r'Session\s*ID[:\s]+(ses_[a-zA-Z0-9_-]+)',
            r'"sessionID"\s*:\s*"(ses_[a-zA-Z0-9_-]+)"',
        ]
        bare = r'(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)'

        last_labeled: Optional[str] = None
        last_bare: Optional[str] = None
        for line in lines:
            for pattern in labeled:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    last_labeled = match.group(1)
            for match in re.finditer(bare, line, re.IGNORECASE):
                last_bare = match.group(1)
        return last_labeled or last_bare
    
    def _build_command(self, task: AgentTask, session_file: Path) -> List[str]:
        """Build the opencode CLI command as a list (cross-platform).
        
        Command format: bunx oh-my-opencode run [options] <message>
        The message must be the last argument.
        
        Returns:
            List of command arguments for use with subprocess (no shell needed)
        """
        agent_name = resolve_opencode_agent_name(task.agent)
        logger.debug(
            f"Building command for task: agent={task.agent} -> {agent_name}, "
            f"model={task.model or settings.default_model}, session_id={task.session_id}"
        )
        
        # Build base command
        cmd_parts = self.opencode_cli.split() + ["run"]

        # Force OpenCode into the issue temp clone (otherwise it walks up to the
        # host git root and edits/commits the wrong repository).
        if self.working_directory:
            cmd_parts.extend(["--dir", str(self.working_directory)])
        
        # Add agent option (resolved OpenCode / oh-my-openagent ID)
        cmd_parts.extend(["--agent", agent_name])
        
        # Use task-specific model if provided, otherwise use configured default
        effective_model = task.model or settings.default_model
        if effective_model:
            cmd_parts.extend(["--model", effective_model])
        
        # Add session continuation if specified
        if task.session_id:
            # Current OpenCode CLI uses --session, not --session-id
            cmd_parts.extend(["--session", task.session_id])

        # Unattended daemon runs: never prompt for permission / tool approval.
        # (--title omitted on purpose — not needed; sessions keyed by issue/dir.)
        cmd_parts.append("--auto")
        
        # Final gate: never pass {params} git blocks to the agent CLI
        from src.issue_git_spec import strip_params_block

        cmd_parts.append(strip_params_block(task.prompt or ""))
        
        return cmd_parts

    def _resolve_session_id(
        self,
        task: AgentTask,
        output_lines: List[str],
        *,
        session_file: Optional[Path] = None,
    ) -> Optional[str]:
        """Parse CLI output, then fall back to OpenCode SQLite by issue/dir."""
        session_id = self._parse_session_id(output_lines)
        if not session_id and task.issue_key:
            try:
                from src.opencode_sessions import resolve_session_id

                session_id = resolve_session_id(
                    task.issue_key,
                    working_directory=self.working_directory,
                )
            except Exception as e:
                logger.debug(f"Session DB lookup failed: {e}")
        if session_id and session_file is not None:
            try:
                Path(str(session_file) + ".session_id").write_text(
                    session_id + "\n", encoding="utf-8"
                )
            except Exception:
                pass
        return session_id
    
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
        # Ensure directory exists (tests patch _default_sessions_dir to isolate)
        sessions_dir = _default_sessions_dir()
        sessions_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(
            f"Generating session file path: task_id={task_id}, issue_key={issue_key}, "
            f"attempt={attempt_number}, task_type={task_type}"
        )

        def _safe_token(s: str) -> str:
            cleaned = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in (s or "")
            )
            return (cleaned.strip("._-") or "unknown")[:80]

        if issue_key:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_key = _safe_token(issue_key)
            if task_type:
                filename = (
                    f"{safe_key}_{_safe_token(task_type)}_{timestamp}_{attempt_number}.log"
                )
            else:
                filename = f"{safe_key}_{timestamp}_{attempt_number}.log"
        else:
            filename = f"{_safe_token(task_id or 'task')}.log"

        path = sessions_dir / filename
        try:
            path.resolve().relative_to(sessions_dir)
        except ValueError:
            path = sessions_dir / f"{_safe_token(task_id or 'task')}.log"
        if task_id:
            self._session_files[task_id] = path
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
        
        child_env = _agent_subprocess_env()
        if IS_WINDOWS:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.working_directory,
                env=child_env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if hasattr(subprocess, 'CREATE_NEW_PROCESS_GROUP') else 0,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd_list,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.working_directory,
                env=child_env,
                start_new_session=True,
            )
        
        # Store for later monitoring / cancel
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
        """Read the output from a session file for a task.

        Prefers the path registered when the agent ran (which may include
        issue_key and timestamp), then falls back to the task_id-only name.
        """
        session_file = self._session_files.get(task_id)
        if session_file is None:
            session_file = self._get_session_file(task_id)
        logger.debug(f"Reading session output: task_id={task_id}, file={session_file}")
        if session_file.exists():
            content = session_file.read_text(encoding="utf-8", errors="replace")
            logger.debug(f"Read {len(content)} chars from session file: {session_file}")
            return content
        logger.debug(f"Session file not found: {session_file}")
        return ""
    
    def _kill_process_tree(self, process: Any, *, force: bool = False) -> None:
        """Terminate process and, on Unix, its process group.

        ``force=True`` sends SIGKILL (Unix) / kill() (Windows) immediately.
        """
        if process is None:
            return
        try:
            if IS_WINDOWS:
                try:
                    if force:
                        process.kill()
                    else:
                        process.terminate()
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
                return

            pid = getattr(process, "pid", None)
            if pid is not None:
                try:
                    import signal
                    sig = signal.SIGKILL if force else signal.SIGTERM
                    os.killpg(pid, sig)
                    return
                except (ProcessLookupError, PermissionError, OSError, AttributeError):
                    pass
            try:
                if force:
                    process.kill()
                else:
                    process.terminate()
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        except ProcessLookupError:
            pass

    async def _kill_process_tree_escalating(
        self, process: Any, task_id: str, *, soft_wait: float = 10.0
    ) -> None:
        """SIGTERM, wait, then SIGKILL if the process is still alive."""
        self._kill_process_tree(process, force=False)
        try:
            await asyncio.wait_for(process.wait(), timeout=soft_wait)
            return
        except asyncio.TimeoutError:
            logger.warning(
                f"Process still alive after SIGTERM, escalating to SIGKILL: {task_id}"
            )
        self._kill_process_tree(process, force=True)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(f"Process wait timed out after SIGKILL: {task_id}")

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task (foreground or background)."""
        process = self._running_tasks.get(task_id)
        if process and process.returncode is None:
            logger.info(f"Cancelling running task: task_id={task_id}")
            self._kill_process_tree(process, force=False)
            # Escalate if still running (sync path — best effort)
            if process.returncode is None:
                try:
                    import time
                    time.sleep(0.5)
                except Exception:
                    pass
                if process.returncode is None:
                    self._kill_process_tree(process, force=True)
            logger.info(f"Task cancel signal sent: task_id={task_id}")
            return True
        logger.debug(f"Task not found or already completed: task_id={task_id}")
        return False

    def cancel_all_tasks(self) -> int:
        """Kill every live child process tracked by this runner. Returns count signalled.

        SIGTERM then brief wait then SIGKILL (same escalation as cancel_task).
        """
        killed = 0
        for task_id, process in list(self._running_tasks.items()):
            if process is None:
                continue
            if getattr(process, "returncode", None) is not None:
                continue
            logger.info(f"Cancelling task on shutdown: task_id={task_id}")
            self._kill_process_tree(process, force=False)
            if getattr(process, "returncode", None) is None:
                try:
                    import time
                    time.sleep(0.5)
                except Exception:
                    pass
                if getattr(process, "returncode", None) is None:
                    self._kill_process_tree(process, force=True)
            killed += 1
        return killed

    async def run_agent_with_retry(
        self,
        task: AgentTask,
        on_output: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
        on_retry: Optional[callable] = None,
        on_session_file: Optional[callable] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        should_abort: Optional[callable] = None,
    ) -> Dict[str, Any]:
        """Run an agent task with automatic retry on failure.

        Args:
            task: The agent task to run
            on_output: Callback for output lines (stream, line)
            on_complete: Callback when complete (result)
            on_progress: Callback for progress updates (percentage, message)
            on_retry: Callback when a retry is attempted
                ``(attempt_number, delay_seconds, reason, session_file,
                error_message, return_code, session_id, new_task_id)``.
                ``new_task_id`` is the id of the upcoming attempt (must be
                stored as ``current_task_id`` for cancel/watchdog).
            on_session_file: Callback when session/prompt files are created
            timeout_seconds: Override timeout from settings
            max_retries: Override max retries from settings
            should_abort: Optional zero-arg callable; when true, stop retrying
                immediately (cancel / stuck watchdog). Result includes
                ``aborted=True``.

        Returns:
            Dict with task result including retry information and all session files
        """
        # Allow max_retries=0 to mean "no retries" (do not treat 0 as unset)
        effective_max_retries = (
            settings.agent_task_max_retries
            if max_retries is None
            else max_retries
        )
        retry_delay = settings.agent_task_retry_delay_seconds
        backoff_multiplier = settings.agent_task_retry_backoff_multiplier
        retry_on_timeout = settings.agent_task_retry_on_timeout
        retry_on_error = settings.agent_task_retry_on_error
        
        logger.info(f"Starting agent with retry: task_id={task.task_id}, max_retries={effective_max_retries}")

        def _aborted() -> bool:
            try:
                return bool(should_abort and should_abort())
            except Exception:
                return False

        def _abort_result(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            base = {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": "",
                "stderr": "Aborted (cancelled or errored while running)",
                "session_file": all_session_files[-1] if all_session_files else None,
                "opencode_session_id": last_session_id,
                "aborted": True,
                "retry_info": {
                    "attempts": attempt + 1,
                    "max_retries": effective_max_retries,
                    "retried": attempt > 0,
                    "aborted": True,
                    "all_session_files": all_session_files,
                    "last_opencode_session_id": last_session_id,
                },
            }
            if extra:
                base.update(extra)
            return base

        last_result = None
        last_session_id = None
        attempt = 0
        all_session_files = []

        while attempt <= effective_max_retries:
            if _aborted():
                logger.info(
                    f"Abort before attempt {attempt + 1}: task_id={task.task_id}"
                )
                return _abort_result()

            logger.info(f"Agent attempt {attempt + 1}/{effective_max_retries + 1}: task_id={task.task_id}")
            
            # Run the task with attempt number (0 = first attempt, 1+ = retries)
            result = await self.run_agent(
                task,
                on_output=on_output,
                on_complete=on_complete,
                on_progress=on_progress,
                on_session_file=on_session_file,
                timeout_seconds=timeout_seconds,
                attempt_number=attempt,
            )

            # Track session file and opencode session ID for this attempt
            if result.get("session_file"):
                all_session_files.append(result["session_file"])
                logger.debug(f"Added session file to tracking: {result['session_file']}")
            if result.get("opencode_session_id"):
                last_session_id = result["opencode_session_id"]

            # Cancel/watchdog during the run — do not retry or treat as success
            if _aborted():
                logger.info(
                    f"Abort after attempt {attempt + 1}: task_id={task.task_id}"
                )
                result = dict(result)
                result["aborted"] = True
                result["retry_info"] = {
                    "attempts": attempt + 1,
                    "max_retries": effective_max_retries,
                    "retried": attempt > 0,
                    "aborted": True,
                    "all_session_files": all_session_files,
                    "last_opencode_session_id": last_session_id,
                }
                return result

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
                if _aborted():
                    logger.info(
                        f"Abort before scheduling retry: task_id={task.task_id}"
                    )
                    return _abort_result(
                        {
                            "stderr": result.get("stderr")
                            or "Aborted before retry",
                            "session_file": result.get("session_file"),
                        }
                    )

                attempt += 1

                # Calculate delay with exponential backoff
                delay = retry_delay * (backoff_multiplier ** (attempt - 1))

                # Extract error details from the failed attempt
                error_message = result.get("stderr", "") if result.get("returncode") != 0 else None
                return_code = result.get("returncode")

                # Mint new task_id BEFORE on_retry so callers can refresh
                # state.current_task_id. Otherwise /cancel and stuck-watchdog
                # keep the first-attempt id and cannot kill the live process.
                old_task_id = task.task_id
                task.task_id = f"task_{uuid.uuid4().hex[:8]}"
                logger.debug(
                    f"Created new task ID for retry: old={old_task_id}, new={task.task_id}"
                )

                if on_retry:
                    on_retry(
                        attempt,
                        delay,
                        retry_reason,
                        result.get("session_file"),
                        error_message,
                        return_code,
                        result.get("opencode_session_id"),
                        task.task_id,  # new_task_id for state sync
                    )

                # Log retry attempt
                logger.warning(
                    f"{retry_reason.capitalize()} on attempt {attempt}/{effective_max_retries} "
                    f"for {task.task_id}, retrying in {delay:.1f}s..."
                )

                # Wait before retry; re-check abort so cancel during backoff sticks
                await asyncio.sleep(delay)
                if _aborted():
                    logger.info(
                        f"Abort during retry backoff: task_id={task.task_id}"
                    )
                    return _abort_result()

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

        # Should not reach here with normal control flow (loop always returns).
        # Defensive fallback kept for safety; last_result branch is effectively dead.
        logger.error(f"Unexpected fallback reached in run_agent_with_retry: task_id={task.task_id}")
        if last_result:  # pragma: no cover
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
