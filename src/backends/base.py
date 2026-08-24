"""Shared contract for unattended agent workers (OpenCode, Codex, …)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol


BACKEND_OPENCODE = "opencode"
BACKEND_CODEX = "codex"
SUPPORTED_BACKENDS = (BACKEND_OPENCODE, BACKEND_CODEX)

_BACKEND_ALIASES = {
    "opencode": BACKEND_OPENCODE,
    "open-code": BACKEND_OPENCODE,
    "omo": BACKEND_OPENCODE,
    "oh-my-openagent": BACKEND_OPENCODE,
    "codex": BACKEND_CODEX,
    "openai-codex": BACKEND_CODEX,
    "openai": BACKEND_CODEX,
}


def normalize_backend_name(raw: Optional[str]) -> str:
    """Map a settings / {params} token to a known backend id."""
    key = (raw or "").strip().lower().replace("_", "-")
    if not key:
        return ""
    return _BACKEND_ALIASES.get(key, key if key in SUPPORTED_BACKENDS else "")


OnOutput = Callable[[str, str], None]
OnSession = Callable[[str], None]
ShouldAbort = Callable[[], bool]


@dataclass
class AgentRunRequest:
    """One unattended turn. Same shape for every backend."""

    prompt: str
    title: str = ""
    model: str = ""
    agent: str = ""
    session_id: Optional[str] = None
    issue_key: Optional[str] = None
    job_id: Optional[str] = None
    working_directory: Optional[Path] = None
    timeout_seconds: float = 1800.0
    abort_busy_session: bool = True
    handle: Dict[str, Any] = field(default_factory=dict)
    on_output: Optional[OnOutput] = None
    on_session: Optional[OnSession] = None
    should_abort: Optional[ShouldAbort] = None
    log_lines: Optional[List[str]] = None


@dataclass
class AgentRunResult:
    """Normalized worker result. Processor/orchestrator only see this."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    session_id: Optional[str] = None
    incomplete: bool = False
    incomplete_reasons: List[str] = field(default_factory=list)
    timed_out: bool = False
    progress: int = 0
    backend: str = BACKEND_OPENCODE
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_agent_result(self, task_id: str, *, session_file: str) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "session_file": session_file,
            "opencode_session_id": self.session_id,
            "session_id": self.session_id,
            "incomplete": self.incomplete,
            "incomplete_reasons": list(self.incomplete_reasons),
            "timed_out": self.timed_out,
            "progress": self.progress,
            "mode": self.backend,
            "backend": self.backend,
            **self.extra,
        }


class AgentBackend(Protocol):
    """Unattended coding worker. OpenCode and Codex implement this."""

    name: str

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Run one prompt in ``request.working_directory`` until done or timeout."""

    def cancel(self, handle: Dict[str, Any]) -> None:
        """Best-effort stop of an in-flight run (abort HTTP or kill process)."""
