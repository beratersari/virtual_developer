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
            sid = parse_codex_thread_id(line)
            if sid:
                handle["session_id"] = sid
                if request.on_session:
                    try:
                        request.on_session(sid)
                    except Exception:
                        pass

        _emit("stdout", f"[codex] cwd={cwd} model={model or '(default)'}")
        proc = None
        timed_out = False
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
            logger.warning(f"codex exec failed: {e}")
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[codex] {e}",
                session_id=handle.get("session_id"),
                backend=self.name,
            )
        finally:
            handle["proc"] = None

        code = int(getattr(proc, "returncode", None) or ( -1 if timed_out else 0))
        if timed_out:
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[codex] timed out after {int(timeout_seconds)}s",
                session_id=handle.get("session_id"),
                timed_out=True,
                backend=self.name,
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
        if proc is None:
            return
        try:
            from src.orchestrator.agent_runner import AgentRunner

            killer = AgentRunner(working_directory=None)
            killer._kill_process_tree(proc, force=False)
            if getattr(proc, "returncode", None) is None:
                killer._kill_process_tree(proc, force=True)
        except Exception as e:
            logger.debug(f"codex cancel failed: {e}")
            try:
                proc.kill()
            except Exception:
                pass
