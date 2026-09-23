"""Claude Code CLI adapter. Unattended ``claude --print`` in the job clone."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.backends.base import (
    BACKEND_CLAUDE,
    AgentRunRequest,
    AgentRunResult,
    is_claude_session_id,
)
from src.config import settings
from src.logger import logger

DEFAULT_CLAUDE_NUDGE_PROMPT = (
    "You are running unattended inside a daemon. There is no human in this "
    "session and no one will answer questions. Do not ask clarifying "
    "questions, confirmation, or multiple-choice options. Choose the safest "
    "defaults consistent with AGENTS.md, the repository, and the original "
    "issue. Finish the remaining work without waiting. Do not git push or "
    "open a merge request — the orchestrator delivers the branch after you stop."
)
DEFAULT_CLAUDE_PLAN_NUDGE_PROMPT = (
    "You are running unattended inside a daemon. There is no human in this "
    "session and no one will answer questions. Choose the safest defaults "
    "and finish the plan file only. Do not implement product code, install "
    "tools, compile, or commit. Do not git push or open a merge request."
)
DEFAULT_CLAUDE_RESUME_PROMPT = (
    "UNATTENDED JOB: continue the work already started in this repository. "
    "Do not restart from scratch. Do not ask clarifying questions. "
    "Do not git push or open a merge request — the orchestrator delivers."
)

_QUESTION_TOOLS = frozenset(
    {
        "askuserquestion",
        "ask_user_question",
    }
)


def resolve_claude_cli(cli: str = "") -> str:
    """Prefer an explicit path, then ``claude`` on PATH."""
    raw = (cli or "").strip() or str(getattr(settings, "claude_cli", "") or "").strip()
    raw = raw or "claude"
    path = Path(raw)
    if path.is_file():
        return str(path)
    found = shutil.which(raw)
    if found:
        return found
    for name in ("claude.cmd", "claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return found
    return raw


def resolve_claude_agent_name(agent: str) -> str:
    """Map a workflow agent id onto a Claude agent file name."""
    from src.orchestrator.agent_runner import resolve_opencode_agent_name

    name = resolve_opencode_agent_name((agent or "").strip()) or "derman-build"
    if name in {"build", "plan"}:
        return f"derman-{name}"
    return name


def _is_plan_agent(agent: str) -> bool:
    return "plan" in (agent or "").lower()


def build_claude_argv(
    *,
    cli: str,
    prompt: str,
    model: str = "",
    agent: str = "",
    resume_id: str = "",
) -> List[str]:
    """``claude --print`` argv. Prompt last. No secrets."""
    exe = (cli or "claude").strip() or "claude"
    text = (prompt or "").strip()
    if "UNATTENDED JOB:" not in text and "no human in this" not in text.lower():
        text = (
            "UNATTENDED JOB: do not ask clarifying questions, confirmations, "
            "or wait for a human. Choose defaults and finish the work. "
            "Do not git push or open a merge request.\n\n"
            + text
        )
    cmd = [
        exe,
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--disallowedTools",
        "AskUserQuestion",
    ]
    agent_name = resolve_claude_agent_name(agent)
    if agent_name:
        cmd.extend(["--agent", agent_name])
    mid = (model or "").strip()
    if mid:
        cmd.extend(["--model", mid])
    rid = (resume_id or "").strip()
    if is_claude_session_id(rid):
        cmd.extend(["--resume", rid])
    cmd.append(text)
    return cmd


def _windows_spawn_argv(argv: List[str]) -> List[str]:
    """``.cmd`` / ``.bat`` shims need ``cmd /c`` or CreateProcess fails."""
    if os.name != "nt" or not argv:
        return argv
    exe = argv[0].lower()
    if exe.endswith(".cmd") or exe.endswith(".bat"):
        return ["cmd", "/c", *argv]
    return argv


def claude_child_env() -> Dict[str, str]:
    """Process env for ``claude``. URL and token come from settings when set."""
    env = dict(os.environ)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    base = (getattr(settings, "anthropic_base_url", None) or "").strip()
    token = (getattr(settings, "anthropic_auth_token", None) or "").strip()
    if base:
        env["ANTHROPIC_BASE_URL"] = base
    if token:
        env["ANTHROPIC_AUTH_TOKEN"] = token
        env.setdefault("ANTHROPIC_API_KEY", token)
    return env


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(p for p in parts if p).strip()


def _tool_names(content: Any) -> List[str]:
    if not isinstance(content, list):
        return []
    names: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"tool_use", "tool_call"}:
            continue
        name = str(block.get("name") or "").strip()
        if name:
            names.append(name)
    return names


_CLI_TAG = re.compile(r"\[claude-code:[^\]]+\]")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _json_object_end(text: str, start: int) -> Optional[int]:
    """Index just past the object that begins at *start*, or None."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _strip_cli_diagnostics(raw: str) -> str:
    """Drop ``[claude-code:…]`` tags and a JSON object that follows one."""
    text = _ANSI.sub("", (raw or "").replace("\r\n", "\n").lstrip("\ufeff"))
    out: List[str] = []
    i = 0
    while True:
        match = _CLI_TAG.search(text, i)
        if not match:
            out.append(text[i:])
            break
        out.append(text[i:match.start()])
        j = match.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        if j < len(text) and text[j] == "{":
            end = _json_object_end(text, j)
            if end is not None:
                j = end
        i = j
    cleaned = "".join(out)
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in cleaned.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _text_from_content(value)


def _is_diagnostic_obj(event: Dict[str, Any]) -> bool:
    return (
        "model" in event
        and "query_source" in event
        and "result" not in event
        and "message" not in event
        and not event.get("type")
    )


def _is_claude_event(event: Dict[str, Any]) -> bool:
    kind = str(event.get("type") or "")
    if kind in {"result", "assistant", "user", "system"} or kind.startswith("rate_limit"):
        return True
    if event.get("session_id") and (
        "result" in event or event.get("message") is not None
    ):
        return True
    return False


def _take_claude_event(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    if _is_diagnostic_obj(event):
        state["saw"] = True
        return
    if not _is_claude_event(event):
        return
    state["saw"] = True
    sid = str(event.get("session_id") or "").strip()
    if sid:
        state["session_id"] = sid
    kind = str(event.get("type") or "")
    if kind in {"system", "user"} or kind.startswith("rate_limit"):
        return
    if kind == "assistant" or isinstance(event.get("message"), dict):
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")
        text = _text_from_content(content)
        if text:
            state["assistant"].append(text)
        state["tools"].extend(_tool_names(content))
        return
    if kind == "result" or "result" in event or event.get("is_error") is not None:
        state["is_error"] = bool(event.get("is_error"))
        result = _as_text(event.get("result")).strip()
        if result:
            state["result"] = result
        err = _as_text(event.get("error")).strip()
        if err:
            state["error"] = err


def _taken_text(state: Dict[str, Any]) -> str:
    result = str(state.get("result") or "").strip()
    if result:
        return result
    parts: List[str] = []
    for chunk in state.get("assistant") or []:
        bit = str(chunk).strip()
        if bit and (not parts or parts[-1] != bit):
            parts.append(bit)
    if parts:
        return "\n".join(parts)
    if state.get("is_error") and str(state.get("error") or "").strip():
        return str(state.get("error") or "").strip()
    return ""


def _empty_take() -> Dict[str, Any]:
    return {
        "result": "",
        "assistant": [],
        "error": "",
        "is_error": False,
        "session_id": "",
        "tools": [],
        "saw": False,
    }


def _try_claude_document(text: str) -> Optional[Dict[str, Any]]:
    trimmed = (text or "").strip()
    if not trimmed.startswith("{") and not trimmed.startswith("["):
        return None
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    state = _empty_take()
    if isinstance(parsed, dict):
        if not _is_claude_event(parsed) and not _is_diagnostic_obj(parsed):
            return None
        _take_claude_event(state, parsed)
        return state
    if not isinstance(parsed, list) or not parsed:
        return None
    events = [item for item in parsed if isinstance(item, dict)]
    if not any(_is_claude_event(item) or _is_diagnostic_obj(item) for item in events):
        return None
    for item in events:
        _take_claude_event(state, item)
    return state


def _state_payload(state: Dict[str, Any], text: str) -> Dict[str, Any]:
    return {
        "text": _strip_cli_diagnostics(text),
        "session_id": str(state.get("session_id") or ""),
        "tools": list(state.get("tools") or []),
        "is_error": bool(state.get("is_error")),
        "error": str(state.get("error") or ""),
    }


def _walk_claude_lines(text: str) -> Dict[str, Any]:
    state = _empty_take()
    plain: List[str] = []
    for line in (text or "").split("\n"):
        piece = line.strip()
        if not piece:
            plain.append("")
            continue
        if not piece.startswith("{") and not piece.startswith("["):
            plain.append(line)
            continue
        try:
            parsed = json.loads(piece)
        except json.JSONDecodeError:
            plain.append(line)
            continue
        if not isinstance(parsed, dict):
            plain.append(line)
            continue
        if _is_diagnostic_obj(parsed):
            continue
        if _is_claude_event(parsed):
            _take_claude_event(state, parsed)
            continue
        plain.append(line)
    prose = _strip_cli_diagnostics("\n".join(plain))
    extracted = _taken_text(state)
    if not prose:
        body = extracted
    elif not extracted or extracted in prose or prose in extracted:
        body = prose
    else:
        body = _strip_cli_diagnostics(prose + "\n" + extracted)
    return _state_payload(state, body)


def parse_claude_output(raw: str) -> Dict[str, Any]:
    """Pull the final text, session id, and question-tool names from CLI output.

    Accepts one JSON object (``--output-format json``, compact or pretty),
    stream-json lines, or plain text with ``[claude-code:…]`` stderr mixed in.
    """
    text = (raw or "").replace("\r\n", "\n").lstrip("\ufeff")
    for candidate in (text, _ANSI.sub("", text)):
        state = _try_claude_document(candidate)
        if state is not None:
            return _state_payload(state, _taken_text(state))
        if candidate != text:
            text = candidate
            break
    cleaned = _strip_cli_diagnostics(text)
    state = _try_claude_document(cleaned)
    if state is not None:
        return _state_payload(state, _taken_text(state))
    if not cleaned.strip():
        return _state_payload(_empty_take(), "")
    return _walk_claude_lines(cleaned)


def claude_reply_asks_question(parsed: Dict[str, Any]) -> bool:
    """True when the last Claude reply is waiting on a human."""
    from src.opencode_sessions import assistant_asked_question

    for name in parsed.get("tools") or []:
        key = str(name).strip().lower().replace("-", "_")
        if key in _QUESTION_TOOLS:
            return True
    return assistant_asked_question(str(parsed.get("text") or ""))


def _stderr_for_log(stderr: str) -> str:
    """Stderr the operator should see. CLI diagnostics are never included."""
    kept: List[str] = []
    for line in _strip_cli_diagnostics(stderr).split("\n"):
        piece = line.strip()
        if not piece:
            continue
        if piece.startswith("{") and "query_source" in piece:
            continue
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def claude_session_log_text(stdout: str, stderr: str = "", *, returncode: int = 0) -> str:
    """Reply text stored for the job transcript.

    Success keeps the assistant text only. A non-zero exit also keeps
    stderr that is not a ``[claude-code:…]`` diagnostic.
    """
    text = str(parse_claude_output(stdout or "").get("text") or "").strip()
    if returncode in (0, None):
        return text
    err = _stderr_for_log(stderr)
    if not err:
        return text
    return text + ("\n" + err if text else err)


def _cmd_for_log(argv: List[str]) -> str:
    if not argv:
        return ""
    parts = list(argv)
    last = parts[-1]
    if last and not last.startswith("-"):
        parts[-1] = f"<prompt {len(last)} chars>"
    return " ".join(parts)


class ClaudeBackend:
    """Drive ``claude --print`` in the job directory. One question nudge."""

    name = BACKEND_CLAUDE

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        cli = resolve_claude_cli()
        model = (request.model or getattr(settings, "default_model", "") or "").strip()
        agent = resolve_claude_agent_name(request.agent)
        handle = request.handle
        log_lines = request.log_lines if request.log_lines is not None else []
        timeout = float(request.timeout_seconds or 1800)
        session_id = (request.session_id or "").strip()
        if session_id and not is_claude_session_id(session_id):
            logger.warning(
                f"[claude] ignoring non-Claude session id {session_id}"
            )
            session_id = ""

        async def _once(prompt: str, *, resume_id: str) -> Dict[str, Any]:
            argv = _windows_spawn_argv(
                build_claude_argv(
                    cli=cli,
                    prompt=prompt,
                    model=model,
                    agent=agent,
                    resume_id=resume_id,
                )
            )
            logger.info(f"[claude] running: {_cmd_for_log(argv)}")
            if request.on_output:
                request.on_output("stdout", f"[claude] running: {_cmd_for_log(argv)}")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(request.working_directory) if request.working_directory else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=claude_child_env(),
            )
            handle["proc"] = proc
            handle["pid"] = proc.pid
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                self.cancel(handle)
                return {"timed_out": True, "text": "", "session_id": resume_id}
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            stderr = (stderr_b or b"").decode("utf-8", errors="replace")
            if stdout.strip():
                log_lines.append(stdout.strip())
                if request.on_output:
                    request.on_output("stdout", stdout.strip()[:4000])
            if stderr.strip() and request.on_output:
                request.on_output("stderr", stderr.strip()[:2000])
            parsed = parse_claude_output(stdout)
            if not parsed["session_id"]:
                parsed["session_id"] = resume_id
            parsed["returncode"] = proc.returncode
            parsed["stderr"] = stderr.strip()
            if parsed["session_id"] and request.on_session:
                try:
                    request.on_session(parsed["session_id"])
                except Exception:
                    pass
            return parsed

        if request.should_abort and request.should_abort():
            return AgentRunResult(
                returncode=-1,
                stderr="[claude] cancelled before start",
                backend=self.name,
            )
        try:
            first = await _once(request.prompt or "", resume_id=session_id)
        except FileNotFoundError:
            return AgentRunResult(
                returncode=127,
                stderr=(
                    "[claude] Claude Code CLI was not found. "
                    "Install it and set CLAUDE_CLI, or put claude on PATH."
                ),
                backend=self.name,
            )
        if first.get("timed_out"):
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[claude] timed out after {int(timeout)}s",
                session_id=first.get("session_id") or None,
                timed_out=True,
                backend=self.name,
            )
        sid = str(first.get("session_id") or "")
        if not claude_reply_asks_question(first):
            code = 0 if not first.get("is_error") and first.get("returncode") == 0 else (
                first.get("returncode") if first.get("returncode") not in (None, 0) else 1
            )
            if first.get("is_error"):
                code = code or 1
            return AgentRunResult(
                returncode=int(code or 0),
                stdout=str(first.get("text") or ""),
                stderr=str(first.get("stderr") or first.get("error") or ""),
                session_id=sid or None,
                backend=self.name,
            )

        logger.info("[claude] assistant asked a clarifying question — sending one unattended nudge")
        nudge = (
            DEFAULT_CLAUDE_PLAN_NUDGE_PROMPT
            if _is_plan_agent(agent)
            else DEFAULT_CLAUDE_NUDGE_PROMPT
        )
        if request.should_abort and request.should_abort():
            return AgentRunResult(
                returncode=-1,
                stdout=str(first.get("text") or ""),
                stderr="[claude] cancelled before nudge",
                session_id=sid or None,
                incomplete=True,
                incomplete_reasons=["assistant asked a clarifying question"],
                backend=self.name,
            )
        try:
            second = await _once(nudge, resume_id=sid)
        except FileNotFoundError:
            return AgentRunResult(
                returncode=127,
                stderr="[claude] Claude Code CLI was not found.",
                session_id=sid or None,
                backend=self.name,
            )
        if second.get("timed_out"):
            return AgentRunResult(
                returncode=-1,
                stdout=str(first.get("text") or ""),
                stderr=f"[claude] timed out after nudge ({int(timeout)}s)",
                session_id=sid or None,
                timed_out=True,
                incomplete=True,
                incomplete_reasons=["assistant asked a clarifying question"],
                backend=self.name,
            )
        if claude_reply_asks_question(second):
            return AgentRunResult(
                returncode=1,
                stdout=str(second.get("text") or first.get("text") or ""),
                stderr=(
                    "[claude] assistant asked a clarifying question "
                    "(unattended; no human reply path). After one nudge still asking."
                ),
                session_id=str(second.get("session_id") or sid) or None,
                incomplete=True,
                incomplete_reasons=["assistant asked a clarifying question"],
                backend=self.name,
                extra={"unattended_nudge": True},
            )
        code = 0 if not second.get("is_error") and second.get("returncode") == 0 else (
            second.get("returncode") if second.get("returncode") not in (None, 0) else 1
        )
        return AgentRunResult(
            returncode=int(code or 0),
            stdout=str(second.get("text") or ""),
            stderr=str(second.get("stderr") or ""),
            session_id=str(second.get("session_id") or sid) or None,
            backend=self.name,
            extra={"unattended_nudge": True},
        )

    def cancel(self, handle: Dict[str, Any]) -> None:
        handle["cancel"] = True
        proc = handle.get("proc")
        pid = getattr(proc, "pid", None) or handle.get("pid")
        if not pid:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
            )
            return
        try:
            proc.kill()
        except Exception:
            pass
