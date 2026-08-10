"""OpenCode HTTP *serve* client and compact-aware task orchestration.

Why this exists
---------------
``opencode run`` is a one-shot process: it can exit 0 after auto-compaction
without continuing the agent (upstream #13946). Virtual Developer then marked
jobs completed while sessions still had open todos.

``opencode serve`` exposes a long-lived API + event bus. The control loop here:

1. Create (or resume) a session
2. ``POST /session/{id}/message`` with the task prompt (sync wait for that turn)
3. Inspect messages + todos (and optional SSE signals)
4. If the turn looks like **compact-then-stop** or otherwise incomplete,
   send a **Continue** prompt on the *same* session (up to N times)
5. Only then report success to the daemon

TLS: all clients use ``verify=False`` (product requirement for on-prem/intercept).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
import httpx

from src.logger import logger
from src.opencode_sessions import assess_session_completeness


def is_serve_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.TimeoutException, TimeoutError))


def format_serve_error(
    exc: BaseException, *, timeout_seconds: Optional[float] = None
) -> str:
    """Human-readable serve HTTP error. ``str(httpx.ReadTimeout())`` is empty."""
    if is_serve_timeout(exc):
        limit = ""
        if timeout_seconds is not None:
            limit = f" ({float(timeout_seconds):.0f}s budget)"
        return (
            f"{type(exc).__name__}: POST /session/{{id}}/message exceeded "
            f"HTTP wait{limit}. OpenCode may still be running that turn."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        body = ""
        try:
            body = (resp.text or "").strip().replace("\n", " ")[:400]
        except Exception:
            body = ""
        extra = f" {body}" if body else ""
        return f"HTTP {resp.status_code} {resp.reason_phrase}:{extra}".rstrip(":")
    msg = str(exc).strip() or repr(exc)
    return f"{type(exc).__name__}: {msg}"

# Injected after compaction / premature idle so the agent loop resumes work.
DEFAULT_CONTINUE_PROMPT = (
    "Continue the previous OpenCode session. The last turn stopped early "
    "(context compaction, timeout, or error). Finish all remaining todos and "
    "complete the original task. Do not restart from scratch; resume "
    "implementation, verification, and commit steps as required."
)

# Long agent jobs compact many times. A default of 3 treated the 4th compact
# as a hard ERROR (returncode 2 → Jira "AI Agent — Error").
DEFAULT_MAX_COMPACT_CONTINUES = 256
# OpenCode /session/{id}/message?limit= used to be 80 — after ~15–20 compact
# cycles the sliding window dropped old markers and mis-counted "new this turn".
DEFAULT_MESSAGE_LIST_LIMIT = 500


@dataclass
class ServeTurnResult:
    """Outcome of one serve-mode orchestration (may include several continues)."""

    session_id: Optional[str]
    returncode: int
    stdout: str
    stderr: str
    incomplete: bool = False
    incomplete_reasons: List[str] = field(default_factory=list)
    compact_events: int = 0
    continue_count: int = 0
    turns: List[Dict[str, Any]] = field(default_factory=list)
    session_completeness: Optional[Dict[str, Any]] = None
    progress: int = 0
    timed_out: bool = False

    def to_agent_result(self, task_id: str, session_file: Optional[str] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "task_id": task_id,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "session_file": session_file,
            "opencode_session_id": self.session_id,
            "progress": self.progress if self.returncode != 0 else 100,
            "compact_events": self.compact_events,
            "continue_count": self.continue_count,
            "serve_turns": self.turns,
            "mode": "serve",
        }
        if self.incomplete:
            out["incomplete"] = True
            out["incomplete_reasons"] = list(self.incomplete_reasons)
        if self.timed_out:
            out["timed_out"] = True
        if self.session_completeness is not None:
            out["session_completeness"] = self.session_completeness
        return out


class OpenCodeServeClient:
    """Minimal async client for OpenCode v1 session HTTP API (1.18.x)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        *,
        timeout_seconds: float = 1800.0,
        client: Optional[httpx.AsyncClient] = None,
        directory: Optional[str] = None,
    ):
        self.base_url = (base_url or "http://127.0.0.1:4096").rstrip("/") + "/"
        self.timeout_seconds = float(timeout_seconds)
        self.directory = directory
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=30.0),
            verify=False,
            headers={"Accept": "application/json"},
        )

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.directory:
            # OpenCode uses this to scope project/workspace for the request
            h["x-opencode-directory"] = self.directory
        return h

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    async def health(self) -> Dict[str, Any]:
        r = await self._client.get("/global/health", headers=self._headers())
        r.raise_for_status()
        return r.json()

    async def create_session(
        self,
        title: str,
        *,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"title": title}
        if parent_id:
            body["parentID"] = parent_id
        r = await self._client.post(
            "/session", json=body, headers=self._headers()
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "id" in data:
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        raise RuntimeError(f"Unexpected create session response: {data!r}")

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        provider_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST /session/{id}/message — blocks until that prompt's loop ends."""
        body: Dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if agent:
            body["agent"] = agent
        if model and provider_id:
            body["model"] = {"providerID": provider_id, "modelID": model}
        elif model and "/" in model:
            prov, mid = model.split("/", 1)
            body["model"] = {"providerID": prov, "modelID": mid}
        r = await self._client.post(
            f"/session/{session_id}/message",
            json=body,
            headers=self._headers(),
            timeout=timeout or self.timeout_seconds,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    async def list_messages(
        self, session_id: str, *, limit: int = DEFAULT_MESSAGE_LIST_LIMIT
    ) -> List[Dict[str, Any]]:
        r = await self._client.get(
            f"/session/{session_id}/message",
            params={"limit": int(limit)},
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def list_all_messages(
        self,
        session_id: str,
        *,
        page_size: int = DEFAULT_MESSAGE_LIST_LIMIT,
        max_messages: int = 2000,
    ) -> List[Dict[str, Any]]:
        """Fetch as much session history as the API will give (newest-biased).

        OpenCode 1.18.x ``GET /session/{id}/message?limit=N`` returns the
        newest N rows. We request a large page so 20+ compact cycles still
        have stable marker ids for ``new_this_turn`` accounting.
        """
        cap = max(1, int(max_messages or 2000))
        size = max(1, min(int(page_size or DEFAULT_MESSAGE_LIST_LIMIT), cap))
        return await self.list_messages(session_id, limit=size)

    async def list_todos(self, session_id: str) -> List[Dict[str, Any]]:
        r = await self._client.get(
            f"/session/{session_id}/todo",
            headers=self._headers(),
        )
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def session_status(self) -> Dict[str, Any]:
        r = await self._client.get("/session/status", headers=self._headers())
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, dict) else {}

    async def abort(self, session_id: str) -> bool:
        try:
            r = await self._client.post(
                f"/session/{session_id}/abort",
                headers=self._headers(),
            )
            return r.status_code in (200, 204)
        except Exception as e:
            logger.debug(f"abort session {session_id} failed: {e}")
            return False

    async def summarize(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        auto: bool = True,
    ) -> Any:
        """Trigger compaction via v1 summarize (v2 compact often 503 on 1.18.x)."""
        r = await self._client.post(
            f"/session/{session_id}/summarize",
            json={
                "providerID": provider_id,
                "modelID": model_id,
                "auto": auto,
            },
            headers=self._headers(),
            timeout=self.timeout_seconds,
        )
        r.raise_for_status()
        if not r.content:
            return True
        try:
            return r.json()
        except Exception:
            return True


def messages_from_api(raw: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize list_messages() payloads into assess_session_completeness shape."""
    out: List[Dict[str, Any]] = []
    for m in raw:
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        if not isinstance(info, dict):
            continue
        # Pass parts through for compaction detection
        parts = m.get("parts") if isinstance(m.get("parts"), list) else []
        row = dict(info)
        row["_parts"] = parts
        out.append(row)
    return out


def _is_compaction_message(m: Dict[str, Any]) -> bool:
    parts = m.get("parts") or m.get("_parts") or []
    if any(isinstance(p, dict) and p.get("type") == "compaction" for p in parts):
        return True
    info = m.get("info") if isinstance(m.get("info"), dict) else m
    if isinstance(info, dict):
        summary = info.get("summary")
        if summary is True or (
            isinstance(summary, dict) and summary.get("compaction")
        ):
            return True
    return False


def compaction_marker_keys(messages: Sequence[Dict[str, Any]]) -> set[str]:
    """Stable ids for compaction markers (survives a sliding list window)."""
    keys: set[str] = set()
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or not _is_compaction_message(m):
            continue
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        mid = ""
        if isinstance(info, dict):
            mid = str(info.get("id") or "").strip()
        if not mid:
            mid = str(m.get("id") or "").strip()
        if not mid and isinstance(info, dict):
            time_obj = info.get("time") if isinstance(info.get("time"), dict) else {}
            raw_t = (
                time_obj.get("created")
                or time_obj.get("start")
                or info.get("time_created")
                or m.get("time_created")
            )
            if raw_t is not None:
                mid = f"t:{raw_t}"
        if not mid:
            # Last resort: content fingerprint (index alone shifts in a window)
            parts = m.get("parts") or m.get("_parts") or []
            mid = f"fp:{i}:{info.get('role') if isinstance(info, dict) else ''}:{len(parts)}"
        keys.add(mid)
    return keys


def count_compaction_signals(
    messages: Sequence[Dict[str, Any]],
) -> int:
    """Count compaction markers in API message list (parts or summary)."""
    return sum(1 for m in messages if isinstance(m, dict) and _is_compaction_message(m))


def assess_serve_turn(
    session_id: Optional[str],
    *,
    messages: Optional[Sequence[Dict[str, Any]]] = None,
    todos: Optional[Sequence[Dict[str, Any]]] = None,
    compact_events_seen: int = 0,
    new_compacts_this_turn: int = 0,
    output_text: str = "",
    db_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Completeness for serve mode using live API snapshots (+ optional DB)."""
    # Prefer live API todos/messages when provided
    api_todos = list(todos or [])
    api_msgs = list(messages or [])

    # Build synthetic assessment input for assess_session_completeness
    # when we have API data: map todos + last message into DB-like checks via
    # the shared function's messages path (extended below).
    result = assess_session_completeness(
        session_id,
        output_text=output_text,
        db_path=db_path,
        messages=messages_from_api(api_msgs) if api_msgs else None,
        todos=api_todos if api_todos else None,
    )

    # Do **not** force incomplete just because a compact happened this turn.
    # Compact-then-stop (last assistant immediately after a compaction part, or
    # last assistant is a compaction summary) is already flagged by
    # assess_session_completeness. Forcing "any compact ⇒ not success" treated
    # a finished turn that compacted mid-loop as an ERROR.
    result["compact_events_seen"] = compact_events_seen
    result["new_compacts_this_turn"] = int(new_compacts_this_turn or 0)
    return result


@dataclass
class ServeOrchestrator:
    """Run one agent task against OpenCode serve with multi-compact continue."""

    client: OpenCodeServeClient
    max_compact_continues: int = DEFAULT_MAX_COMPACT_CONTINUES
    continue_prompt: str = DEFAULT_CONTINUE_PROMPT
    # Optional: force summarize after each turn (tests / stress). Production: False.
    force_summarize_after_turn: bool = False
    force_summarize_provider: str = "opencode"
    force_summarize_model: str = "deepseek-v4-flash-free"

    async def run(
        self,
        *,
        prompt: str,
        title: str,
        agent: Optional[str] = None,
        model: Optional[str] = None,
        session_id: Optional[str] = None,
        on_output: Optional[Callable[[str, str], None]] = None,
        on_session: Optional[Callable[[str], None]] = None,
        should_abort: Optional[Callable[[], bool]] = None,
        log_lines: Optional[List[str]] = None,
    ) -> ServeTurnResult:
        lines = log_lines if log_lines is not None else []
        turns: List[Dict[str, Any]] = []
        compact_total = 0
        continue_count = 0

        def _emit(stream: str, text: str) -> None:
            lines.append(text)
            if on_output:
                try:
                    on_output(stream, text)
                except Exception:
                    pass

        def _aborted() -> bool:
            try:
                return bool(should_abort and should_abort())
            except Exception:
                return False

        # Health
        try:
            health = await self.client.health()
            _emit("stdout", f"[serve] health={health}")
        except Exception as e:
            return ServeTurnResult(
                session_id=session_id,
                returncode=1,
                stdout="\n".join(lines),
                stderr=f"[serve] health check failed: {e}",
                incomplete=True,
                incomplete_reasons=[f"serve unreachable: {e}"],
            )

        # Session — create once, then every Continue POST uses the same id
        sid = session_id
        if not sid:
            try:
                sess = await self.client.create_session(title=title or "VD agent task")
                sid = sess.get("id")
                _emit("stdout", f"[serve] session created: {sid}")
            except Exception as e:
                return ServeTurnResult(
                    session_id=None,
                    returncode=1,
                    stdout="\n".join(lines),
                    stderr=f"[serve] create session failed: {e}",
                    incomplete=True,
                    incomplete_reasons=[f"create session: {e}"],
                )
        else:
            _emit("stdout", f"[serve] session resumed: {sid}")
        if not sid:
            return ServeTurnResult(
                session_id=None,
                returncode=1,
                stdout="\n".join(lines),
                stderr="[serve] no session id",
                incomplete=True,
                incomplete_reasons=["no session id"],
            )
        if on_session:
            try:
                on_session(str(sid))
            except Exception:
                pass

        current_prompt = prompt
        # Initial prompt + up to max_compact_continues continues
        max_turns = 1 + max(0, int(self.max_compact_continues))

        for turn_idx in range(max_turns):
            if _aborted():
                await self.client.abort(sid)
                return ServeTurnResult(
                    session_id=sid,
                    returncode=-1,
                    stdout="\n".join(lines),
                    stderr="Aborted",
                    incomplete=True,
                    incomplete_reasons=["aborted"],
                    compact_events=compact_total,
                    continue_count=continue_count,
                    turns=turns,
                )

            label = "initial" if turn_idx == 0 else f"continue#{turn_idx}"
            # Markers already in the session (resume / prior turn) must not count
            # as "new this turn" or every retry after compact looks premature.
            try:
                messages_before = await self.client.list_all_messages(sid)
            except Exception:
                messages_before = []
            keys_before = compaction_marker_keys(messages_before)

            _emit("stdout", f"[serve] turn={label} sending message…")
            t0 = time.time()
            try:
                msg = await self.client.send_message(
                    sid,
                    current_prompt,
                    agent=agent,
                    model=model,
                )
            except Exception as e:
                err = format_serve_error(
                    e, timeout_seconds=getattr(self.client, "timeout_seconds", None)
                )
                timed_out = is_serve_timeout(e)
                note = f"[serve] message failed: {err}"
                if timed_out:
                    # Dropping the HTTP wait does not stop OpenCode. Abort so a
                    # Continue retry is not posted into a still-running turn.
                    try:
                        await self.client.abort(sid)
                        note = note + "\n[serve] aborted session after HTTP timeout"
                    except Exception as abort_exc:
                        note = (
                            note
                            + f"\n[serve] abort after timeout failed: {abort_exc}"
                        )
                return ServeTurnResult(
                    session_id=sid,
                    returncode=-1 if timed_out else 1,
                    stdout="\n".join(lines),
                    stderr=note,
                    incomplete=True,
                    incomplete_reasons=[
                        "http timeout" if timed_out else f"message error: {err}"
                    ],
                    compact_events=compact_total,
                    continue_count=continue_count,
                    turns=turns,
                    timed_out=timed_out,
                )
            elapsed = time.time() - t0
            info = msg.get("info") if isinstance(msg.get("info"), dict) else msg
            finish = info.get("finish") if isinstance(info, dict) else None
            summary = info.get("summary") if isinstance(info, dict) else None
            text_parts = []
            for p in msg.get("parts") or []:
                if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
                    text_parts.append(str(p["text"]))
            reply_text = "\n".join(text_parts)
            if reply_text:
                _emit("stdout", reply_text[:4000])
            _emit(
                "stdout",
                f"[serve] turn={label} done finish={finish!r} summary={summary!r} "
                f"elapsed={elapsed:.2f}s",
            )

            # Optional forced compaction (e2e / stress): simulate context pressure
            forced_compact = False
            if self.force_summarize_after_turn:
                try:
                    _emit("stdout", "[serve] force summarize (compact)…")
                    await self.client.summarize(
                        sid,
                        provider_id=self.force_summarize_provider,
                        model_id=self.force_summarize_model,
                        auto=True,
                    )
                    forced_compact = True
                    _emit("stdout", "[serve] summarize returned; re-fetching messages")
                except Exception as e:
                    _emit("stderr", f"[serve] force summarize failed: {e}")

            # Snapshot state (full-ish history — do not use a 80-row window)
            try:
                messages = await self.client.list_all_messages(sid)
            except Exception as e:
                messages = []
                _emit("stderr", f"[serve] list messages failed: {e}")
            try:
                todos = await self.client.list_todos(sid)
            except Exception as e:
                todos = []
                _emit("stderr", f"[serve] list todos failed: {e}")

            keys_after = compaction_marker_keys(messages)
            compact_in_msgs = len(keys_after)
            new_this_turn = len(keys_after - keys_before)
            if forced_compact:
                new_this_turn = max(new_this_turn, 1)
            compact_total = max(
                compact_total + (1 if forced_compact else 0), compact_in_msgs
            )

            assessment = assess_serve_turn(
                sid,
                messages=messages,
                todos=todos,
                compact_events_seen=compact_total,
                new_compacts_this_turn=new_this_turn,
                # This turn's assistant text only — the accumulated serve log
                # still contains earlier "Compacting…" lines and would false-
                # flag every later continue as incomplete.
                output_text=reply_text,
            )
            turn_rec = {
                "turn": label,
                "finish": finish,
                "summary": summary,
                "elapsed_s": round(elapsed, 3),
                "assessment": {
                    "complete": assessment.get("complete"),
                    "premature": assessment.get("premature"),
                    "reasons": assessment.get("reasons"),
                    "open_todos": assessment.get("open_todos"),
                },
                "compact_markers": compact_in_msgs,
            }
            turns.append(turn_rec)
            _emit(
                "stdout",
                f"[serve] assessment complete={assessment.get('complete')} "
                f"premature={assessment.get('premature')} "
                f"reasons={assessment.get('reasons')}",
            )

            if not assessment.get("premature"):
                # Real completion of the agent loop for this task
                return ServeTurnResult(
                    session_id=sid,
                    returncode=0,
                    stdout="\n".join(lines),
                    stderr="",
                    incomplete=False,
                    compact_events=compact_total,
                    continue_count=continue_count,
                    turns=turns,
                    session_completeness=assessment,
                    progress=100,
                )

            # Incomplete — try continue if budget remains
            remaining = max_turns - turn_idx - 1
            if remaining <= 0:
                reasons = assessment.get("reasons") or ["incomplete after max continues"]
                note = (
                    f"[INCOMPLETE] serve session still incomplete after "
                    f"{continue_count} continue(s): {'; '.join(map(str, reasons))}"
                )
                _emit("stderr", note)
                return ServeTurnResult(
                    session_id=sid,
                    returncode=2,
                    stdout="\n".join(lines),
                    stderr=note,
                    incomplete=True,
                    incomplete_reasons=list(reasons),
                    compact_events=compact_total,
                    continue_count=continue_count,
                    turns=turns,
                    session_completeness=assessment,
                    progress=50,
                )

            continue_count += 1
            _emit(
                "stdout",
                f"[serve] incomplete (likely compact/mid-work); "
                f"continue {continue_count}/{self.max_compact_continues}",
            )
            current_prompt = self.continue_prompt

        # Should not reach
        return ServeTurnResult(
            session_id=sid,
            returncode=2,
            stdout="\n".join(lines),
            stderr="[INCOMPLETE] exhausted serve turns",
            incomplete=True,
            incomplete_reasons=["exhausted turns"],
            compact_events=compact_total,
            continue_count=continue_count,
            turns=turns,
        )
