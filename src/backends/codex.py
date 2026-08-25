"""Codex CLI adapter — ``codex exec`` in the job temp clone."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.backends.base import BACKEND_CODEX, AgentRunRequest, AgentRunResult
from src.config import settings
from src.logger import logger
from src.orchestrator.agent_runner import _agent_subprocess_env


_THREAD_RE = re.compile(
    r"(?:thread_id|session_id|thread)\s*[:=]\s*[\"']?([0-9a-fA-F-]{16,})[\"']?"
)

# Codex holds an exclusive writer on the thread store. A second
# ``exec resume`` against the same id fails immediately with this.
_THREAD_LOCK_MARKERS = (
    "already has an active writer",
    "thread-store conflict",
    "thread/resume failed",
)

# Resume / new-thread prompts for Codex. Never reuse OpenCode
# finish-todos / "Continue the previous OpenCode session" text.
DEFAULT_CODEX_RESUME_PROMPT = (
    "UNATTENDED JOB: continue the work already started in this repository. "
    "Do not restart from scratch. Finish remaining implementation, "
    "verification, and local commit steps. Do not ask clarifying questions. "
    "Do not git push or open a merge request — the orchestrator delivers."
)
DEFAULT_CODEX_COLD_CONTINUE_PROMPT = (
    "UNATTENDED JOB: the previous Codex thread could not be resumed "
    "(thread store still had an active writer). Continue from the current "
    "files and git state. Do not restart from scratch. Finish remaining "
    "implementation, verification, and local commit steps. Do not ask "
    "clarifying questions. Do not git push or open a merge request — "
    "the orchestrator delivers."
)

# Isolated job home only overrides these; everything else comes from the
# operator's ~/.codex (or $CODEX_HOME) config — same idea as OpenCode.
_ISOLATED_OVERRIDE_KEYS = frozenset({"approval_policy", "sandbox_mode", "model"})


_CODEX_MODEL_LINE = re.compile(
    r"^model\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|(\S+))\s*(?:#.*)?$"
)


def user_codex_home() -> Path:
    """Operator Codex config dir (not the per-job isolated home)."""
    raw = (os.environ.get("CODEX_HOME") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def models_from_codex_config(user_config: str) -> List[str]:
    """Model ids declared in the operator's Codex config.toml (any table)."""
    found: List[str] = []
    seen: set[str] = set()
    for line in (user_config or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _CODEX_MODEL_LINE.match(stripped)
        if not match:
            continue
        mid = (match.group(1) or match.group(2) or match.group(3) or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            found.append(mid)
    return found


def list_codex_config_models() -> tuple[List[str], Optional[str], Optional[str]]:
    """Return (model ids, config path, error) from the operator Codex home."""
    home = user_codex_home()
    path = home / "config.toml"
    if not path.is_file():
        return [], str(path), None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return [], str(path), f"Could not read Codex config: {e}"
    return models_from_codex_config(text), str(path), None


def _codex_home_for(cwd: str) -> Path:
    """Stable isolated Codex home for this clone so ``exec resume`` can find threads.

    Never written into the customer work tree. Tests isolate ``Path.cwd()``.
    """
    key = hashlib.sha1(os.path.abspath(cwd or os.getcwd()).encode("utf-8")).hexdigest()[
        :16
    ]
    home = Path.cwd() / ".jira-agent" / "codex-homes" / key
    home.mkdir(parents=True, exist_ok=True)
    return home


def _usable_cli(path: Path) -> bool:
    if not path.is_file():
        return False
    # Windows PE shipped in vendor/bin must not win on Linux/WSL.
    if path.suffix.lower() == ".exe" and os.name != "nt":
        return False
    return True


def build_codex_config_toml(*, model: str = "", user_config: str = "") -> str:
    """Isolated Codex home: unattended flags + DEFAULT_MODEL over the user's config.

    Provider, base URL, wire API, and API keys stay in the operator's Codex
    config (``~/.codex/config.toml`` / ``auth.json``). Never write this file
    into the customer clone.
    """
    header = [
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
    ]
    mid = (model or "").strip()
    if mid:
        header.append(f"model = {json.dumps(mid)}")
    kept: List[str] = []
    in_table = False
    for line in (user_config or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("["):
            in_table = True
        if not in_table:
            match = re.match(r"^([A-Za-z0-9_]+)\s*=", stripped)
            if match and match.group(1) in _ISOLATED_OVERRIDE_KEYS:
                continue
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    text = "\n".join(header)
    if kept:
        text += "\n\n" + "\n".join(kept)
    if not text.endswith("\n"):
        text += "\n"
    return text


def seed_isolated_codex_home(home: Path, *, model: str = "") -> None:
    """Copy operator auth + merge config into the job-local Codex home."""
    home.mkdir(parents=True, exist_ok=True)
    user = user_codex_home()
    user_cfg = ""
    try:
        same = user.resolve() == home.resolve()
    except OSError:
        same = False
    if not same and user.is_dir():
        auth = user / "auth.json"
        if auth.is_file():
            try:
                (home / "auth.json").write_bytes(auth.read_bytes())
            except OSError as e:
                logger.debug(f"[codex] could not copy operator auth.json: {e}")
        cfg = user / "config.toml"
        if cfg.is_file():
            try:
                user_cfg = cfg.read_text(encoding="utf-8")
            except OSError as e:
                logger.debug(f"[codex] could not read operator config.toml: {e}")
    (home / "config.toml").write_text(
        build_codex_config_toml(model=model, user_config=user_cfg),
        encoding="utf-8",
    )


def resolve_codex_cli(cli: str = "") -> str:
    """Prefer an explicit path, then Codex's default install, then vendor/PATH."""
    raw = (cli or "").strip() or "codex"
    if _usable_cli(Path(raw)):
        return raw
    names = ("codex.exe", "codex") if os.name == "nt" else ("codex",)
    folders: List[Path] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if local:
        folders.append(Path(local) / "Programs" / "OpenAI" / "Codex" / "bin")
        folders.append(Path(local) / "OpenAI" / "Codex" / "bin")
    if home:
        folders.append(Path(home) / "AppData" / "Local" / "Programs" / "OpenAI" / "Codex" / "bin")
        folders.append(Path(home) / ".local" / "bin")
    try:
        folders.append(Path(__file__).resolve().parents[2] / "vendor" / "bin")
    except Exception:
        pass
    cwd = Path.cwd()
    folders.append(cwd / "vendor" / "bin")
    seen: set[str] = set()
    for folder in folders:
        key = str(folder)
        if key in seen:
            continue
        seen.add(key)
        for name in names:
            cand = folder / name
            if _usable_cli(cand):
                return str(cand)
    return raw


def build_codex_argv(
    *,
    cli: str,
    prompt: str,
    model: str = "",
    resume_id: str = "",
) -> List[str]:
    """``codex exec`` argv. Prompt last. No secrets."""
    exe = (cli or "codex").strip() or "codex"
    text = (prompt or "").strip()
    if "UNATTENDED JOB:" not in text:
        text = (
            "UNATTENDED JOB: do not ask clarifying questions, confirmations, "
            "or wait for a human. Choose defaults and finish the work.\n\n"
            + text
        )
    # Full auto: no approval prompts / no interactive Q&A. The job is
    # unattended (no human reply path).
    cmd = [
        exe,
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
    ]
    rid = (resume_id or "").strip()
    mid = (model or "").strip()
    if mid and not rid:
        cmd.extend(["-m", mid])
    # `exec resume` is a subcommand — flags must come before it.
    if rid:
        cmd.extend(["resume", rid])
    cmd.append(text)
    return cmd


def _codex_cmd_for_log(argv: List[str]) -> str:
    """Join argv for logs; replace the prompt (last arg) so we never dump the kit."""
    if not argv:
        return ""
    parts = list(argv)
    last = parts[-1]
    if last and not last.startswith("-") and last not in {"exec", "resume"}:
        parts[-1] = f"<prompt {len(last)} chars>"
    return " ".join(parts)


def is_codex_thread_lock_error(text: str) -> bool:
    """True when Codex refused resume because another writer holds the thread."""
    blob = (text or "").lower()
    return any(marker in blob for marker in _THREAD_LOCK_MARKERS)


async def _await_process_exit(proc: Any, *, seconds: float) -> bool:
    """Wait until ``proc`` exits. Returns True if it is gone."""
    if proc is None:
        return True
    if getattr(proc, "returncode", None) is not None:
        return True
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.1, float(seconds)))
        return True
    except (asyncio.TimeoutError, Exception):
        return getattr(proc, "returncode", None) is not None


def parse_codex_thread_id(text: str) -> Optional[str]:
    """Best-effort thread/session id from JSONL or log text."""
    if not (text or "").strip():
        return None
    last: Optional[str] = None
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict):
                tid = obj.get("thread_id")
                thread = obj.get("thread")
                if not tid and isinstance(thread, dict):
                    tid = thread.get("id")
                if tid:
                    last = str(tid).strip()
                    continue
        m = _THREAD_RE.search(raw)
        if m:
            last = m.group(1)
    return last


def summarize_codex_exec_line(line: str, *, limit: int = 220) -> Optional[str]:
    """Operator-facing one-liner for a ``codex exec --json`` event (or stderr)."""
    raw = (line or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        if raw.startswith("[codex]"):
            return raw
        return f"[codex] {raw[:limit]}"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return f"[codex] {raw[:limit]}"
    if not isinstance(obj, dict):
        return None
    etype = str(obj.get("type") or "")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
    item_type = str(item.get("type") or "")
    status = str(item.get("status") or "").strip()

    def _clip(text: object) -> str:
        s = " ".join(str(text or "").split())
        if len(s) <= limit:
            return s
        return s[: max(0, limit - 1)] + "…"

    if etype == "thread.started":
        tid = str(obj.get("thread_id") or "").strip()
        return f"[codex] thread {tid}" if tid else "[codex] thread started"
    if etype == "turn.failed" or etype == "error":
        msg = obj.get("message") or (obj.get("error") or {})
        if isinstance(msg, dict):
            msg = msg.get("message") or msg.get("error") or msg
        return f"[codex] error: {_clip(msg)}" if msg else "[codex] error"
    if etype in {"turn.started", "turn.completed", "item.updated"}:
        return None
    if item_type == "command_execution" or item_type == "command":
        cmd = _clip(item.get("command") or item.get("cmd") or "command")
        if etype == "item.started" or status == "in_progress":
            return f"[codex] running: {cmd}"
        code = item.get("exit_code")
        tail = f" exit={code}" if code is not None and str(code) != "" else ""
        return f"[codex] command{tail}: {cmd}"
    if item_type == "agent_message" or item_type == "message":
        text = _clip(item.get("text") or item.get("content") or "")
        return f"[codex] assistant: {text}" if text else None
    if item_type == "reasoning":
        text = _clip(item.get("text") or "")
        return f"[codex] thinking: {text}" if text else None
    if item_type in {"file_change", "fileChange"}:
        changes = item.get("changes") or item.get("files") or []
        names: List[str] = []
        if isinstance(changes, list):
            for ch in changes[:8]:
                if isinstance(ch, dict):
                    names.append(str(ch.get("path") or ch.get("filename") or ""))
                elif ch:
                    names.append(str(ch))
        names = [n for n in names if n]
        return f"[codex] files: {', '.join(names)}" if names else "[codex] files changed"
    if item_type in {"mcp_tool_call", "mcpToolCall"}:
        tool = item.get("tool") or item.get("name") or "mcp"
        server = item.get("server") or ""
        label = f"{server}/{tool}" if server else str(tool)
        return f"[codex] mcp: {label}"
    if item_type in {"web_search", "webSearch"}:
        q = _clip(item.get("query") or item.get("q") or "")
        return f"[codex] search: {q}" if q else "[codex] web search"
    if item_type in {"todo_list", "todoList"}:
        return "[codex] todos updated"
    if item_type == "error":
        return f"[codex] error: {_clip(item.get('message') or item.get('text') or 'failed')}"
    return None


def daemon_worthy_codex_summary(summary: Optional[str]) -> bool:
    """True for daemon-tab lines (errors only). Chatter belongs in Transcript."""
    s = (summary or "").strip()
    if not s:
        return False
    return s.startswith("[codex] error")


def extract_codex_failure_detail(blob: str, *, limit: int = 8000) -> str:
    """Structured failure dump from a ``codex exec`` JSONL stream."""
    errors: List[str] = []
    failed_cmds: List[str] = []
    last_assistant = ""
    last_error = ""
    for raw in (blob or "").splitlines():
        line = (raw or "").strip()
        if not line:
            continue
        if not line.startswith("{"):
            if "error" in line.lower() or "fail" in line.lower():
                errors.append(line[:500])
                last_error = line[:800]
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        etype = str(obj.get("type") or "")
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        item_type = str(item.get("type") or "")
        if etype in {"turn.failed", "error"} or item_type == "error":
            msg = obj.get("message") or obj.get("error") or item.get("message") or item.get("text")
            if isinstance(msg, dict):
                msg = msg.get("message") or msg.get("error") or msg
            text = " ".join(str(msg or "error").split())
            last_error = text
            errors.append(text[:800])
            continue
        if item_type in {"command_execution", "command"}:
            code = item.get("exit_code")
            if code is None or str(code) in {"", "0"}:
                continue
            cmd = str(item.get("command") or item.get("cmd") or "command")
            out = item.get("aggregated_output") or item.get("output") or item.get("stderr") or ""
            tail = " ".join(str(out).split())[:400]
            row = f"exit_code={code} cmd={cmd}"
            if tail:
                row += f" output={tail}"
            failed_cmds.append(row)
            continue
        if item_type in {"agent_message", "message"}:
            text = " ".join(str(item.get("text") or item.get("content") or "").split())
            if text:
                last_assistant = text[:2000]
    parts: List[str] = []
    if last_error:
        parts.append(f"exit_message: {last_error}")
    if errors:
        parts.append("errors:")
        parts.extend(f"  - {e}" for e in errors[-12:])
    if failed_cmds:
        parts.append("failed_commands:")
        parts.extend(f"  - {c}" for c in failed_cmds[-12:])
    if last_assistant:
        parts.append(f"last_assistant: {last_assistant}")
    return "\n".join(parts)[:limit]


def format_failure_report(
    *,
    backend: str,
    returncode: Any,
    stderr: str = "",
    stdout: str = "",
    timed_out: bool = False,
    incomplete: bool = False,
    incomplete_reasons: Optional[List[Any]] = None,
    session_id: str = "",
    duration_s: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Full operator-facing failure dump for the Daemon tab."""
    lines = [
        f"{backend} failure",
        f"  exit_code={returncode}",
        f"  timed_out={bool(timed_out)}",
        f"  incomplete={bool(incomplete)}",
        f"  reasons={list(incomplete_reasons or []) or '-'}",
        f"  session_id={session_id or '-'}",
    ]
    if duration_s is not None:
        lines.append(f"  duration_s={duration_s:.1f}")
    if extra:
        skip = {"stdout", "stderr", "session_file", "serve_turns"}
        for key, val in extra.items():
            if key in skip or val in (None, "", [], {}):
                continue
            lines.append(f"  {key}={val}")
    if stderr and stderr.strip():
        lines.append("  --- stderr ---")
        lines.append(stderr.strip()[-8000:])
    parsed = extract_codex_failure_detail(stdout) if stdout else ""
    if parsed:
        lines.append("  --- exec detail ---")
        lines.append(parsed)
    elif stdout and stdout.strip():
        lines.append("  --- stdout (tail) ---")
        lines.append(stdout.strip()[-4000:])
    return "\n".join(lines)


def _looks_like_session_id(sid: str) -> bool:
    s = (sid or "").strip()
    if not s:
        return False
    if s.startswith("ses_") or s.startswith("thread_"):
        return True
    return s.count("-") >= 4 and len(s) >= 16


class CodexBackend:
    """Unattended ``codex exec`` in the isolated job clone."""

    name = BACKEND_CODEX

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        from src.orchestrator.agent_runner import AgentRunner

        timeout_seconds = float(request.timeout_seconds or 1800)
        from src.log_context import set_issue_key, set_job_id

        if request.issue_key:
            set_issue_key(request.issue_key)
        if request.job_id:
            set_job_id(request.job_id)
        handle = request.handle
        log_lines = request.log_lines if request.log_lines is not None else []
        cli = resolve_codex_cli(
            getattr(settings, "codex_cli", None) or "codex"
        )
        model = (request.model or getattr(settings, "default_model", "") or "").strip()
        resume = (request.session_id or "").strip()
        cwd = (
            str(request.working_directory)
            if request.working_directory
            else os.getcwd()
        )

        home = _codex_home_for(cwd)
        try:
            seed_isolated_codex_home(home, model=model)
        except OSError as e:
            return AgentRunResult(
                returncode=-1,
                stderr=f"[codex] could not write isolated config: {e}",
                backend=self.name,
            )

        argv = build_codex_argv(
            cli=cli,
            prompt=request.prompt or "",
            model=model,
            resume_id=resume if _looks_like_session_id(resume) else "",
        )
        if os.name != "nt":
            import shutil

            stdbuf = shutil.which("stdbuf")
            if stdbuf:
                argv = [stdbuf, "-oL", "-eL", *argv]
        env = _agent_subprocess_env(request.working_directory)
        env["CODEX_HOME"] = str(home)
        has_codex_key = bool((env.get("CODEX_API_KEY") or "").strip())
        has_openai_key = bool((env.get("OPENAI_API_KEY") or "").strip())
        logger.info(
            f"[codex] host env keys present: "
            f"CODEX_API_KEY={has_codex_key} OPENAI_API_KEY={has_openai_key}"
        )

        handle["mode"] = "codex"
        handle["backend"] = self.name
        handle["cancel"] = False
        handle["session_id"] = resume or None

        def _emit(stream: str, line: str) -> None:
            log_lines.append(line)
            if request.on_output:
                try:
                    request.on_output(stream, line)
                except Exception:
                    pass
            summary = summarize_codex_exec_line(line)
            # Assistant / tool chatter stays in the session log (Transcript).
            # Daemon tab only gets errors here; start/exit are logged around exec.
            if summary and daemon_worthy_codex_summary(summary):
                logger.info(summary)
            sid = parse_codex_thread_id(line)
            if sid:
                handle["session_id"] = sid
                if request.on_session:
                    try:
                        request.on_session(sid)
                    except Exception:
                        pass

        _emit("stdout", f"[codex] cwd={cwd} model={model or '(default)'}")
        logger.info(
            f"[codex] start command: {_codex_cmd_for_log(argv)} "
            f"cwd={cwd} model={model or '(default)'} "
            f"timeout={int(timeout_seconds)}s resume={resume or 'no'}"
        )
        logger.debug(f"[codex] cli={cli} home={home}")
        proc = None
        timed_out = False
        started = asyncio.get_event_loop().time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            handle["proc"] = proc
            logger.info(f"[codex] process started pid={proc.pid}")

            async def _pump(stream_name: str, reader: Any) -> None:
                if reader is None:
                    return
                while True:
                    if handle.get("cancel") or (
                        request.should_abort and request.should_abort()
                    ):
                        break
                    raw = await reader.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\n")
                    _emit(stream_name, line)

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _pump("stdout", proc.stdout),
                        _pump("stderr", proc.stderr),
                    ),
                    timeout=timeout_seconds,
                )
                await asyncio.wait_for(proc.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                timed_out = True
                self.cancel(handle)
                gone = await _await_process_exit(proc, seconds=15.0)
                if not gone:
                    self.cancel(handle)
                    gone = await _await_process_exit(proc, seconds=10.0)
                if not gone:
                    logger.warning(
                        f"[codex] process still alive after timeout kill "
                        f"pid={getattr(proc, 'pid', None)}"
                    )
                # Windows keeps the thread-store writer until the process
                # (and its file handles) are actually gone.
                await asyncio.sleep(0.75)
        except FileNotFoundError:
            report = format_failure_report(
                backend="codex",
                returncode=-1,
                stderr=(
                    f"[codex] binary not found: {cli}. "
                    "Install Codex CLI or set CODEX_CLI."
                ),
                stdout="\n".join(log_lines),
                session_id=str(handle.get("session_id") or ""),
            )
            logger.error(report)
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=report,
                session_id=handle.get("session_id"),
                backend=self.name,
            )
        except Exception as e:
            report = format_failure_report(
                backend="codex",
                returncode=-1,
                stderr=f"[codex] {type(e).__name__}: {e}",
                stdout="\n".join(log_lines),
                session_id=str(handle.get("session_id") or ""),
            )
            logger.error(report)
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=report,
                session_id=handle.get("session_id"),
                backend=self.name,
            )
        finally:
            handle["proc"] = None

        elapsed = asyncio.get_event_loop().time() - started
        code = int(getattr(proc, "returncode", None) or ( -1 if timed_out else 0))
        blob = "\n".join(log_lines)
        still_running = (
            proc is not None and getattr(proc, "returncode", None) is None
        )
        locked = is_codex_thread_lock_error(blob) or (
            timed_out and still_running
        )
        if timed_out:
            report = format_failure_report(
                backend="codex",
                returncode=-1,
                stderr=f"[codex] timed out after {int(timeout_seconds)}s "
                f"pid={getattr(proc, 'pid', None)}"
                + (" writer_still_alive=true" if still_running else ""),
                stdout=blob,
                timed_out=True,
                session_id=str(handle.get("session_id") or ""),
                duration_s=elapsed,
                extra={"thread_locked": bool(locked or still_running)},
            )
            logger.error(report)
            return AgentRunResult(
                returncode=-1,
                stdout=blob,
                stderr=report,
                session_id=handle.get("session_id"),
                timed_out=True,
                backend=self.name,
                extra={"thread_locked": bool(locked or still_running)},
            )
        if locked:
            report = format_failure_report(
                backend="codex",
                returncode=code if code else -1,
                stderr="[codex] thread-store conflict: already has an active writer",
                stdout=blob,
                incomplete=False,
                incomplete_reasons=["codex thread locked"],
                session_id=str(handle.get("session_id") or ""),
                duration_s=elapsed,
                extra={"thread_locked": True},
            )
            logger.error(report)
            return AgentRunResult(
                returncode=code if code else -1,
                stdout=blob,
                stderr=report,
                session_id=handle.get("session_id"),
                incomplete=False,
                incomplete_reasons=["codex thread locked"],
                progress=0,
                backend=self.name,
                extra={"thread_locked": True},
            )
        if code != 0:
            report = format_failure_report(
                backend="codex",
                returncode=code,
                stderr=f"[codex] process exit_code={code}",
                stdout=blob,
                session_id=str(handle.get("session_id") or ""),
                duration_s=elapsed,
            )
            logger.error(report)
            return AgentRunResult(
                returncode=code,
                stdout=blob,
                stderr=report,
                session_id=handle.get("session_id"),
                incomplete=False,
                incomplete_reasons=[],
                progress=0,
                backend=self.name,
            )
        logger.info(
            f"[codex] exit ok returncode={code} duration={elapsed:.1f}s "
            f"thread={handle.get('session_id') or '-'}"
        )
        return AgentRunResult(
            returncode=code,
            stdout=blob,
            stderr="",
            session_id=handle.get("session_id"),
            incomplete=False,
            incomplete_reasons=[],
            progress=100,
            backend=self.name,
        )

    def cancel(self, handle: Dict[str, Any]) -> None:
        handle["cancel"] = True
        proc = handle.get("proc")
        logger.info(f"[codex] cancel requested pid={getattr(proc, 'pid', None)}")
        if proc is None:
            return
        try:
            from src.process_kill import kill_process_tree

            kill_process_tree(proc, force=True)
        except Exception as e:
            logger.debug(f"codex cancel failed: {e}")
            try:
                proc.kill()
            except Exception:
                pass
