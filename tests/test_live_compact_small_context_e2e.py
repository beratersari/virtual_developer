"""LIVE e2e: force OpenCode auto-compaction by shrinking the model context limit.

Not a fake backend. Starts a real ``opencode serve`` whose project
``opencode.json`` sets ``provider.opencode.models.*.limit.context`` far below
the free-tier default (200k) so auto-compact fires during one Atlas turn.

Then asserts Virtual Developer does **not** treat compact-then-stop as success:
``ServeOrchestrator`` must continue (or fail incomplete), not returncode=0
with zero continues after compaction markers.

Opt-in (hits the real model; slow)::

    VD_LIVE_COMPACT=1 .venv/bin/python -m pytest \\
        tests/test_live_compact_small_context_e2e.py -v -s
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pytest

from src.opencode_serve import (
    OpenCodeServeClient,
    ServeOrchestrator,
    count_compaction_signals,
)
from tests.test_opencode_serve_live_e2e import (
    _free_port,
    _opencode_bin,
    _stop_serve,
    _wait_health,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# Small enough that reading docs/pad_*.txt will trip auto-compact, large enough
# that the first user message is not rejected as already over-limit (4096 500'd).
TINY_CONTEXT = 16384
MODEL_ID = "deepseek-v4-flash-free"
PROVIDER_ID = "opencode"
FULL_MODEL = f"{PROVIDER_ID}/{MODEL_ID}"


def _write_small_context_config(project: Path) -> Path:
    """Project opencode.json that shrinks flash-free context to TINY_CONTEXT."""
    project.mkdir(parents=True, exist_ok=True)
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "model": FULL_MODEL,
        "autoupdate": False,
        "plugin": ["oh-my-openagent@latest"],
        "provider": {
            PROVIDER_ID: {
                "models": {
                    MODEL_ID: {
                        "limit": {
                            "context": TINY_CONTEXT,
                            "output": 1024,
                        }
                    }
                }
            }
        },
    }
    path = project / "opencode.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def _seed_workspace(work: Path) -> None:
    """Enough text that reading files + BUILD_PROMPT exceeds a 4k context."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "main.cpp").write_text(
        "#include <iostream>\nint main() {\n"
        "    std::cout << 20 + 4 << std::endl;\n    return 0;\n}\n",
        encoding="utf-8",
    )
    (work / "README.md").write_text(
        "Tiny C++ test project. No Makefile. Change main.cpp per Jira.\n",
        encoding="utf-8",
    )
    docs = work / "docs"
    docs.mkdir()
    for i in range(12):
        line = f"lorem ipsum dolor sit amet context filler file={i:02d}\n"
        (docs / f"pad_{i:02d}.txt").write_text(line * 80, encoding="utf-8")


async def _provider_context_limit(base: str, *, directory: str) -> Optional[int]:
    async with httpx.AsyncClient(verify=False, timeout=30.0) as c:
        r = await c.get(
            f"{base.rstrip('/')}/provider",
            headers={"x-opencode-directory": directory},
        )
        r.raise_for_status()
        data = r.json()
    blob = json.dumps(data)
    # Walk for the model object
    def find(o: Any) -> Optional[int]:
        if isinstance(o, dict):
            if o.get("id") == MODEL_ID or o.get("id") == FULL_MODEL:
                lim = o.get("limit") or {}
                if isinstance(lim, dict) and "context" in lim:
                    try:
                        return int(lim["context"])
                    except (TypeError, ValueError):
                        return None
            for v in o.values():
                hit = find(v)
                if hit is not None:
                    return hit
        elif isinstance(o, list):
            for v in o:
                hit = find(v)
                if hit is not None:
                    return hit
        return None
    return find(data)


@pytest.mark.asyncio
async def test_live_auto_compact_via_reduced_context_is_not_false_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    flag = (os.environ.get("VD_LIVE_COMPACT") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        pytest.skip("Set VD_LIVE_COMPACT=1 to force real auto-compact via tiny context")
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    from src.config import settings

    project = tmp_path / "oc_project"
    work = project / "workspace"
    _write_small_context_config(project)
    _seed_workspace(work)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "serve-small-ctx.log"
    proc = None

    monkeypatch.setattr(settings, "opencode_run_mode", "serve")
    monkeypatch.setattr(settings, "opencode_serve_url", base)
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "default_model", FULL_MODEL)

    report: Dict[str, Any] = {
        "base": base,
        "tiny_context": TINY_CONTEXT,
        "model": FULL_MODEL,
        "phases": {},
    }

    # Start serve *from the project dir* so opencode.json limit applies.
    bin_path = _opencode_bin()
    assert bin_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")
    import subprocess

    t0 = time.monotonic()
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
        cwd=str(project),
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env={
            **os.environ,
            "OPENCODE_SERVER_PASSWORD": "",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
        },
    )
    proc._vd_log_f = log_f  # type: ignore[attr-defined]

    try:
        health = await _wait_health(base, timeout=90.0)
        report["phases"]["serve_ready_s"] = round(time.monotonic() - t0, 3)
        report["health"] = health
        assert health.get("healthy") is True, health

        applied = await _provider_context_limit(base, directory=str(work))
        report["applied_context_limit"] = applied
        print(
            f"\n[live compact] provider context limit={applied} "
            f"(wanted {TINY_CONTEXT})",
            flush=True,
        )
        if applied is None:
            pytest.fail(
                "Could not read provider limit for deepseek-v4-flash-free. "
                f"serve log:\n{log_path.read_text(encoding='utf-8', errors='replace')[-2000:]}"
            )
        if applied > TINY_CONTEXT * 2:
            pytest.fail(
                f"Context limit did not shrink (got {applied}, wanted {TINY_CONTEXT}). "
                "OpenCode ignored project opencode.json provider.models.limit. "
                "Cannot force auto-compact this way."
            )

        # Intentionally NOT the full BUILD_PROMPT (that alone 500'd at 4k context).
        # Fill the tiny window by reading pad files so OpenCode auto-compacts.
        prompt = (
            "You are running a compaction e2e.\n"
            "1. Read EVERY file under docs/ (pad_00.txt through pad_11.txt) in full.\n"
            "2. After all reads, change main.cpp so it prints 5+4.\n"
            "3. Do not install compilers or download packages.\n"
            "4. Reply with COMPACT-E2E-DONE when finished.\n"
        )
        report["prompt_chars"] = len(prompt)

        client = OpenCodeServeClient(
            base,
            timeout_seconds=600.0,
            directory=str(work),
        )
        orch = ServeOrchestrator(
            client=client,
            max_compact_continues=3,
        )
        t_run = time.monotonic()
        result = await orch.run(
            prompt=prompt,
            title="E2E-COMPACT: tiny context auto-compact",
            agent="explore",
            model=FULL_MODEL,
        )
        report["phases"]["orchestrator_s"] = round(time.monotonic() - t_run, 3)
        report["returncode"] = result.returncode
        report["incomplete"] = result.incomplete
        report["continue_count"] = result.continue_count
        report["compact_events"] = result.compact_events
        report["incomplete_reasons"] = result.incomplete_reasons
        report["turns"] = [
            {
                "turn": t.get("turn"),
                "finish": t.get("finish"),
                "summary": t.get("summary"),
                "premature": (t.get("assessment") or {}).get("premature"),
                "reasons": (t.get("assessment") or {}).get("reasons"),
                "compact_markers": t.get("compact_markers"),
            }
            for t in (result.turns or [])
        ]
        report["session_id"] = result.session_id

        # Re-fetch messages to count live compact markers
        try:
            msgs = await client.list_messages(result.session_id or "", limit=100)
            report["live_compact_markers"] = count_compaction_signals(msgs)
        except Exception as e:
            report["live_compact_markers_error"] = str(e)

        print(
            "\n[live compact small-context e2e]\n"
            + json.dumps(report, indent=2, default=str),
            flush=True,
        )

        compacted = (
            int(report.get("compact_events") or 0) >= 1
            or int(report.get("live_compact_markers") or 0) >= 1
            or any(
                (t.get("compact_markers") or 0) >= 1 or t.get("premature")
                for t in report["turns"]
            )
        )
        assert compacted, (
            "Tiny context did not produce a compaction or premature assessment. "
            f"report={report}"
        )
        # The bug: compact happened and we still returned success with 0 continues.
        false_complete = (
            result.returncode == 0
            and result.continue_count == 0
            and int(report.get("compact_events") or 0) >= 1
        )
        assert not false_complete, (
            "FALSE COMPLETE: compaction occurred but orchestrator returned "
            f"returncode=0 with continue_count=0. report={report}"
        )
        # Accept either: continued then finished, or exhausted continues as incomplete.
        assert result.returncode in (0, 2), report
        if result.returncode == 0:
            assert result.continue_count >= 1, report
    finally:
        try:
            if proc is not None:
                await OpenCodeServeClient(base, timeout_seconds=5.0).aclose()
        except Exception:
            pass
        _stop_serve(proc)
        out = tmp_path / "live-compact-report.json"
        try:
            out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass
        print(f"[live compact] report -> {out}", flush=True)
        print(
            f"[live compact] serve log tail:\n"
            f"{log_path.read_text(encoding='utf-8', errors='replace')[-1500:]}",
            flush=True,
        )
