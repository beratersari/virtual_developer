"""Claude Code CLI adapter. Unattended ``claude --print`` in the job clone."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
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


def prepare_claude_prompt(prompt: str) -> str:
    """Standing unattended rules in front of the job text."""
    text = (prompt or "").strip()
    if "UNATTENDED JOB:" in text or "no human in this" in text.lower():
        return text
    return (
        "UNATTENDED JOB: do not ask clarifying questions, confirmations, "
        "or wait for a human. Choose defaults and finish the work. "
        "Do not git push or open a merge request.\n\n"
        + text
    )


def build_claude_argv(
    *,
    cli: str,
    prompt: str = "",
    model: str = "",
    agent: str = "",
    resume_id: str = "",
) -> List[str]:
    """``claude --print`` argv. The prompt is written to stdin, not argv."""
    del prompt
    exe = (cli or "claude").strip() or "claude"
    cmd = [
        exe,
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
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
    return cmd


# No assistant, tool, or result line for this long means the stream is stuck.
SILENCE_SECONDS = 180.0


def claude_user_line(text: str) -> bytes:
    """One stdin turn for ``--input-format stream-json``."""
    payload = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


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
        # Tools belong to the open turn. A later finish must not inherit
        # AskUserQuestion from the turn that was nudged.
        state["pending_tools"].extend(_tool_names(content))
        state["tools"] = list(state["pending_tools"])
        return
    if kind == "result" or "result" in event or event.get("is_error") is not None:
        state["tools"] = list(state.get("pending_tools") or [])
        state["pending_tools"] = []
        state["is_error"] = bool(event.get("is_error"))
        result = _as_text(event.get("result")).strip()
        if result:
            state["result"] = result
        err = _as_text(event.get("error")).strip()
        if err:
            state["error"] = err
        cost = event.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            state["total_cost_usd"] = float(cost)


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
        "pending_tools": [],
        "saw": False,
        "total_cost_usd": None,
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
        "total_cost_usd": state.get("total_cost_usd"),
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
    # A result event, or the last assistant text, is the final message.
    # The launch line and a cut-off tool-result object are not.
    body = extracted or prose
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

        async def _drive(prompt: str, *, resume_id: str) -> Dict[str, Any]:
            argv = _windows_spawn_argv(
                build_claude_argv(
                    cli=cli,
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
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=claude_child_env(),
            )
            handle["proc"] = proc
            handle["pid"] = proc.pid
            stdout_parts: List[str] = []
            stderr_parts: List[str] = []
            seen = {"session_id": resume_id, "published": False}
            last_event = time.monotonic()
            deadline = last_event + timeout
            nudged = {"done": False}

            def _publish(sid: str) -> None:
                if not sid or seen["published"]:
                    return
                seen["session_id"] = sid
                seen["published"] = True
                if request.on_session:
                    try:
                        request.on_session(sid)
                    except Exception:
                        pass

            def _emit(kind: str, raw: bytes) -> None:
                nonlocal last_event
                text = raw.decode("utf-8", errors="replace").rstrip("\r")
                if not text.strip():
                    return
                if kind == "stdout":
                    stdout_parts.append(text)
                    log_lines.append(text)
                    piece = text.strip()
                    if piece.startswith("{"):
                        try:
                            event = json.loads(piece)
                        except json.JSONDecodeError:
                            event = None
                        if isinstance(event, dict) and _is_claude_event(event):
                            last_event = time.monotonic()
                            sid = str(event.get("session_id") or "").strip()
                            if sid:
                                _publish(sid)
                else:
                    stderr_parts.append(text)
                if request.on_output:
                    request.on_output(kind, text)

            async def _pump_stderr() -> None:
                stream = proc.stderr
                if stream is None:
                    return
                pending = b""
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        if pending.strip():
                            _emit("stderr", pending)
                        return
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        _emit("stderr", line)

            stderr_task = asyncio.create_task(_pump_stderr())

            async def _send(text: str) -> None:
                if proc.stdin is None:
                    return
                proc.stdin.write(claude_user_line(text))
                await proc.stdin.drain()

            async def _read_until_result() -> str:
                """Return result, stall, timeout, or eof."""
                stream = proc.stdout
                if stream is None:
                    return "eof"
                pending = b""
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        return "timeout"
                    wait = min(SILENCE_SECONDS, deadline - now)
                    try:
                        chunk = await asyncio.wait_for(stream.read(65536), timeout=wait)
                    except asyncio.TimeoutError:
                        if time.monotonic() >= deadline:
                            return "timeout"
                        return "stall"
                    if not chunk:
                        if pending.strip():
                            _emit("stdout", pending)
                        return "eof"
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        _emit("stdout", line)
                        piece = line.decode("utf-8", errors="replace").strip()
                        if not piece.startswith("{"):
                            continue
                        try:
                            event = json.loads(piece)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict) and event.get("type") == "result":
                            return "result"

            def _parsed() -> Dict[str, Any]:
                parsed = parse_claude_output("\n".join(stdout_parts))
                if not parsed.get("session_id"):
                    parsed["session_id"] = seen["session_id"]
                parsed["stderr"] = "\n".join(stderr_parts).strip()
                return parsed

            try:
                await _send(prepare_claude_prompt(prompt))
                status = await _read_until_result()
                if status in {"stall", "timeout"}:
                    self.cancel(handle)
                    parsed = _parsed()
                    parsed["timed_out"] = True
                    parsed["stall"] = status == "stall"
                    return parsed
                first = _parsed()
                if (
                    claude_reply_asks_question(first)
                    and not nudged["done"]
                    and not (request.should_abort and request.should_abort())
                ):
                    nudged["done"] = True
                    logger.info(
                        "[claude] assistant asked a clarifying question "
                        "— sending one unattended nudge"
                    )
                    nudge = (
                        DEFAULT_CLAUDE_PLAN_NUDGE_PROMPT
                        if _is_plan_agent(agent)
                        else DEFAULT_CLAUDE_NUDGE_PROMPT
                    )
                    await _send(nudge)
                    status = await _read_until_result()
                    if status in {"stall", "timeout"}:
                        self.cancel(handle)
                        parsed = _parsed()
                        parsed["timed_out"] = True
                        parsed["stall"] = status == "stall"
                        parsed["nudged"] = True
                        return parsed
                if proc.stdin is not None:
                    proc.stdin.close()
                remain = max(1.0, deadline - time.monotonic())
                try:
                    await asyncio.wait_for(proc.wait(), timeout=remain)
                except asyncio.TimeoutError:
                    self.cancel(handle)
                    parsed = _parsed()
                    parsed["timed_out"] = True
                    return parsed
                parsed = _parsed()
                parsed["returncode"] = proc.returncode
                parsed["nudged"] = nudged["done"]
                return parsed
            finally:
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass

        if request.should_abort and request.should_abort():
            return AgentRunResult(
                returncode=-1,
                stderr="[claude] cancelled before start",
                backend=self.name,
            )
        try:
            outcome = await _drive(request.prompt or "", resume_id=session_id)
        except FileNotFoundError:
            return AgentRunResult(
                returncode=127,
                stderr=(
                    "[claude] Claude Code CLI was not found. "
                    "Install it and set CLAUDE_CLI, or put claude on PATH."
                ),
                backend=self.name,
            )
        sid = str(outcome.get("session_id") or "") or None
        cost = outcome.get("total_cost_usd")
        extra: Dict[str, Any] = {}
        if isinstance(cost, float):
            extra["total_cost_usd"] = cost
        if outcome.get("nudged"):
            extra["unattended_nudge"] = True
        if outcome.get("timed_out"):
            why = (
                f"[claude] no stream output for {int(SILENCE_SECONDS)}s"
                if outcome.get("stall")
                else f"[claude] timed out after {int(timeout)}s"
            )
            return AgentRunResult(
                returncode=-1,
                stdout=str(outcome.get("text") or ""),
                stderr=why,
                session_id=sid,
                timed_out=True,
                incomplete=bool(outcome.get("nudged")),
                incomplete_reasons=(
                    ["assistant asked a clarifying question"]
                    if outcome.get("nudged")
                    else []
                ),
                backend=self.name,
                extra=extra,
            )
        if claude_reply_asks_question(outcome):
            return AgentRunResult(
                returncode=1,
                stdout=str(outcome.get("text") or ""),
                stderr=(
                    "[claude] assistant asked a clarifying question "
                    "(unattended; no human reply path). After one nudge still asking."
                ),
                session_id=sid,
                incomplete=True,
                incomplete_reasons=["assistant asked a clarifying question"],
                backend=self.name,
                extra=extra,
            )
        code = 0 if not outcome.get("is_error") and outcome.get("returncode") == 0 else (
            outcome.get("returncode") if outcome.get("returncode") not in (None, 0) else 1
        )
        if outcome.get("is_error"):
            code = code or 1
        return AgentRunResult(
            returncode=int(code or 0),
            stdout=str(outcome.get("text") or ""),
            stderr=str(outcome.get("stderr") or outcome.get("error") or ""),
            session_id=sid,
            backend=self.name,
            extra=extra,
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
