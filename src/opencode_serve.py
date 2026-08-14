"""OpenCode HTTP *serve* client and compact-aware task orchestration.

Why this exists
---------------
Agent jobs use ``opencode serve``. The control loop here:

1. Create (or resume) a session
2. ``POST /session/{id}/message`` with the **task prompt only** (one-pass)
3. If the turn looks like compact-then-stop (or the HTTP wait ended while
   OpenCode is still compacting), **wait** for auto-compact / auto-resume
4. Re-assess. Never inject a user "Continue" for compaction — that shows
   up in chat as the operator and races OpenCode's own compact loop
5. If the model asks a **clarifying question**, leave compact-wait immediately
   and send **one** unattended nudge (not the full BUILD kit). Auto-resume
   never answers a human; spinning on "waiting for auto-resume" with
   reasons containing "clarifying question" is a bug (see AGENTS.md §2).
6. After the nudge, only the **last** assistant turn decides "still asking";
   stale open todos alone after a clean finish=stop may be accepted
   (todo API lag). Timeout/error resume may still send Continue; compact never does.

TLS: INTENTIONAL — all clients use ``verify=False`` (on-prem / intercept;
do not enable verification until a custom-CA path exists).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence
import httpx

from src.logger import logger
from src.opencode_sessions import (
    assess_session_completeness,
    reasons_are_compact_only,
    strip_compact_reasons,
)


def coerce_json_list(data: Any) -> List[Dict[str, Any]]:
    """Normalize serve list payloads (raw array or ``{data|messages|todos: [...]}``)."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "messages", "items", "todos"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


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

# Used only for timeout/error resume — never injected because of auto-compact.
DEFAULT_CONTINUE_PROMPT = (
    "Continue the previous OpenCode session. The last turn stopped early "
    "(timeout or error). Finish all remaining todos and complete the original "
    "task. Do not restart from scratch; resume implementation, verification, "
    "and commit steps as required."
)

# Incomplete (open todos) resume — short nudge, not the original BUILD kit.
DEFAULT_FINISH_TODOS_PROMPT = (
    "Finish remaining todos and complete the original task in this session. "
    "Do not restart from scratch."
)

# When the model stops to ask the operator (daemon is unattended / one-pass).
# One corrective user turn only — never re-send the full BUILD/PLAN kit.
DEFAULT_UNATTENDED_NUDGE_PROMPT = (
    "You are running unattended inside a daemon — there is no human in the "
    "loop and no one will answer questions. Do not ask clarifying questions, "
    "confirmation, or multiple-choice options. Choose the safest defaults "
    "consistent with AGENTS.md, the repository, and the original issue "
    "description. Finish all remaining work (implementation, verification, "
    "and local commit steps as required) without waiting. Do **not** git push "
    "or open a merge request — the orchestrator delivers the branch after "
    "you stop. Mark todos completed when the work is done."
)

# How long to wait for OpenCode auto-compact / auto-resume before re-assessing.
# Do not POST a user "Continue" while compact is running — that pollutes chat
# and fights the built-in compact loop.
DEFAULT_COMPACT_WAIT_SECONDS = 180.0
DEFAULT_COMPACT_POLL_SECONDS = 2.0
# After the session goes idle, wait this long for auto-resume to start.
# Compact-then-stop often idles briefly before OpenCode replays the last turn.
DEFAULT_COMPACT_SETTLE_SECONDS = 2.0

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
        if self.incomplete_reasons:
            out["incomplete_reasons"] = list(self.incomplete_reasons)
        if self.timed_out:
            out["timed_out"] = True
        if int(self.compact_events or 0) > 0:
            out["had_compact"] = True
        if self.session_completeness is not None:
            out["session_completeness"] = self.session_completeness
            if self.session_completeness.get("assistant_asked_question"):
                out["assistant_asked_question"] = True
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
        # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
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

    def ensure_directory_ready(self) -> None:
        """Fail before POST when x-opencode-directory is set but missing on disk.

        OpenCode 1.18 returns HTTP 500 UnknownError for ENOENT workspaces.
        Catch that here with a clear error instead of a generic serve failure.
        """
        raw = (self.directory or "").strip()
        if not raw:
            return
        from pathlib import Path

        path = Path(raw)
        if not path.is_dir():
            raise FileNotFoundError(
                f"OpenCode work directory does not exist: {raw}. "
                "The temp clone may have been cleaned up or never created. "
                "Re-queue the issue after git prep succeeds."
            )

    async def list_agents(self) -> List[Dict[str, Any]]:
        """GET /agent — registered agent names (oh-my + built-ins)."""
        r = await self._client.get("/agent", headers=self._headers())
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return coerce_json_list(r.json())

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
        self.ensure_directory_ready()
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
        return coerce_json_list(r.json())

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
        return coerce_json_list(r.json())

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


def session_is_busy(status: Any, session_id: Optional[str]) -> bool:
    """True when OpenCode reports this session still running or compacting.

    Status payloads vary by OpenCode version. Accept common shapes:
    ``{ses_…: {type: busy}}``, nested ``data``, top-level ``type``, list
    of session rows, and ``state`` / ``status`` string fields.
    """
    if not isinstance(status, dict) or not session_id:
        return False

    def _row_busy(row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        kind = str(
            row.get("type")
            or row.get("status")
            or row.get("state")
            or row.get("phase")
            or ""
        ).strip().lower()
        if kind in {
            "busy",
            "busy_compacting",
            "compacting",
            "retry",
            "running",
            "in_progress",
            "in-progress",
            "active",
            "working",
            "processing",
            "message",
        }:
            return True
        if row.get("busy") is True or row.get("running") is True:
            return True
        if "compact" in kind or "run" in kind:
            return True
        return False

    row = status.get(session_id)
    if row is None and isinstance(status.get("data"), dict):
        data = status["data"]
        row = data.get(session_id)
        if row is None and _row_busy(data):
            return True
        if isinstance(data.get("sessions"), dict):
            row = data["sessions"].get(session_id)
    if row is None and isinstance(status.get("sessions"), dict):
        row = status["sessions"].get(session_id)
    if _row_busy(row):
        return True
    # Sometimes {type: busy} at top level for the only active session
    if _row_busy(status):
        return True
    # List form: [{id|sessionID: ses_…, type: busy}, …]
    for key in ("data", "sessions", "items", "status"):
        lst = status.get(key)
        if not isinstance(lst, list):
            continue
        for item in lst:
            if not isinstance(item, dict):
                continue
            sid = str(
                item.get("id")
                or item.get("sessionID")
                or item.get("sessionId")
                or item.get("session_id")
                or ""
            )
            if sid == session_id and _row_busy(item):
                return True
    return False


def is_message_conflict_error(exc: BaseException) -> bool:
    """True when the response body/status clearly means a turn is already running.

    Do **not** treat every HTTP 500 as a conflict. Live OpenCode 1.18 also
    returns generic ``UnknownError`` 500 for missing workdir, bad agent name,
    or unknown model — those must fail fast, not wait forever for messages.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    resp = exc.response
    code = int(getattr(resp, "status_code", 0) or 0)
    body = ""
    try:
        body = (resp.text or "").lower()
    except Exception:
        body = ""
    if code in (409, 423, 429):
        return True
    return any(
        token in body
        for token in (
            "already running",
            "session busy",
            "active turn",
            "concurrent",
            "another message",
            "turn in progress",
        )
    )


def explain_message_http_error(
    exc: BaseException,
    *,
    directory: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Actionable hint for serve POST /message failures (ref + common causes)."""
    base = format_serve_error(exc)
    hints: List[str] = []
    body = ""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = (exc.response.text or "")[:800]
        except Exception:
            body = ""
    low = body.lower()
    dir_path = (directory or "").strip()
    if dir_path:
        try:
            from pathlib import Path

            if not Path(dir_path).is_dir():
                hints.append(
                    f"work directory does not exist: {dir_path} "
                    "(temp clone missing/cleaned — OpenCode returns UnknownError 500)"
                )
        except Exception:
            pass
    if "model not found" in low or "providermodelnotfound" in low:
        hints.append(
            f"model not found on serve: {model or '(default)'}. "
            "Set DEFAULT_MODEL / dashboard model to a provider/model OpenCode has."
        )
    if "enoent" in low or "notfound" in low or "realpath" in low:
        hints.append(
            "OpenCode could not resolve the workspace path "
            "(x-opencode-directory). Check temp clone still exists."
        )
    if agent and not hints:
        hints.append(
            f"If agent {agent!r} is not registered on this serve "
            "(oh-my-openagent missing), OpenCode returns UnknownError 500. "
            "Check GET /agent and plugin install."
        )
    if not hints and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        hints.append(
            "Common causes of serve 500 on a *new* session: missing workdir, "
            "unknown agent name, or unknown model. Check opencode serve logs "
            "for the error ref above."
        )
    if hints:
        return base + " | " + " ".join(hints)
    return base


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
    """Run one agent task against OpenCode serve; wait out auto-compact."""

    client: OpenCodeServeClient
    max_compact_continues: int = DEFAULT_MAX_COMPACT_CONTINUES
    continue_prompt: str = DEFAULT_CONTINUE_PROMPT
    compact_wait_seconds: float = DEFAULT_COMPACT_WAIT_SECONDS
    compact_poll_seconds: float = DEFAULT_COMPACT_POLL_SECONDS
    compact_settle_seconds: float = DEFAULT_COMPACT_SETTLE_SECONDS
    # Optional: force summarize after each turn (tests / stress). Production: False.
    force_summarize_after_turn: bool = False
    force_summarize_provider: str = "opencode"
    force_summarize_model: str = "deepseek-v4-flash-free"

    def _compact_wait_budget(self) -> float:
        explicit = float(self.compact_wait_seconds or 0)
        if explicit > 0:
            return max(0.2, explicit)
        client_budget = float(getattr(self.client, "timeout_seconds", 0) or 0)
        return max(1.0, client_budget or DEFAULT_COMPACT_WAIT_SECONDS)

    async def _wait_for_auto_compact(
        self,
        sid: str,
        *,
        _emit: Callable[[str, str], None],
        _aborted: Callable[[], bool],
        compact_total: int = 0,
    ) -> Dict[str, Any]:
        """Poll until OpenCode finishes auto-compact / auto-resume.

        Compact-then-stop often returns the HTTP turn while the session is
        briefly idle; OpenCode then auto-resumes. A short idle is **not**
        done: we re-assess and keep waiting (no user POST) until the session
        is actually complete, auto-resume goes busy again, or the budget ends.
        """
        wait_s = self._compact_wait_budget()
        poll_s = max(0.05, float(self.compact_poll_seconds or DEFAULT_COMPACT_POLL_SECONDS))
        settle_s = min(
            max(0.05, float(self.compact_settle_seconds or DEFAULT_COMPACT_SETTLE_SECONDS)),
            max(0.05, wait_s / 3.0),
        )
        deadline = time.time() + wait_s
        polls = 0
        last_busy = False
        idle_since: Optional[float] = None
        last_reasons: List[Any] = []
        while time.time() < deadline:
            if _aborted():
                return {"aborted": True, "polls": polls}
            status: Dict[str, Any] = {}
            poll_failed = False
            try:
                status = await self.client.session_status()
            except Exception as e:
                poll_failed = True
                _emit("stderr", f"[serve] status poll failed: {e}")
            # A failed poll must not look like "compact finished".
            busy = True if poll_failed else session_is_busy(status, sid)
            last_busy = busy
            polls += 1
            now = time.time()
            if busy:
                idle_since = None
                _emit("stdout", f"[serve] waiting for auto-compact (poll {polls}, busy)")
                await asyncio.sleep(poll_s)
                continue
            if idle_since is None:
                idle_since = now
                _emit(
                    "stdout",
                    f"[serve] session idle — settling {settle_s:.1f}s for auto-resume",
                )
            if (now - idle_since) < settle_s:
                await asyncio.sleep(min(poll_s, settle_s))
                continue
            # Idle long enough to snapshot. Do **not** treat this as terminal:
            # auto-resume often starts a few seconds after compact-then-stop.
            _messages, assessment, compact_total = await self._snapshot_assess(
                sid, compact_total=compact_total, output_text="", _emit=_emit
            )
            strip_compact_reasons(assessment)
            last_reasons = list(assessment.get("reasons") or [])
            if not assessment.get("premature"):
                _emit(
                    "stdout",
                    f"[serve] auto-compact settled after {polls} poll(s) "
                    f"(session complete)",
                )
                return {
                    "settled": True,
                    "complete": True,
                    "polls": polls,
                    "busy": False,
                    "assessment": assessment,
                    "compact_total": compact_total,
                }
            # Clarifying question is a clean idle stop — not mid-compact.
            # Production symptom without this exit: poll 700–900+ of
            # "still incomplete … assistant asked a clarifying question —
            # waiting for auto-resume" while no human reply path exists.
            # Match flag *or* reason text (belt-and-suspenders).
            asked = bool(assessment.get("assistant_asked_question")) or any(
                "clarifying question" in str(r).lower() for r in last_reasons
            )
            if asked:
                assessment["assistant_asked_question"] = True
                if not any(
                    "clarifying question" in str(r).lower() for r in last_reasons
                ):
                    last_reasons = list(last_reasons) + [
                        "assistant asked a clarifying question"
                    ]
                    assessment["reasons"] = last_reasons
                _emit(
                    "stdout",
                    f"[serve] idle after compact: assistant asked a clarifying "
                    f"question (poll {polls}) — leaving compact wait for "
                    f"unattended nudge (reasons={last_reasons})",
                )
                return {
                    "settled": True,
                    "complete": False,
                    "polls": polls,
                    "busy": False,
                    "assessment": assessment,
                    "compact_total": compact_total,
                    "reasons": last_reasons,
                }
            if polls == 1 or polls % 5 == 0:
                _emit(
                    "stdout",
                    f"[serve] idle after compact but still incomplete "
                    f"(poll {polls}, reasons={last_reasons}) — "
                    "waiting for auto-resume (no user message)",
                )
            await asyncio.sleep(poll_s)
        _emit(
            "stderr",
            f"[serve] auto-compact still unfinished after {wait_s:.0f}s "
            f"({polls} polls, reasons={last_reasons})",
        )
        return {
            "timeout": True,
            "polls": polls,
            "busy": last_busy,
            "reasons": last_reasons,
            "compact_total": compact_total,
        }

    async def _fetch_session_lists(
        self,
        sid: str,
        *,
        _emit: Callable[[str, str], None],
    ) -> tuple[Optional[List[Dict[str, Any]]], Optional[List[Dict[str, Any]]], bool]:
        """Load messages/todos. Empty or failed message lists are None (DB fallback)."""
        messages: Optional[List[Dict[str, Any]]] = None
        todos: Optional[List[Dict[str, Any]]] = None
        messages_failed = False
        try:
            fetched = await self.client.list_all_messages(sid)
            messages = fetched if fetched else None
        except Exception as e:
            messages_failed = True
            _emit("stderr", f"[serve] list messages failed: {e}")
        try:
            todos = await self.client.list_todos(sid)
        except Exception as e:
            todos = None
            _emit("stderr", f"[serve] list todos failed: {e}")
        return messages, todos, messages_failed

    @staticmethod
    def _fail_closed_if_no_evidence(
        assessment: Dict[str, Any],
        *,
        messages: Optional[List[Dict[str, Any]]],
        messages_failed: bool,
    ) -> Dict[str, Any]:
        """Do not treat a missing snapshot as COMPLETED."""
        if messages or assessment.get("last_role") or assessment.get("open_todos"):
            return assessment
        if assessment.get("reasons"):
            return assessment
        if messages_failed or messages is None:
            assessment["complete"] = False
            assessment["premature"] = True
            reasons = list(assessment.get("reasons") or [])
            reasons.append("completeness snapshot unavailable")
            assessment["reasons"] = reasons
        return assessment

    async def _assess_after_unattended_nudge(
        self,
        sid: str,
        *,
        compact_total: int,
        output_text: str = "",
        _emit: Callable[[str, str], None],
    ) -> Dict[str, Any]:
        """Re-assess after the unattended nudge, tolerating stale todo lag.

        Production failure mode: model finishes work (last assistant
        ``finish=stop``, no new question) but ``list_todos`` still shows the
        pre-todowrite pending list, and/or an *earlier* clarifying question
        poisoned completeness. That wrongly returned incomplete after a
        successful recovery.
        """
        assessment: Dict[str, Any] = {}
        messages: Optional[List[Dict[str, Any]]] = None
        messages_failed = False
        for attempt in range(3):
            messages, todos, messages_failed = await self._fetch_session_lists(
                sid, _emit=_emit
            )
            assessment = assess_serve_turn(
                sid,
                messages=messages,
                todos=todos,
                compact_events_seen=compact_total,
                new_compacts_this_turn=0,
                # Only this turn's reply — do not re-scan the pre-nudge log
                # for "compaction near end" false positives.
                output_text=output_text if attempt == 0 else "",
            )
            self._fail_closed_if_no_evidence(
                assessment, messages=messages, messages_failed=messages_failed
            )
            if not assessment.get("premature"):
                return assessment

            reasons = list(assessment.get("reasons") or [])
            still_asking = bool(assessment.get("assistant_asked_question")) or any(
                "clarifying question" in str(r).lower() for r in reasons
            )
            finish = str(assessment.get("last_finish") or "").strip().lower()
            clean_stop = (
                not still_asking
                and not assessment.get("last_is_summary")
                and finish not in {"", "tool-calls", "unknown"}
                and finish == "stop"
            )
            only_stale_todos = clean_stop and reasons and all(
                "open todos" in str(r).lower()
                or "clarifying question" in str(r).lower()
                for r in reasons
            ) and not still_asking

            if only_stale_todos and attempt < 2:
                _emit(
                    "stdout",
                    f"[serve] post-nudge: last turn looks finished but todos "
                    f"still open (attempt {attempt + 1}/3) — re-fetching "
                    f"(reasons={reasons})",
                )
                await asyncio.sleep(0.4)
                continue

            if only_stale_todos:
                # Todo API lag after todowrite. Last turn is a clean stop and
                # not waiting on the operator — accept; processor still gates
                # on plan file / git delivery.
                _emit(
                    "stdout",
                    "[serve] post-nudge: treating clean finish=stop as complete "
                    f"despite stale open todos (reasons={reasons})",
                )
                assessment["complete"] = True
                assessment["premature"] = False
                assessment["reasons"] = []
                assessment["stale_todos_ignored"] = True
                return assessment

            if still_asking:
                return assessment
            # Hard incomplete (unfinished finish, compact-then-stop, etc.)
            return assessment

        return assessment

    async def _snapshot_assess(
        self,
        sid: str,
        *,
        compact_total: int,
        output_text: str = "",
        _emit: Callable[[str, str], None],
        new_compacts_this_turn: int = 0,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any], int]:
        messages, todos, messages_failed = await self._fetch_session_lists(
            sid, _emit=_emit
        )
        raw = list(messages or [])
        compact_total = max(compact_total, len(compaction_marker_keys(raw)))
        assessment = assess_serve_turn(
            sid,
            messages=messages,
            todos=todos,
            compact_events_seen=compact_total,
            new_compacts_this_turn=new_compacts_this_turn,
            output_text=output_text,
        )
        self._fail_closed_if_no_evidence(
            assessment, messages=messages, messages_failed=messages_failed
        )
        return raw, assessment, compact_total

    async def _turn_after_compact_wait(
        self,
        sid: str,
        *,
        wait_info: Dict[str, Any],
        compact_total: int,
        continue_count: int,
        turns: List[Dict[str, Any]],
        lines: List[str],
        http_note: str = "",
        _emit: Callable[[str, str], None],
    ) -> ServeTurnResult:
        """Map a compact-wait outcome to a turn result. Never POSTs a user message."""
        compact_total = int(wait_info.get("compact_total") or compact_total or 0)
        if wait_info.get("aborted"):
            await self.client.abort(sid)
            return ServeTurnResult(
                session_id=sid,
                returncode=-1,
                stdout="\n".join(lines),
                stderr="Aborted while waiting for auto-compact",
                incomplete=True,
                incomplete_reasons=["aborted"],
                compact_events=compact_total,
                continue_count=continue_count,
                turns=turns,
                timed_out=bool(wait_info.get("timeout")),
            )
        assessment = wait_info.get("assessment")
        if not isinstance(assessment, dict) or wait_info.get("timeout"):
            _messages, assessment, compact_total = await self._snapshot_assess(
                sid, compact_total=compact_total, output_text="", _emit=_emit
            )
            strip_compact_reasons(assessment)
        if wait_info.get("complete") or not assessment.get("premature"):
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
        reasons = list(
            assessment.get("reasons")
            or wait_info.get("reasons")
            or ["auto-compact wait timed out"]
        )
        # Full wait budget used — this is a timeout, not a "compaction error"
        # and not a signal to inject Finish-todos / Continue.
        note = http_note or (
            "[TIMEOUT] still incomplete after waiting for auto-compact: "
            + "; ".join(map(str, reasons))
        )
        _emit("stderr", note)
        return ServeTurnResult(
            session_id=sid,
            returncode=-1,
            stdout="\n".join(lines),
            stderr=note,
            incomplete=False,
            incomplete_reasons=reasons,
            compact_events=compact_total,
            continue_count=continue_count,
            turns=turns,
            session_completeness=assessment,
            timed_out=True,
        )

    async def _ensure_session_idle(
        self,
        sid: str,
        *,
        _emit: Callable[[str, str], None],
        _aborted: Callable[[], bool],
        wait_seconds: float = 30.0,
    ) -> None:
        """Abort a leftover turn before posting a new job prompt.

        Cancel often kills our HTTP client without aborting OpenCode. A new
        POST /message then 500s while the old turn keeps editing files.
        """
        try:
            status = await self.client.session_status()
        except Exception as e:
            _emit("stdout", f"[serve] status check failed: {e}")
            return
        if not session_is_busy(status, sid):
            return
        _emit(
            "stdout",
            f"[serve] session {sid} still busy; aborting leftover turn",
        )
        await self.client.abort(sid)
        deadline = time.time() + max(1.0, float(wait_seconds))
        while time.time() < deadline:
            if _aborted():
                return
            await asyncio.sleep(0.15)
            try:
                status = await self.client.session_status()
            except Exception:
                return
            if not session_is_busy(status, sid):
                _emit("stdout", "[serve] session idle after abort")
                return
        _emit("stdout", "[serve] session still busy after abort wait")

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

        # Workdir before create/resume — OpenCode 500s on missing paths
        try:
            self.client.ensure_directory_ready()
        except FileNotFoundError as e:
            note = f"[serve] {e}"
            _emit("stderr", note)
            return ServeTurnResult(
                session_id=session_id,
                returncode=1,
                stdout="\n".join(lines),
                stderr=note,
                incomplete=False,
                incomplete_reasons=[str(e)],
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
            await self._ensure_session_idle(
                sid, _emit=_emit, _aborted=_aborted
            )
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

        # Preflight workdir before first message (clear error vs UnknownError 500)
        try:
            self.client.ensure_directory_ready()
        except FileNotFoundError as e:
            note = f"[serve] {e}"
            _emit("stderr", note)
            return ServeTurnResult(
                session_id=sid,
                returncode=1,
                stdout="\n".join(lines),
                stderr=note,
                incomplete=False,
                incomplete_reasons=[str(e)],
                compact_events=compact_total,
                continue_count=continue_count,
                turns=turns,
            )

        # Soft-check agent is registered (plugin missing → UnknownError 500)
        if agent:
            try:
                agents = await self.client.list_agents()
                names = {
                    str(a.get("name") or "").strip()
                    for a in agents
                    if isinstance(a, dict) and a.get("name")
                }
                if names and agent not in names:
                    sample = ", ".join(sorted(names)[:12])
                    note = (
                        f"[serve] agent {agent!r} is not registered on this "
                        f"OpenCode serve. Available (sample): {sample}. "
                        "Install/load oh-my-openagent or use a built-in agent "
                        "(build/plan)."
                    )
                    _emit("stderr", note)
                    return ServeTurnResult(
                        session_id=sid,
                        returncode=1,
                        stdout="\n".join(lines),
                        stderr=note,
                        incomplete=False,
                        incomplete_reasons=[f"unknown agent: {agent}"],
                        compact_events=compact_total,
                        continue_count=continue_count,
                        turns=turns,
                    )
            except Exception as e:
                _emit("stdout", f"[serve] agent list check skipped: {e}")

        current_prompt = prompt
        # One task prompt. Compact is waited out — we do not POST Continue
        # for auto-compact (that looked like the user typed it and broke the loop).
        max_turns = 1

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
                timed_out_send = is_serve_timeout(e)
                if timed_out_send:
                    err = format_serve_error(
                        e,
                        timeout_seconds=getattr(
                            self.client, "timeout_seconds", None
                        ),
                    )
                else:
                    err = explain_message_http_error(
                        e,
                        directory=getattr(self.client, "directory", None),
                        agent=agent,
                        model=model,
                    )
                note = f"[serve] message failed: {err}"
                _emit("stderr", note)
                if not timed_out_send:
                    # Only wait when status shows busy, or body says conflict.
                    # Generic 500 (missing dir/agent/model) must fail immediately.
                    busy500 = False
                    for _probe in range(6):
                        try:
                            st500 = await self.client.session_status()
                            busy500 = session_is_busy(st500, sid)
                        except Exception:
                            busy500 = False
                        if busy500:
                            break
                        await asyncio.sleep(0.2)
                    if busy500 or is_message_conflict_error(e):
                        _emit(
                            "stdout",
                            "[serve] session is busy — waiting for the "
                            "in-flight turn (no extra prompt)",
                        )
                        wait_info = await self._wait_for_auto_compact(
                            sid,
                            _emit=_emit,
                            _aborted=_aborted,
                            compact_total=compact_total,
                        )
                        return await self._turn_after_compact_wait(
                            sid,
                            wait_info=wait_info,
                            compact_total=compact_total,
                            continue_count=continue_count,
                            turns=turns,
                            lines=lines,
                            http_note=note,
                            _emit=_emit,
                        )
                    return ServeTurnResult(
                        session_id=sid,
                        returncode=1,
                        stdout="\n".join(lines),
                        stderr=note,
                        incomplete=False,
                        incomplete_reasons=[f"message error: {err}"],
                        compact_events=compact_total,
                        continue_count=continue_count,
                        turns=turns,
                        timed_out=False,
                    )
                # HTTP wait ended. Compact often idles the session for a few
                # seconds before auto-resume. Always wait — do not treat idle
                # as a hard timeout and retry with the original BUILD prompt.
                try:
                    status = await self.client.session_status()
                    status_ok = True
                except Exception:
                    status = {}
                    status_ok = False
                still_running = (not status_ok) or session_is_busy(status, sid)
                _emit(
                    "stdout",
                    "[serve] HTTP wait ended "
                    f"({'busy' if still_running else 'idle'}) — "
                    "waiting for auto-compact/auto-resume (no user prompt)",
                )
                wait_info = await self._wait_for_auto_compact(
                    sid,
                    _emit=_emit,
                    _aborted=_aborted,
                    compact_total=compact_total,
                )
                return await self._turn_after_compact_wait(
                    sid,
                    wait_info=wait_info,
                    compact_total=compact_total,
                    continue_count=continue_count,
                    turns=turns,
                    lines=lines,
                    http_note=note,
                    _emit=_emit,
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
            messages, todos, messages_failed = await self._fetch_session_lists(
                sid, _emit=_emit
            )
            raw_messages = list(messages or [])
            keys_after = compaction_marker_keys(raw_messages)
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
            self._fail_closed_if_no_evidence(
                assessment, messages=messages, messages_failed=messages_failed
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

            reasons = list(assessment.get("reasons") or [])
            asked_question = bool(assessment.get("assistant_asked_question")) or any(
                "clarifying question" in str(r).lower() for r in reasons
            )
            if asked_question:
                assessment["assistant_asked_question"] = True
            # Real auto-compact signals only — a clarifying question is a clean
            # stop waiting on a human, not mid-compaction. Do not burn the full
            # compact-wait budget on "Shall I continue?".
            had_compact = (
                reasons_are_compact_only(reasons)
                or int(new_this_turn or 0) > 0
                or int(compact_total or 0) > 0
                or any("compact" in str(r).lower() for r in reasons)
            )

            if assessment.get("premature") and had_compact and not asked_question:
                # Auto-compact is in-session. Wait; do not POST a user message.
                _emit(
                    "stdout",
                    "[serve] compact detected — waiting for auto-compact "
                    "(no Continue / Finish-todos prompt)",
                )
                wait_info = await self._wait_for_auto_compact(
                    sid,
                    _emit=_emit,
                    _aborted=_aborted,
                    compact_total=compact_total,
                )
                # After compact settles the model may still ask a question.
                post = wait_info.get("assessment")
                post_reasons = (
                    list(post.get("reasons") or [])
                    if isinstance(post, dict)
                    else []
                )
                post_asked = isinstance(post, dict) and (
                    bool(post.get("assistant_asked_question"))
                    or any(
                        "clarifying question" in str(r).lower()
                        for r in post_reasons
                    )
                )
                if post_asked and not wait_info.get("complete"):
                    assessment = post if isinstance(post, dict) else assessment
                    assessment["assistant_asked_question"] = True
                    reasons = post_reasons or reasons
                    asked_question = True
                else:
                    return await self._turn_after_compact_wait(
                        sid,
                        wait_info=wait_info,
                        compact_total=compact_total,
                        continue_count=continue_count,
                        turns=turns,
                        lines=lines,
                        _emit=_emit,
                    )

            if assessment.get("premature") and asked_question:
                # One unattended nudge: product is one-pass, no human answers.
                # Do not re-send the original BUILD/PLAN kit.
                _emit(
                    "stdout",
                    "[serve] assistant asked a clarifying question — "
                    "sending one unattended nudge (no human reply path)",
                )
                nudge_text = ""
                try:
                    nudge_msg = await self.client.send_message(
                        sid,
                        DEFAULT_UNATTENDED_NUDGE_PROMPT,
                        agent=agent,
                        model=model,
                    )
                    continue_count += 1
                    nudge_parts = []
                    for p in (nudge_msg or {}).get("parts") or []:
                        if (
                            isinstance(p, dict)
                            and p.get("type") == "text"
                            and p.get("text")
                        ):
                            nudge_parts.append(str(p["text"]))
                    nudge_text = "\n".join(nudge_parts)
                    if nudge_text:
                        _emit("stdout", nudge_text[:4000])
                except Exception as e:
                    note = (
                        f"[INCOMPLETE] clarifying question and unattended "
                        f"nudge failed: {e}; reasons: "
                        f"{'; '.join(map(str, reasons))}"
                    )
                    _emit("stderr", note)
                    return ServeTurnResult(
                        session_id=sid,
                        returncode=2,
                        stdout="\n".join(lines),
                        stderr=note,
                        incomplete=True,
                        incomplete_reasons=list(reasons)
                        + ["unattended nudge failed"],
                        compact_events=compact_total,
                        continue_count=continue_count,
                        turns=turns,
                        session_completeness=assessment,
                        progress=50,
                    )

                assessment = await self._assess_after_unattended_nudge(
                    sid,
                    compact_total=compact_total,
                    output_text=nudge_text,
                    _emit=_emit,
                )
                turns.append(
                    {
                        "turn": "unattended_nudge",
                        "assessment": {
                            "complete": assessment.get("complete"),
                            "premature": assessment.get("premature"),
                            "reasons": assessment.get("reasons"),
                            "open_todos": assessment.get("open_todos"),
                            "assistant_asked_question": assessment.get(
                                "assistant_asked_question"
                            ),
                        },
                    }
                )
                if not assessment.get("premature"):
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
                reasons = list(assessment.get("reasons") or reasons)
                still_asking = bool(
                    assessment.get("assistant_asked_question")
                ) or any(
                    "clarifying question" in str(r).lower() for r in reasons
                )
                if still_asking:
                    note = (
                        "[INCOMPLETE] assistant asked a clarifying question "
                        "(unattended; no human reply path). After one nudge "
                        f"still incomplete: {'; '.join(map(str, reasons))}"
                    )
                else:
                    note = (
                        "[INCOMPLETE] after unattended nudge still incomplete: "
                        f"{'; '.join(map(str, reasons))}"
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

            # Still incomplete (non-compact, non-question). Do not inject Continue.
            note = (
                f"[INCOMPLETE] session still incomplete: "
                f"{'; '.join(map(str, reasons))}"
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
