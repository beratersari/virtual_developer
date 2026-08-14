"""Agent runner that drives Oh My OpenAgent over ``opencode serve``."""

import asyncio
import os
import platform
import subprocess
import time
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

# ---------------------------------------------------------------------------
# Agent child env: pass everything except sensitive names.
# (working_directory is accepted for call-site compatibility; unused.)
# ---------------------------------------------------------------------------

# Host bot / push / SSH — always blocked.
_AGENT_ENV_BLOCK_EXACT = frozenset(
    {
        "JIRA_API_TOKEN",
        "JIRA_PASSWORD",
        "JIRA_EMAIL",
        "GITLAB_PAT",
        "GITLAB_TOKEN",
        "GITLAB_HOST_PATS",
        "GITLAB_PRIVATE_TOKEN",
        "GITLAB_ACCESS_TOKEN",
        "VD_GIT_PASSWORD",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "GIT_ASKPASS",
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "SSH_ASKPASS",
    }
)

# Block if any of these appear in the variable name.
# (No bare "_PAT" — it false-matches CMAKE_PREFIX_PATH; PATs are in EXACT above.)
_AGENT_ENV_BLOCK_PATTERNS = (
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
    "_API_KEY",
    "PRIVATE_KEY",
)

# LLM keys: needed by OpenCode even though they match _API_KEY / _TOKEN.
_AGENT_ENV_PASS = frozenset(
    {
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


def _agent_subprocess_env(
    working_directory: Optional[Path] = None,
) -> Dict[str, str]:
    """Pass all env vars except blocked sensitive credentials.

    - Build tools get toolchain vars (PATH, INCLUDE, LIB, MSVC, CMake, …).
    - Model provider keys pass (see ``_AGENT_ENV_PASS``).
    - Host Jira/GitLab/SSH secrets stay out; push stays on GitManager askpass.
    """
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if value is None:
            continue
        if key in _AGENT_ENV_BLOCK_EXACT:
            continue
        if key.startswith("VD_GIT_"):
            continue
        if key not in _AGENT_ENV_PASS:
            if any(p in key for p in _AGENT_ENV_BLOCK_PATTERNS):
                continue
        env[key] = value

    # Harden git so the agent cannot push with host credentials.
    # Do NOT rewrite HOME/USERPROFILE — OpenCode loads plugins from
    # ~/.opencode, ~/.config/opencode, ~/.cache/opencode.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "credential.helper"
    env["GIT_CONFIG_VALUE_0"] = ""
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
    abandoned_session_id: Optional[str] = None
    forgotten_session_ids: List[str] = field(default_factory=list)
    original_prompt: Optional[str] = None

    def __post_init__(self) -> None:
        from src.issue_git_spec import strip_params_block

        cleaned = strip_params_block(self.prompt or "")
        object.__setattr__(self, "prompt", cleaned)
        if not (self.original_prompt or "").strip():
            object.__setattr__(self, "original_prompt", cleaned)
    
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
    """Runs agents via OpenCode HTTP serve (same session, auto-compact)."""

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
        on_session_id: Optional[callable] = None,
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
            on_session_id: Callback (ses_*) as soon as the OpenCode session is known
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

        published_sid = {"id": None, "last_db": 0.0}

        def _publish_session(sid: Optional[str]) -> None:
            sid = (sid or "").strip()
            if not sid or not sid.startswith("ses_") or sid == published_sid["id"]:
                return
            forgotten = {
                str(x).strip()
                for x in (getattr(task, "forgotten_session_ids", None) or [])
                if str(x).strip()
            }
            abandoned = (getattr(task, "abandoned_session_id", None) or "").strip()
            if abandoned:
                forgotten.add(abandoned)
            if sid in forgotten:
                logger.warning(
                    f"Not publishing forgotten/abandoned session {sid} "
                    f"for task_id={task.task_id}"
                )
                return
            published_sid["id"] = sid
            try:
                Path(str(session_file) + ".session_id").write_text(
                    sid + "\n", encoding="utf-8"
                )
            except Exception:
                pass
            if on_session_id is not None:
                try:
                    on_session_id(sid)
                except Exception:
                    pass

        if getattr(task, "session_id", None):
            _publish_session(str(task.session_id))

        return await self._run_agent_via_serve(
            task,
            session_file=session_file,
            on_output=on_output,
            on_complete=on_complete,
            on_progress=on_progress,
            on_session_id=_publish_session,
            timeout_seconds=effective_timeout,
            start_time=start_time,
        )


    @staticmethod
    def _session_log_is_empty(session_file: Optional[str]) -> bool:
        if not session_file:
            return True
        try:
            p = Path(session_file)
            return (not p.is_file()) or p.stat().st_size < 40
        except OSError:
            return True

    def _resume_opencode_session_for_retry(
        self,
        task: AgentTask,
        session_id: Optional[str],
        *,
        why: str,
        session_file: Optional[str] = None,
        timed_out: bool = False,
        stdout: Optional[str] = None,
    ) -> None:
        """Point the next serve attempt at an existing OpenCode session.

        Serve reuse uses ``task.session_id``.
        A cold retry would discard compacted history — but an empty timeout
        usually means the session pointed at another clone directory and
        hung; retrying Continue on that id stays stuck.
        """
        sid = (session_id or "").strip()
        empty_log = self._session_log_is_empty(session_file)
        no_stdout = not (stdout or "").strip()
        if timed_out and empty_log and no_stdout and not sid:
            logger.warning(
                f"Retry after {why}: empty session log and no session id; "
                "starting cold"
            )
            return
        if sid and self.working_directory:
            try:
                from src.opencode_sessions import (
                    lookup_session_directory,
                    session_matches_workdir,
                )

                stored_dir, ok = lookup_session_directory(sid)
                if not ok:
                    # Same job / same clone: keep the session. The row may not
                    # be flushed yet; a transient DB error must not drop Continue.
                    logger.warning(
                        f"Retry after {why}: OpenCode DB unreadable; "
                        f"keeping session {sid} on current clone"
                    )
                elif stored_dir and not session_matches_workdir(
                    sid, self.working_directory
                ):
                    logger.warning(
                        f"Retry after {why}: session {sid} is not for "
                        f"{self.working_directory}; starting cold (do not "
                        "re-send BUILD/PLAN on another clone's session)"
                    )
                    task.abandoned_session_id = sid
                    task.session_id = None
                    return
            except Exception as e:
                logger.debug(f"session dir check failed: {e}")
        if not sid:
            logger.warning(
                f"Retry after {why} has no OpenCode session id; starting cold"
            )
            return
        task.session_id = sid
        if why == "incomplete_session":
            from src.opencode_serve import DEFAULT_FINISH_TODOS_PROMPT

            # Short finish-todos nudge — not the original BUILD/PLAN kit and
            # not a fake operator "Continue" during compact.
            task.prompt = DEFAULT_FINISH_TODOS_PROMPT
            logger.warning(
                f"Retry after {why}: resume session {sid} with finish-todos prompt"
            )
            return
        from src.opencode_serve import DEFAULT_CONTINUE_PROMPT

        prev = (task.prompt or "").lstrip()
        if not prev.lower().startswith("continue"):
            task.prompt = DEFAULT_CONTINUE_PROMPT
        logger.warning(
            f"Retry after {why}: resume OpenCode session {sid}"
        )

    def _assess_incomplete_run(
        self,
        *,
        session_id: Optional[str],
        output_text: str,
    ) -> Optional[Dict[str, Any]]:
        """Inspect OpenCode DB + transcript for premature exit-0 runs."""
        try:
            from src.opencode_sessions import assess_session_completeness

            return assess_session_completeness(
                session_id,
                output_text=output_text or "",
            )
        except Exception as e:
            logger.debug(f"Session completeness assessment failed: {e}")
            return None

    async def _run_agent_via_serve(
        self,
        task: AgentTask,
        *,
        session_file: Path,
        on_output: Optional[callable] = None,
        on_complete: Optional[callable] = None,
        on_progress: Optional[callable] = None,
        on_session_id: Optional[callable] = None,
        timeout_seconds: int = 1800,
        start_time: float = 0.0,
    ) -> Dict[str, Any]:
        """Drive OpenCode over HTTP serve and wait out auto-compact.

        Requires a running ``opencode serve`` at ``settings.opencode_serve_url``.
        Compaction is never resumed by posting a user Continue prompt.
        """
        from src.opencode_serve import OpenCodeServeClient, ServeOrchestrator

        base = (
            getattr(settings, "opencode_serve_url", None) or "http://127.0.0.1:4096"
        )
        max_cont = int(
            getattr(settings, "opencode_serve_max_compact_continues", 256) or 0
        )
        work_dir = str(self.working_directory) if self.working_directory else None
        agent_name = resolve_opencode_agent_name(task.agent)
        model = task.model or settings.default_model
        title = (
            f"{task.issue_key}: {task.description}"
            if task.issue_key
            else (task.description or task.task_id)
        )[:120]

        log_lines: List[str] = []
        try:
            session_file.write_text("", encoding="utf-8")
        except OSError:
            pass
        client = OpenCodeServeClient(
            base,
            timeout_seconds=float(timeout_seconds),
            directory=work_dir,
        )
        # Cancel handle: serve path stores client + session (not a subprocess).
        serve_handle: Dict[str, Any] = {
            "mode": "serve",
            "client": client,
            "session_id": task.session_id,
            "cancel": False,
        }
        self._running_tasks[task.task_id] = serve_handle

        def _on_out(stream: str, line: str) -> None:
            try:
                with open(session_file, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
            except OSError:
                pass
            if on_output:
                on_output(stream, line)

        def _remember_session(sid: str) -> None:
            """Publish ses_* immediately so cancel/retry/continue share it."""
            if not sid:
                return
            serve_handle["session_id"] = sid
            task.session_id = sid
            try:
                Path(str(session_file) + ".session_id").write_text(
                    sid + "\n", encoding="utf-8"
                )
            except Exception:
                pass
            if on_session_id is not None:
                try:
                    on_session_id(sid)
                except Exception:
                    pass

        orch = ServeOrchestrator(
            client=client,
            max_compact_continues=max_cont,
            # Cover the rest of the job: auto-compact then auto-resume can
            # take as long as the original turn. Never inject Continue.
            compact_wait_seconds=float(timeout_seconds) or 180.0,
            compact_poll_seconds=2.0,
        )
        turn = None
        try:
            # One user turn + one compact wait. max_compact_continues no longer
            # POSTs Continue; do not multiply the outer wait by 256.
            outer_timeout = float(timeout_seconds) + float(
                orch.compact_wait_seconds or 0
            )
            turn = await asyncio.wait_for(
                orch.run(
                    prompt=task.prompt or "",
                    title=title,
                    agent=agent_name,
                    model=model,
                    session_id=task.session_id,
                    on_output=_on_out,
                    on_session=_remember_session,
                    should_abort=lambda: bool(serve_handle.get("cancel")),
                    log_lines=log_lines,
                ),
                timeout=outer_timeout,
            )
            if turn.session_id:
                _remember_session(turn.session_id)
        except asyncio.TimeoutError:
            try:
                sid = serve_handle.get("session_id")
                if sid:
                    await client.abort(sid)
            except Exception:
                pass
            result = {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": "\n".join(log_lines),
                "stderr": f"[serve] timed out after {timeout_seconds}s",
                "session_file": str(session_file),
                "opencode_session_id": serve_handle.get("session_id"),
                "progress": 0,
                "mode": "serve",
                "timed_out": True,
            }
            if on_complete:
                on_complete(result)
            return result
        finally:
            self._running_tasks.pop(task.task_id, None)
            try:
                await client.aclose()
            except Exception:
                pass

        if turn is None:
            result = {
                "task_id": task.task_id,
                "returncode": -1,
                "stdout": "\n".join(log_lines),
                "stderr": "[serve] no result",
                "session_file": str(session_file),
                "opencode_session_id": None,
                "progress": 0,
                "mode": "serve",
            }
            if on_complete:
                on_complete(result)
            return result

        try:
            body = turn.stdout or ""
            if turn.stderr:
                body = body + ("\n" if body else "") + turn.stderr
            session_file.write_text(body, encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not write serve session log: {e}")

        if turn.session_id:
            try:
                Path(str(session_file) + ".session_id").write_text(
                    turn.session_id + "\n", encoding="utf-8"
                )
            except Exception:
                pass

        if on_progress and turn.returncode == 0:
            try:
                on_progress(100, "serve complete")
            except Exception:
                pass

        result = turn.to_agent_result(task.task_id, session_file=str(session_file))
        elapsed = asyncio.get_event_loop().time() - start_time
        if turn.returncode == 0:
            logger.info(
                f"Serve agent completed: task_id={task.task_id}, "
                f"session={turn.session_id}, continues={turn.continue_count}, "
                f"compacts={turn.compact_events}, duration={elapsed:.2f}s"
            )
        else:
            logger.warning(
                f"Serve agent failed: task_id={task.task_id}, "
                f"returncode={turn.returncode}, continues={turn.continue_count}, "
                f"incomplete={turn.incomplete}, duration={elapsed:.2f}s"
            )
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

        # Pattern 2: Progress bar blocks (do not count prose spaces as empty cells)
        filled_blocks = line.count('█') + line.count('▓') + line.count('■')
        empty_blocks = line.count('░') + line.count('▒')
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

        created = [
            r'session created:\s*(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)',
            r'session resumed:\s*(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)',
        ]
        labeled = [
            r'Session:\s*(ses_[a-zA-Z0-9_-]+)',
            r'Session\s+ID[:\s]+(ses_[a-zA-Z0-9_-]+)',
        ]
        bare = r'(ses_[a-zA-Z0-9]{6,}[a-zA-Z0-9_-]*)'

        last_created: Optional[str] = None
        last_labeled: Optional[str] = None
        last_bare: Optional[str] = None
        for line in lines:
            for pattern in created:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    last_created = match.group(1)
            for pattern in labeled:
                for match in re.finditer(pattern, line, re.IGNORECASE):
                    last_labeled = match.group(1)
            for match in re.finditer(bare, line, re.IGNORECASE):
                last_bare = match.group(1)
        return last_created or last_labeled or last_bare

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

        Naming convention (attempt_number is 0-based; retries use _retryN):
            - First attempt: PROJ-123_20240327_143052.log
            - Code review:   PROJ-123_review_20240327_143052.log
            - Retry 1:       PROJ-123_20240327_143052_retry1.log
            - Retry 2:       PROJ-123_20240327_143052_retry2.log
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
            # attempt 0 = initial run; attempt N>=1 → suffix _retryN (dashboard labels)
            retry_suffix = (
                f"_retry{int(attempt_number)}" if int(attempt_number or 0) > 0 else ""
            )
            if task_type:
                filename = (
                    f"{safe_key}_{_safe_token(task_type)}_{timestamp}{retry_suffix}.log"
                )
            else:
                filename = f"{safe_key}_{timestamp}{retry_suffix}.log"
        else:
            base = _safe_token(task_id or "task")
            if int(attempt_number or 0) > 0:
                filename = f"{base}_retry{int(attempt_number)}.log"
            else:
                filename = f"{base}.log"

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
        """Start a serve-backed agent in the background and return task ID."""
        logger.info(f"Starting background agent: task_id={task.task_id}, agent={task.agent}")
        self._running_tasks[task.task_id] = {
            "mode": "serve",
            "client": None,
            "session_id": task.session_id,
            "cancel": False,
        }

        async def _bg() -> None:
            try:
                await self.run_agent(task, on_output=on_output)
            except Exception as e:
                logger.warning(f"Background agent failed: task_id={task.task_id}: {e}")
            finally:
                self._running_tasks.pop(task.task_id, None)

        asyncio.create_task(_bg())
        logger.info(f"Background agent scheduled: task_id={task.task_id}")
        return task.task_id

    
    async def check_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Check status of a running background task."""
        process = self._running_tasks.get(task_id)
        if not process:
            logger.debug(f"Task not found in running tasks: {task_id}")
            return None

        # Serve-mode handle is a dict, not a subprocess
        if isinstance(process, dict) and process.get("mode") == "serve":
            return {"task_id": task_id, "status": "running", "mode": "serve"}

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
        if not process:
            logger.debug(f"Task not found or already completed: task_id={task_id}")
            return False

        # Serve mode: POST /session/{id}/abort (best effort, sync wrapper)
        if isinstance(process, dict) and process.get("mode") == "serve":
            process["cancel"] = True
            sid = process.get("session_id")
            client = process.get("client")
            logger.info(f"Cancelling serve task: task_id={task_id}, session={sid}")
            if client is not None and sid:
                try:
                    # cancel_task is sync; schedule abort if loop is running
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.ensure_future(client.abort(sid))
                        else:
                            loop.run_until_complete(client.abort(sid))
                    except Exception:
                        pass
                except Exception as e:
                    logger.debug(f"serve abort failed: {e}")
            return True

        if process.returncode is None:
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
            if isinstance(process, dict) and process.get("mode") == "serve":
                if self.cancel_task(task_id):
                    killed += 1
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
        on_session_id: Optional[callable] = None,
        timeout_seconds: Optional[int] = None,
        max_retries: Optional[int] = None,
        max_incomplete_retries: Optional[int] = None,
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
            max_incomplete_retries: Compact/incomplete resume budget.
                Independent of ``max_retries`` so compaction is not treated as
                a generic error. ``None`` uses settings. When the caller
                passes ``max_retries=0``, incomplete retries are also 0.
            should_abort: Optional zero-arg callable; when true, stop retrying
                immediately (cancel / stuck watchdog). Result includes
                ``aborted=True``.

        Returns:
            Dict with task result including retry information and all session files
        """
        def _safe_int(val: Any, default: int = 0) -> int:
            try:
                if val is None or isinstance(val, bool):
                    return int(default)
                return int(val)
            except (TypeError, ValueError):
                return int(default)

        # Allow max_retries=0 to mean "no retries" (do not treat 0 as unset)
        effective_max_retries = (
            settings.agent_task_max_retries
            if max_retries is None
            else max_retries
        )
        effective_max_retries = _safe_int(effective_max_retries, 0)
        if max_incomplete_retries is not None:
            incomplete_cap = _safe_int(max_incomplete_retries, 0)
        elif max_retries == 0:
            # Explicit "no retries" from caller (tests / one-shot)
            incomplete_cap = 0
        else:
            incomplete_cap = _safe_int(
                getattr(settings, "agent_task_max_incomplete_retries", 0), 0
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
                "retry_info": _retry_info(aborted=True),
            }
            if extra:
                base.update(extra)
            return base

        last_result = None
        last_session_id = None
        attempt = 0
        incomplete_used = 0
        error_used = 0
        timeout_used = 0
        all_session_files = []

        def _retry_info(**extra: Any) -> Dict[str, Any]:
            info: Dict[str, Any] = {
                "attempts": attempt + 1,
                "max_retries": effective_max_retries,
                "max_incomplete_retries": incomplete_cap,
                "incomplete_retries_used": incomplete_used,
                "retried": attempt > 0,
                "all_session_files": all_session_files,
                "last_opencode_session_id": last_session_id,
                "abandoned_session_id": getattr(
                    task, "abandoned_session_id", None
                ),
            }
            info.update(extra)
            return info

        # Error/timeout still use max_retries. Incomplete/compact uses its own
        # cap so a 20-compact job is not killed after 3 generic retries.
        max_total_attempts = (
            1 + int(effective_max_retries) + int(incomplete_cap)
        )

        while attempt < max_total_attempts:
            if _aborted():
                logger.info(
                    f"Abort before attempt {attempt + 1}: task_id={task.task_id}"
                )
                return _abort_result()

            logger.info(
                f"Agent attempt {attempt + 1}/{max_total_attempts}: "
                f"task_id={task.task_id}"
            )
            
            # Run the task with attempt number (0 = first attempt, 1+ = retries)
            result = await self.run_agent(
                task,
                on_output=on_output,
                on_complete=on_complete,
                on_progress=on_progress,
                on_session_file=on_session_file,
                on_session_id=on_session_id,
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
                result["retry_info"] = _retry_info(aborted=True)
                return result

            # Check if successful
            if result.get("returncode") == 0:
                logger.info(f"Agent succeeded on attempt {attempt + 1}: task_id={task.task_id}")
                result["retry_info"] = _retry_info()
                return result

            # Determine if we should retry
            should_retry = False
            retry_reason = ""

            if result.get("timed_out"):
                from src.opencode_sessions import compact_related_reasons

                to_reasons = list(result.get("incomplete_reasons") or [])
                compact_followup = (
                    bool(result.get("had_compact"))
                    or bool(result.get("assistant_asked_question"))
                    or int(result.get("compact_events") or 0) > 0
                    or compact_related_reasons(to_reasons)
                    or any("compact" in str(r).lower() for r in to_reasons)
                    or any("clarifying question" in str(r).lower() for r in to_reasons)
                )
                if compact_followup:
                    logger.warning(
                        f"Timeout after compact/question — not sending another "
                        f"user prompt: task_id={task.task_id} reasons={to_reasons}"
                    )
                elif retry_on_timeout and timeout_used < effective_max_retries:
                    should_retry = True
                    retry_reason = "timeout"
                    logger.warning(f"Agent timed out on attempt {attempt + 1}, will retry: task_id={task.task_id}")
            elif result.get("incomplete"):
                # Compact/incomplete was already waited out inside run_agent.
                # Another prompt (Continue, Finish-todos, or the original BUILD)
                # shows up as a user chat turn and races OpenCode auto-compact.
                # Clarifying questions are also one-pass: serve already sent at
                # most one unattended nudge; do not re-blast BUILD here.
                from src.opencode_sessions import compact_related_reasons

                reasons = list(result.get("incomplete_reasons") or [])
                compact_followup = (
                    bool(result.get("had_compact"))
                    or bool(result.get("assistant_asked_question"))
                    or int(result.get("compact_events") or 0) > 0
                    or compact_related_reasons(reasons)
                    or any("compact" in str(r).lower() for r in reasons)
                    or any("clarifying question" in str(r).lower() for r in reasons)
                )
                if compact_followup:
                    logger.warning(
                        f"Incomplete after compact/question — not sending another "
                        f"user message: task_id={task.task_id} reasons={reasons}"
                    )
                else:
                    incomplete_budget = (
                        incomplete_cap
                        if incomplete_cap > 0
                        else effective_max_retries
                    )
                    if incomplete_used < incomplete_budget:
                        should_retry = True
                        retry_reason = "incomplete_session"
                        logger.warning(
                            f"Incomplete session on attempt {attempt + 1}: "
                            f"task_id={task.task_id} "
                            f"(resume {incomplete_used + 1}/{incomplete_budget})"
                        )
            elif retry_on_error and error_used < effective_max_retries:
                should_retry = True
                retry_reason = "error"
                logger.warning(f"Agent failed with error on attempt {attempt + 1}, will retry: task_id={task.task_id}, returncode={result.get('returncode')}")
            else:
                logger.error(f"Agent failed and no more retries allowed: task_id={task.task_id}, attempt={attempt + 1}")

            if should_retry:
                self._resume_opencode_session_for_retry(
                    task,
                    result.get("opencode_session_id") or last_session_id,
                    why=retry_reason,
                    session_file=result.get("session_file"),
                    timed_out=bool(result.get("timed_out")),
                    stdout=result.get("stdout"),
                )
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

                if retry_reason == "incomplete_session":
                    incomplete_used += 1
                elif retry_reason == "timeout":
                    timeout_used += 1
                else:
                    error_used += 1
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
                result["retry_info"] = _retry_info(final_failure=True)
                return result

        # Should not reach here with normal control flow (loop always returns).
        # Defensive fallback kept for safety; last_result branch is effectively dead.
        logger.error(f"Unexpected fallback reached in run_agent_with_retry: task_id={task.task_id}")
        if last_result:  # pragma: no cover
            last_result["retry_info"] = _retry_info(retried=True, final_failure=True)
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
            "retry_info": _retry_info(retried=True, final_failure=True),
        }
