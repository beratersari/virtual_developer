"""LIVE: plan → build → test → build → test → build → test on one issue.

Uses real ``opencode serve`` and a free Zen model. Same repo / work /
target. Processor attach must resume the matching kind only:

- one plan ``ses_*``
- one build ``ses_*`` (steps 2, 4, 6)
- one test ``ses_*`` (steps 3, 5, 7)

Skipped only when the ``opencode`` binary is missing or serve will not start.

Run::

    set VD_LIVE_KIND_MAP=1
    .venv/Scripts/python -m pytest tests/test_live_plan_build_test_sessions_e2e.py -v -s --tb=short
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pytest

from src.config import settings
from src.orchestrator.agent_runner import AgentTask
from src.opencode_serve import OpenCodeServeClient
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.session_bind_store import SessionBindStore


REPO = "https://gitlab.example.com/acme/app.git"
WORK = "feature/KAN-12"
TARGET = "develop"
ISSUE = "KAN-12"
SEQUENCE = ("plan", "build", "test", "build", "test", "build", "test")
_WF = {"plan": "planning", "build": "execution", "test": "testing"}
_STATUS = {
    "plan": TaskStatus.PLANNING,
    "build": TaskStatus.EXECUTING,
    "test": TaskStatus.EXECUTING,
}
# Prefer small free Zen models. deepseek-v4-flash-free has been rotated off.
_FREE_PREFERENCE = (
    "opencode/north-mini-code-free",
    "opencode/ling-3.0-flash-free",
    "opencode/laguna-s-2.1-free",
    "opencode/mimo-v2.5-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/deepseek-v4-flash-free",
)


def _opencode_bin() -> Optional[str]:
    import shutil

    return shutil.which("opencode")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_health(base: str, *, timeout: float = 90.0) -> Dict[str, Any]:
    deadline = time.time() + timeout
    last_err: Optional[Exception] = None
    async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(f"{base.rstrip('/')}/global/health")
                if resp.status_code == 200:
                    return resp.json()
            except Exception as exc:
                last_err = exc
            await asyncio.sleep(0.4)
    raise TimeoutError(f"serve health not ready at {base}: {last_err}")


def _start_serve(port: int, log_path: Path) -> subprocess.Popen:
    bin_path = _opencode_bin()
    if not bin_path:
        raise RuntimeError("opencode binary not found on PATH")
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


def _pick_free_model(known: List[str]) -> str:
    known_set = {str(mid).strip() for mid in known if str(mid).strip()}
    for mid in _FREE_PREFERENCE:
        if mid in known_set:
            return mid
    for mid in known:
        raw = str(mid).strip()
        if raw.startswith("opencode/") and raw.endswith("-free"):
            return raw
    raise RuntimeError(
        f"No free OpenCode model in live inventory: {known[:20]!r}"
    )


def _processor(tmp_path: Path, isolate: Dict[str, Any], monkeypatch) -> JobProcessor:
    monkeypatch.setattr(settings, "jira_host", "")
    monkeypatch.setattr(settings, "jira_api_token", "")
    proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.job_store = isolate["job_store"]
    proc.queue_store = isolate["queue_store"]
    return proc


class _Git:
    def __init__(self, clone: Path) -> None:
        self.remote_url = REPO
        self.work_branch = WORK
        self.target_branch = TARGET
        self._clone = clone

    def get_working_directory(self) -> Path:
        return self._clone


def _live_flag() -> str:
    flag = (os.environ.get("VD_LIVE_KIND_MAP") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        return "Set VD_LIVE_KIND_MAP=1 to run the live plan/build/test session e2e"
    return ""


@pytest.mark.asyncio
async def test_live_sequential_plan_build_test_keeps_three_sessions(
    tmp_path, isolate_jira_agent_artifacts, monkeypatch
):
    reason = _live_flag()
    if reason:
        pytest.skip(reason)
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "README.md").write_text("# live kind-map workspace\n", encoding="utf-8")
    log_path = tmp_path / "opencode-serve-kind-map.log"

    isolate = isolate_jira_agent_artifacts
    store: SessionBindStore = isolate["session_bind_store"]
    proc = _processor(tmp_path, isolate, monkeypatch)
    git = _Git(work)
    proc.state_manager.create_state(
        ISSUE,
        "cover login",
        "{params}\nMode: plan\n"
        f"Repository: {REPO}\nSource branch: develop\n"
        f"Target branch: {TARGET}\n{{params}}",
    )
    proc._contexts[ISSUE] = {"git": git, "runner": None}

    serve: Optional[subprocess.Popen] = None
    client: Optional[OpenCodeServeClient] = None
    steps: List[Tuple[str, str, bool]] = []
    try:
        serve = _start_serve(port, log_path)
        health = await _wait_health(base, timeout=90.0)
        assert health.get("healthy") is True, health

        client = OpenCodeServeClient(
            base,
            timeout_seconds=180.0,
            directory=str(work),
        )
        known = await client.list_known_models()
        model = _pick_free_model(known)
        print(f"\nLIVE free model: {model}")
        print(f"known free: {[m for m in known if 'free' in m]}")

        for index, kind in enumerate(SEQUENCE, start=1):
            proc.state_manager.update_state(
                ISSUE,
                status=_STATUS[kind],
                current_opencode_session_id=None,
                metadata={
                    "workflow_type": _WF[kind],
                    "repository_url": REPO,
                    "source_branch": "develop",
                    "target_branch": TARGET,
                    "feature_branch": WORK,
                },
            )
            task = AgentTask(
                description=f"{kind} step {index}",
                prompt=f"step {index}",
                agent="build",
                issue_key=ISSUE,
                backend="opencode",
            )
            attached = proc._attach_bound_opencode_session(ISSUE, task, git)
            resumed = bool(attached)
            sid = attached
            if not sid:
                created = await client.create_session(
                    title=f"{ISSUE} {kind} step {index}"
                )
                sid = str(created.get("id") or "")
                assert sid.startswith("ses_"), created
                proc._upsert_session_bind(ISSUE, sid)
                task.session_id = sid
            assert sid and sid.startswith("ses_"), sid

            token = f"TOKEN-{kind}-{index}"
            reply = await client.send_message(
                sid,
                (
                    f"You are the {kind} chat. Do not use tools. "
                    f"Reply with exactly {token} on one line."
                ),
                model=model,
                timeout=180.0,
            )
            assert reply is not None, f"step {index} ({kind}) send_message returned None"
            steps.append((kind, sid, resumed))
            print(f"step {index}: kind={kind} sid={sid} resumed={resumed}")

        by_kind: Dict[str, List[str]] = {"plan": [], "build": [], "test": []}
        for kind, sid, _resumed in steps:
            by_kind[kind].append(sid)

        plan_sid = by_kind["plan"][0]
        build_sid = by_kind["build"][0]
        test_sid = by_kind["test"][0]
        assert len({plan_sid, build_sid, test_sid}) == 3, (
            f"kinds must be three sessions, got plan={plan_sid} "
            f"build={build_sid} test={test_sid}"
        )
        assert by_kind["plan"] == [plan_sid]
        assert by_kind["build"] == [build_sid, build_sid, build_sid], by_kind["build"]
        assert by_kind["test"] == [test_sid, test_sid, test_sid], by_kind["test"]
        assert steps[0][2] is False
        assert steps[1][2] is False
        assert steps[2][2] is False
        assert all(resumed for _kind, _sid, resumed in steps[3:]), steps

        assert store.get(REPO, WORK, TARGET, kind="plan")["session_id"] == plan_sid
        assert store.get(REPO, WORK, TARGET, kind="build")["session_id"] == build_sid
        assert store.get(REPO, WORK, TARGET, kind="test")["session_id"] == test_sid
    finally:
        if client is not None:
            await client.aclose()
        _stop_serve(serve)
