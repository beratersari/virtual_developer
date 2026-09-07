"""LIVE: large git tree + large prompt must auto-compact without a user nudge.

Reproduces the dashboard chat the operator saw::

    Session auto-compacted
    Session compacted
    You: "Finish remaining todos and complete the original task..."

That second user turn is injected by Virtual Developer after a short idle
settle. Compact is OpenCode's job — we wait, we do not POST.

This test copies the real ``src/`` tree into a throwaway git repo, starts a
real ``opencode serve`` with a reduced context window (so compact fires),
and runs ServeOrchestrator + the retry layer.

Run::

    .venv/bin/python -m pytest tests/test_live_large_repo_compact_e2e.py -v -s
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.opencode_serve import (
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_FINISH_TODOS_PROMPT,
    OpenCodeServeClient,
    ServeOrchestrator,
    count_compaction_signals,
)
from src.opencode_sessions import (
    is_internal_compact_followup_text,
    is_orchestrator_continue_text,
)
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from tests.test_opencode_serve_live_e2e import (
    _free_port,
    _opencode_bin,
    _stop_serve,
    _wait_health,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TINY_CONTEXT = 32768
MODEL_ID = "deepseek-v4-flash-free"
PROVIDER_ID = "opencode"
FULL_MODEL = f"{PROVIDER_ID}/{MODEL_ID}"

LARGE_PROMPT = (
    "Write a README inventory of every source file in this git repository. "
    "For each file under src/, create docs/inventory/<relative-path>.md with "
    "a 3-sentence description of what the file does. Work systematically "
    "through the whole tree. Do not stop until every file has a page. "
    "This is a long job; keep going after context compaction."
)


def _user_texts(messages: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for m in messages:
        info = m.get("info") if isinstance(m.get("info"), dict) else m
        role = ""
        if isinstance(info, dict):
            role = str(info.get("role") or m.get("role") or "")
        if role != "user":
            continue
        parts = m.get("parts") or m.get("_parts") or []
        texts = [
            str(p.get("text") or "")
            for p in parts
            if isinstance(p, dict) and (p.get("type") or "text") == "text"
        ]
        blob = "\n".join(t for t in texts if t.strip())
        if blob.strip():
            out.append(blob)
    return out


@pytest.mark.asyncio
async def test_live_large_repo_readme_prompt_does_not_inject_after_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    from src.config import settings

    project = tmp_path / "large_repo"
    project.mkdir()
    # Real product tree — enough files that "readme every file" blows a 32k window.
    shutil.copytree(REPO_ROOT / "src", project / "src")
    shutil.copytree(REPO_ROOT / "agent", project / "agent")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=vd@test",
            "-c",
            "user.name=vd",
            "commit",
            "-m",
            "seed large tree",
        ],
        cwd=project,
        check=True,
        capture_output=True,
    )
    file_count = sum(1 for p in (project / "src").rglob("*") if p.is_file())
    assert file_count >= 40, file_count

    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "model": FULL_MODEL,
        "autoupdate": False,
        "plugin": ["oh-my-openagent@latest"],
        "provider": {
            PROVIDER_ID: {
                "models": {
                    MODEL_ID: {
                        "limit": {"context": TINY_CONTEXT, "output": 2048}
                    }
                }
            }
        },
    }
    (project / "opencode.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "serve-large.log"
    bin_path = _opencode_bin()
    assert bin_path
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

    monkeypatch.setattr(settings, "opencode_serve_url", base)
    monkeypatch.setattr(settings, "default_model", FULL_MODEL)
    monkeypatch.setattr(settings, "agent_task_max_incomplete_retries", 256)
    monkeypatch.setattr(settings, "agent_task_max_retries", 2)

    client = OpenCodeServeClient(
        base, timeout_seconds=420.0, directory=str(project)
    )
    try:
        await _wait_health(base, timeout=90.0)
        orch = ServeOrchestrator(
            client=client,
            compact_wait_seconds=90.0,
            compact_poll_seconds=2.0,
            compact_settle_seconds=2.0,
        )
        t0 = time.monotonic()
        result = await orch.run(
            prompt=LARGE_PROMPT,
            title="LARGE: readme every file",
        )
        elapsed = time.monotonic() - t0
        print(
            f"[large-repo] orch stderr={result.stderr!r} "
            f"returncode={result.returncode}",
            flush=True,
        )
        messages = await client.list_all_messages(result.session_id or "")
        user_blobs = _user_texts(messages)
        compact_n = count_compaction_signals(messages)
        print(
            f"[large-repo] files={file_count} elapsed={elapsed:.1f}s "
            f"returncode={result.returncode} timed_out={result.timed_out} "
            f"incomplete={result.incomplete} continues={result.continue_count} "
            f"compacts={compact_n} user_turns={len(user_blobs)} "
            f"session={result.session_id}",
            flush=True,
        )
        for i, blob in enumerate(user_blobs):
            print(f"[large-repo] user[{i}]={blob[:180]!r}", flush=True)

        assert result.continue_count == 0
        vd_injected = [
            b
            for b in user_blobs
            if is_orchestrator_continue_text(b)
            or DEFAULT_FINISH_TODOS_PROMPT in b
            or DEFAULT_CONTINUE_PROMPT in b
            or "Finish remaining todos" in b
        ]
        assert vd_injected == [], (
            "Virtual Developer injected a user turn after compact: "
            f"{vd_injected!r}"
        )
        assert user_blobs and user_blobs[0].startswith("Write a README inventory")
        # Plugin-internal auto-continue / todo-continuation may appear as
        # role=user; those are not VD posts and must not render as "You".
        for blob in user_blobs[1:]:
            assert is_internal_compact_followup_text(blob) or is_orchestrator_continue_text(
                blob
            ), blob[:200]
        # Compact should fire on this tree+prompt; if the model finishes
        # without compacting we still proved no injection.
        if compact_n == 0:
            print("[large-repo] warning: no compact markers this run", flush=True)

        # Retry layer must not send Finish-todos either.
        runner = AgentRunner(working_directory=project)

        async def once(task, **kwargs):
            return result.to_agent_result(task.task_id)

        monkeypatch.setattr(runner, "run_agent", once)
        task = AgentTask(
            description="readme every file",
            prompt=LARGE_PROMPT,
            agent="atlas",
            issue_key="KAN-LARGE",
            session_id=result.session_id,
        )
        retried = await runner.run_agent_with_retry(
            task,
            max_retries=2,
            max_incomplete_retries=256,
        )
        assert retried.get("retry_info", {}).get("retried") is not True
        assert task.prompt == LARGE_PROMPT
        assert "Finish remaining todos" not in (task.prompt or "")
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
        _stop_serve(proc)
