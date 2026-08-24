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


def resolve_codex_wire_api(*, base_url: str, wire_api: str = "") -> str:
    """Codex 0.149+ only accepts ``responses``. ``chat`` is kept if set explicitly."""
    explicit = (wire_api or "").strip().lower()
    if explicit in ("responses", "chat"):
        return explicit
    return "responses"


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


def build_codex_config_toml(
    *,
    model: str,
    base_url: str,
    wire_api: str = "",
    api_key: str = "",
) -> str:
    """Isolated Codex home config: never write this into the customer clone."""
    mid = (model or "").strip()
    url = (base_url or "").strip().rstrip("/")
    wire = resolve_codex_wire_api(base_url=url, wire_api=wire_api)
    have_key = bool((api_key or "").strip())
    lines = [
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
    ]
    if have_key:
        lines.append('preferred_auth_method = "apikey"')
    if mid:
        lines.append(f"model = {json.dumps(mid)}")
    if url:
        if not url.endswith("/v1"):
            url = url + "/v1"
        lines.append('model_provider = "yaver"')
        lines.append("")
        lines.append("[model_providers.yaver]")
        lines.append('name = "Yaver"')
        lines.append(f"base_url = {json.dumps(url)}")
        if have_key:
            lines.append('env_key = "CODEX_API_KEY"')
        else:
            # Zen free models (and some OSS gateways) accept unauthenticated
            # Responses calls. A dummy key becomes HTTP 401.
            lines.append("requires_openai_auth = false")
        lines.append(f"wire_api = {json.dumps(wire)}")
    return "\n".join(lines) + "\n"


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
        api_key = (getattr(settings, "codex_api_key", None) or "").strip()
        if not api_key:
            api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        base_url = (getattr(settings, "codex_base_url", None) or "").strip()
        wire_api = (getattr(settings, "codex_wire_api", None) or "").strip()
        resume = (request.session_id or "").strip()
        cwd = (
            str(request.working_directory)
            if request.working_directory
            else os.getcwd()
        )

        home = _codex_home_for(cwd)
        try:
            (home / "config.toml").write_text(
                build_codex_config_toml(
                    model=model,
                    base_url=base_url,
                    wire_api=wire_api,
                    api_key=api_key,
                ),
                encoding="utf-8",
            )
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
        if api_key:
            env["CODEX_API_KEY"] = api_key
            env["OPENAI_API_KEY"] = api_key
        if base_url:
            env["OPENAI_BASE_URL"] = base_url.rstrip("/")

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
            if summary:
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
        logger.debug(
            f"[codex] cli={cli} home={home} base_url={base_url or '(default)'} "
            f"wire_api={wire_api or '(default)'} has_api_key={bool(api_key)}"
        )
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
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except Exception:
                    pass
        except FileNotFoundError:
            logger.error(f"[codex] binary not found: {cli}")
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=(
                    f"[codex] binary not found: {cli}. "
                    "Install Codex CLI or set CODEX_CLI."
                ),
                session_id=handle.get("session_id"),
                backend=self.name,
            )
        except Exception as e:
            logger.warning(f"[codex] exec failed: {e}")
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[codex] {e}",
                session_id=handle.get("session_id"),
                backend=self.name,
            )
        finally:
            handle["proc"] = None

        elapsed = asyncio.get_event_loop().time() - started
        code = int(getattr(proc, "returncode", None) or ( -1 if timed_out else 0))
        if timed_out:
            logger.warning(
                f"[codex] exec timed out after {int(timeout_seconds)}s "
                f"(ran {elapsed:.1f}s) pid={getattr(proc, 'pid', None)}"
            )
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[codex] timed out after {int(timeout_seconds)}s",
                session_id=handle.get("session_id"),
                timed_out=True,
                backend=self.name,
            )
        logger.info(
            f"[codex] exec finished returncode={code} duration={elapsed:.1f}s "
            f"thread={handle.get('session_id') or '-'}"
        )
        return AgentRunResult(
            returncode=code,
            stdout="\n".join(log_lines),
            stderr="" if code == 0 else f"[codex] exit {code}",
            session_id=handle.get("session_id"),
            incomplete=code != 0,
            incomplete_reasons=["codex non-zero exit"] if code != 0 else [],
            progress=100 if code == 0 else 0,
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
