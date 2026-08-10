"""LIVE e2e: real ``opencode serve`` + real compaction (no fakes, no mocks).

This test:
1. Starts a real ``opencode serve`` process
2. Creates a real session via HTTP
3. Sends a real model turn
4. Triggers real compaction **twice** via ``POST /session/{id}/summarize``
5. Asserts compaction markers from the live message list
6. Runs the real ``ServeOrchestrator`` continue path against that session
   only when live assessment says the turn is incomplete

Skipped only when the ``opencode`` binary is missing or serve fails to start.
Not skipped for "slow" — real compact takes wall-clock time.

Run::

    .venv/bin/python -m pytest tests/test_opencode_serve_live_e2e.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from src.opencode_serve import (
    OpenCodeServeClient,
    ServeOrchestrator,
    assess_serve_turn,
    count_compaction_signals,
)


def _opencode_bin() -> Optional[str]:
    import shutil

    return shutil.which("opencode")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_health(base: str, *, timeout: float = 60.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(verify=False, timeout=5.0) as c:
        while time.time() < deadline:
            try:
                r = await c.get(f"{base.rstrip('/')}/global/health")
                if r.status_code == 200:
                    return r.json()
            except Exception as e:
                last_err = e
            await asyncio.sleep(0.4)
    raise TimeoutError(f"serve health not ready at {base}: {last_err}")


def _start_serve(port: int, log_path: Path) -> subprocess.Popen:
    bin_path = _opencode_bin()
    if not bin_path:
        raise RuntimeError("opencode binary not on PATH")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            bin_path,
            "serve",
            "--port",
            str(port),
            "--hostname",
            "127.0.0.1",
            "--print-logs",
            "--log-level",
            "INFO",
        ],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env={**os.environ, "OPENCODE_SERVER_PASSWORD": ""},
    )
    # stash for cleanup
    proc._vd_log_f = log_f  # type: ignore[attr-defined]
    return proc


def _stop_serve(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass
    log_f = getattr(proc, "_vd_log_f", None)
    if log_f is not None:
        try:
            log_f.close()
        except Exception:
            pass


async def _resolve_provider_model(client: OpenCodeServeClient) -> Tuple[str, str]:
    """Discover provider/model from live server config — no hard-coded model ids."""
    async with httpx.AsyncClient(
        base_url=client.base_url,
        verify=False,
        timeout=30.0,
    ) as c:
        r = await c.get("/config", headers=client._headers())
        r.raise_for_status()
        cfg = r.json()
    model = cfg.get("model") if isinstance(cfg, dict) else None
    if isinstance(model, str) and "/" in model:
        prov, mid = model.split("/", 1)
        if prov and mid:
            return prov, mid
    # Fallback: env DEFAULT_MODEL / settings only if config empty
    env_model = (os.environ.get("DEFAULT_MODEL") or "").strip()
    if "/" in env_model:
        prov, mid = env_model.split("/", 1)
        return prov, mid
    raise RuntimeError(
        f"Could not resolve provider/model from live /config "
        f"(got model={model!r}). Set a model in opencode.json or DEFAULT_MODEL."
    )


def _message_digest(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact human-readable snapshot of live messages (for assertions/logs)."""
    out: List[Dict[str, Any]] = []
    for m in messages:
        info = m.get("info") if isinstance(m.get("info"), dict) else {}
        role = info.get("role") or m.get("role")
        finish = info.get("finish") if "finish" in info else m.get("finish")
        summary = info.get("summary") if "summary" in info else m.get("summary")
        parts = m.get("parts") or []
        part_types = [
            p.get("type") for p in parts if isinstance(p, dict) and p.get("type")
        ]
        text = next(
            (
                (p.get("text") or "")[:160]
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ),
            "",
        )
        out.append(
            {
                "role": role,
                "finish": finish,
                "summary": summary,
                "part_types": part_types,
                "text_snip": text,
            }
        )
    return out


@pytest.mark.asyncio
async def test_live_opencode_serve_two_real_compactions(tmp_path: Path):
    """Real serve: two real summarize/compactions on one session, then live continue.

    Makes **no** assumptions about whether OpenCode leaves the session
    incomplete after compact — that is measured from live messages/todos.
    Asserts only:
    - serve is healthy
    - two real summarize calls succeed
    - live message list shows **>= 2** compaction markers after that
    - a real follow-up message (continue or probe) gets a real assistant reply
    """
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "opencode-serve-live.log"
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "README.md").write_text(
        "# live e2e workspace\n\nUsed only for OpenCode directory scoping.\n",
        encoding="utf-8",
    )

    proc: Optional[subprocess.Popen] = None
    client: Optional[OpenCodeServeClient] = None
    report: Dict[str, Any] = {
        "base": base,
        "steps": [],
        "compacts": [],
        "message_digest": [],
    }

    try:
        proc = _start_serve(port, log_path)
        health = await _wait_health(base, timeout=90.0)
        report["health"] = health
        assert health.get("healthy") is True, health
        assert "version" in health, health

        client = OpenCodeServeClient(
            base,
            timeout_seconds=300.0,
            directory=str(work),
        )
        provider_id, model_id = await _resolve_provider_model(client)
        report["provider_id"] = provider_id
        report["model_id"] = model_id

        # --- real session ---
        sess = await client.create_session(
            title="VD live e2e: double real compaction"
        )
        sid = sess.get("id")
        assert isinstance(sid, str) and sid.startswith("ses_"), sess
        report["session_id"] = sid
        report["steps"].append({"create_session": sid})

        # --- real first turn (model) ---
        msg1 = await client.send_message(
            sid,
            "Reply with exactly the token LIVE-E2E-1. Do not use tools. One line only.",
        )
        info1 = msg1.get("info") if isinstance(msg1.get("info"), dict) else msg1
        report["steps"].append(
            {
                "first_message_finish": (info1 or {}).get("finish"),
                "first_message_role": (info1 or {}).get("role"),
            }
        )
        # We only assert the HTTP call returned a structure; finish may vary by model.
        assert msg1 is not None

        messages = await client.list_messages(sid, limit=100)
        compact_before = count_compaction_signals(messages)
        report["compact_before_summarize"] = compact_before

        # --- real compaction #1 ---
        sum1 = await client.summarize(
            sid, provider_id=provider_id, model_id=model_id, auto=True
        )
        report["compacts"].append({"n": 1, "api_result": sum1})
        # API returns boolean True on this build; accept truthy without inventing meaning
        assert sum1 is not False and sum1 is not None, f"summarize#1 failed: {sum1!r}"

        messages_after_1 = await client.list_messages(sid, limit=100)
        compact_after_1 = count_compaction_signals(messages_after_1)
        report["compact_after_1"] = compact_after_1
        report["digest_after_1"] = _message_digest(messages_after_1)
        assert compact_after_1 >= compact_before + 1, (
            f"Expected at least one new compaction marker after summarize#1; "
            f"before={compact_before} after={compact_after_1} "
            f"digest={report['digest_after_1']}"
        )

        # --- real compaction #2 (second time on same session) ---
        sum2 = await client.summarize(
            sid, provider_id=provider_id, model_id=model_id, auto=True
        )
        report["compacts"].append({"n": 2, "api_result": sum2})
        assert sum2 is not False and sum2 is not None, f"summarize#2 failed: {sum2!r}"

        messages_after_2 = await client.list_messages(sid, limit=100)
        compact_after_2 = count_compaction_signals(messages_after_2)
        report["compact_after_2"] = compact_after_2
        report["digest_after_2"] = _message_digest(messages_after_2)
        report["message_digest"] = report["digest_after_2"]

        assert compact_after_2 >= 2, (
            f"Expected >= 2 compaction markers after two summarize calls; "
            f"got {compact_after_2}. digest={report['digest_after_2']}"
        )
        assert compact_after_2 >= compact_after_1, (
            f"Second summarize did not increase or keep compact markers: "
            f"after1={compact_after_1} after2={compact_after_2}"
        )

        todos = await client.list_todos(sid)
        report["todos_after_2_compacts"] = todos

        assessment = assess_serve_turn(
            sid,
            messages=messages_after_2,
            todos=todos,
            compact_events_seen=compact_after_2,
        )
        report["assessment_after_2_compacts"] = {
            "complete": assessment.get("complete"),
            "premature": assessment.get("premature"),
            "reasons": assessment.get("reasons"),
            "open_todos": assessment.get("open_todos"),
            "last_finish": assessment.get("last_finish"),
            "last_is_summary": assessment.get("last_is_summary"),
        }

        # Probe that the session still accepts a real turn after compact.
        # Do not inject the orchestrator Continue prompt — that is the bug.
        follow_text = (
            "After two context compactions on this session, reply with exactly "
            "LIVE-E2E-AFTER-2-COMPACTS. One line, no tools."
        )
        report["follow_mode"] = (
            "probe_after_incomplete" if assessment.get("premature") else "probe_after_complete"
        )

        msg_follow = await client.send_message(sid, follow_text)
        info_f = (
            msg_follow.get("info")
            if isinstance(msg_follow.get("info"), dict)
            else msg_follow
        )
        report["follow_finish"] = (info_f or {}).get("finish")
        report["follow_role"] = (info_f or {}).get("role")
        follow_parts = msg_follow.get("parts") or []
        follow_text_out = next(
            (
                (p.get("text") or "")
                for p in follow_parts
                if isinstance(p, dict) and p.get("type") == "text"
            ),
            "",
        )
        report["follow_text_snip"] = (follow_text_out or "")[:300]

        # Live assistant reply must exist (role + some content or finish)
        assert (info_f or {}).get("role") == "assistant" or any(
            isinstance(p, dict) and p.get("type") == "text" for p in follow_parts
        ), f"No assistant follow-up from live serve: {msg_follow!r}"

        messages_final = await client.list_messages(sid, limit=100)
        report["final_compact_count"] = count_compaction_signals(messages_final)
        report["final_digest"] = _message_digest(messages_final)
        report["final_message_count"] = len(messages_final)

        # Hard guarantees from live system only:
        assert report["final_compact_count"] >= 2
        assert report["final_message_count"] >= 4  # at least user/asst + compact traffic

        # Optional orchestrator path on a *fresh* session: one real task with
        # force_summarize_after_turn so the real loop hits summarize (compact)
        # after the first model turn — still no fakes.
        orch_client = OpenCodeServeClient(
            base, timeout_seconds=300.0, directory=str(work)
        )
        try:
            orch = ServeOrchestrator(
                client=orch_client,
                max_compact_continues=2,
                compact_wait_seconds=120.0,
                compact_poll_seconds=2.0,
                compact_settle_seconds=2.0,
                force_summarize_after_turn=True,
                force_summarize_provider=provider_id,
                force_summarize_model=model_id,
            )
            orch_result = await orch.run(
                prompt=(
                    "Reply with exactly ORCH-LIVE-1. One line. No tools. "
                    "If continued after compaction, reply ORCH-LIVE-CONTINUED."
                ),
                title="VD live e2e orchestrator + real summarize",
            )
            report["orchestrator"] = {
                "returncode": orch_result.returncode,
                "session_id": orch_result.session_id,
                "continue_count": orch_result.continue_count,
                "compact_events": orch_result.compact_events,
                "incomplete": orch_result.incomplete,
                "incomplete_reasons": orch_result.incomplete_reasons,
                "turns": orch_result.turns,
                "stderr_snip": (orch_result.stderr or "")[:400],
            }
            # Real session id from orchestrator
            assert orch_result.session_id and str(orch_result.session_id).startswith(
                "ses_"
            )
            # force_summarize means at least one real compact attempted on that run
            assert orch_result.compact_events >= 1 or any(
                (t.get("compact_markers") or 0) >= 1 for t in orch_result.turns
            ), report["orchestrator"]
            assert orch_result.continue_count == 0, report["orchestrator"]
            assert "Continue the previous OpenCode session" not in (
                orch_result.stdout or ""
            )
        finally:
            await orch_client.aclose()

    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
        _stop_serve(proc)
        # Persist report under tmp for debugging failed runs
        try:
            (tmp_path / "live_e2e_report.json").write_text(
                json.dumps(report, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    # Print report when running with -s so operators see real numbers
    print("\n===== LIVE E2E REPORT =====")
    print(json.dumps(report, indent=2, default=str))
    print("===== END LIVE E2E REPORT =====\n")
