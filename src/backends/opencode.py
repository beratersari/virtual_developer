"""OpenCode serve adapter — existing unattended HTTP control loop."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from src.backends.base import BACKEND_OPENCODE, AgentRunRequest, AgentRunResult
from src.config import settings
from src.logger import logger


class OpenCodeBackend:
    """Drive ``opencode serve`` (compact wait, one nudge). Same contract as Codex."""

    name = BACKEND_OPENCODE

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        from src.opencode_serve import OpenCodeServeClient, ServeOrchestrator

        base = (
            getattr(settings, "opencode_serve_url", None) or "http://127.0.0.1:4096"
        )
        from src.orchestrator.agent_runner import resolve_opencode_agent_name

        work_dir = str(request.working_directory) if request.working_directory else None
        agent_name = resolve_opencode_agent_name(request.agent)
        model = request.model or getattr(settings, "default_model", "") or ""
        timeout_seconds = float(request.timeout_seconds or 1800)
        handle = request.handle
        log_lines = request.log_lines if request.log_lines is not None else []

        client = OpenCodeServeClient(
            base,
            timeout_seconds=timeout_seconds,
            directory=work_dir,
        )
        handle["mode"] = "serve"
        handle["backend"] = self.name
        handle["client"] = client
        handle["session_id"] = request.session_id
        handle["cancel"] = False

        def _on_out(stream: str, line: str) -> None:
            if request.on_output:
                request.on_output(stream, line)

        def _remember(sid: str) -> None:
            if not sid:
                return
            handle["session_id"] = sid
            if request.on_session:
                try:
                    request.on_session(sid)
                except Exception:
                    pass

        orch = ServeOrchestrator(
            client=client,
            compact_wait_seconds=timeout_seconds or 180.0,
            compact_poll_seconds=2.0,
        )
        turn = None
        try:
            outer_timeout = timeout_seconds + float(orch.compact_wait_seconds or 0)
            turn = await asyncio.wait_for(
                orch.run(
                    prompt=request.prompt or "",
                    title=request.title,
                    agent=agent_name,
                    model=model,
                    session_id=request.session_id,
                    abort_busy_session=bool(request.abort_busy_session),
                    on_output=_on_out,
                    on_session=_remember,
                    should_abort=lambda: bool(
                        handle.get("cancel")
                        or (request.should_abort and request.should_abort())
                    ),
                    log_lines=log_lines,
                ),
                timeout=outer_timeout,
            )
            if turn and turn.session_id:
                _remember(turn.session_id)
        except asyncio.TimeoutError:
            try:
                sid = handle.get("session_id")
                if sid:
                    await client.abort(sid)
            except Exception:
                pass
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr=f"[serve] timed out after {int(timeout_seconds)}s",
                session_id=handle.get("session_id"),
                timed_out=True,
                backend=self.name,
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

        if turn is None:
            return AgentRunResult(
                returncode=-1,
                stdout="\n".join(log_lines),
                stderr="[serve] no result",
                backend=self.name,
            )
        extra: Dict[str, Any] = {}
        for key in (
            "incomplete",
            "continue_count",
            "compact_events",
            "turns",
            "session_completeness",
        ):
            if hasattr(turn, key):
                extra[key] = getattr(turn, key)
        return AgentRunResult(
            returncode=int(turn.returncode),
            stdout=turn.stdout or "",
            stderr=turn.stderr or "",
            session_id=turn.session_id,
            incomplete=bool(getattr(turn, "incomplete", False)),
            incomplete_reasons=list(getattr(turn, "incomplete_reasons", None) or []),
            timed_out=bool(getattr(turn, "timed_out", False)),
            progress=int(getattr(turn, "progress", 0) or 0),
            backend=self.name,
            extra=extra,
        )

    def cancel(self, handle: Dict[str, Any]) -> None:
        handle["cancel"] = True
        sid = handle.get("session_id")
        client = handle.get("client")
        if client is None or not sid:
            return
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(client.abort(sid))
            else:
                loop.run_until_complete(client.abort(sid))
        except Exception as e:
            logger.debug(f"OpenCode abort failed: {e}")
