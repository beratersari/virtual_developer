"""E2E timing for a trivial build (Jira text like ``5+4`` / one-line C++ edit).

Real incident — KAN-21 scheduled ``5+4`` (2026-08-07):

    started_at    18:15:06
    clone done    18:15:09   (2.1s)
    work branch   18:15:14   (~8s git prep total)
    atlas serve   18:15:14 → 18:27:41   **747s**
    push + MR     18:27:43 → 18:27:46   (~5s)
    completed_at  18:27:46              **12m 40s wall**

Session log: ``elapsed=746.12s``. Atlas followed BUILD_PROMPT ("build + test"),
found no g++, and downloaded an unprivileged GCC 9 toolchain into ``/tmp``.
KAN-20 (``4+3``, no compiler hunt) was already **144s** of agent vs ~8s git.

These tests separate **orchestrator** time from **agent** time.

1. Hermetic (CI): real ``GitManager`` clone of a local origin + processor
   execution with an instant fake agent. Orchestrator must finish in seconds.
2. Live (opt-in): real ``opencode serve`` + Atlas + production BUILD_PROMPT
   on a tiny C++ tree. Prints a phase report. Set ``VD_LIVE_SIMPLE_TASK=1``.

Run::

    .venv/bin/python -m pytest tests/test_simple_task_timing_e2e.py -q
    VD_LIVE_SIMPLE_TASK=1 .venv/bin/python -m pytest \\
        tests/test_simple_task_timing_e2e.py -k live -v -s
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.prompt_builder import PromptBuilder
from src.processor import JobProcessor
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from tests.test_opencode_serve_live_e2e import (
    _free_port,
    _opencode_bin,
    _start_serve,
    _stop_serve,
    _wait_health,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CPP_OLD = (
    "#include <iostream>\n\nint main() {\n"
    "    std::cout << 20 + 4 << std::endl;\n    return 0;\n}\n"
)
MAIN_CPP_HINT = "single-file C++ test project; no Makefile/CMake/tests.\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_tiny_cpp_repo(path: Path, *, branch: str = "develop") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "main.cpp").write_text(MAIN_CPP_OLD, encoding="utf-8")
    (path / "README.md").write_text(MAIN_CPP_HINT, encoding="utf-8")
    _git(path, "init")
    _git(path, "config", "user.email", "devbot@example.com")
    _git(path, "config", "user.name", "DevBot")
    _git(path, "checkout", "-b", branch)
    _git(path, "add", ".")
    _git(path, "commit", "-m", "chore: seed tiny cpp project")


def _make_local_origin(root: Path) -> Path:
    src = root / "seed"
    _init_tiny_cpp_repo(src)
    bare = root / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


def _issue_description(*, repo: str, source: str, target: str, body: str) -> str:
    return (
        f"{body}\n\n"
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        "Mode: build\n"
        "{params}\n"
    )


def parse_serve_elapsed_seconds(session_log: str) -> Optional[float]:
    """Parse ``[serve] turn=initial done ... elapsed=746.12s`` from session logs."""
    m = re.search(r"elapsed=(\d+(?:\.\d+)?)s", session_log or "")
    if not m:
        return None
    return float(m.group(1))


def test_parse_serve_elapsed_matches_kan21_session_line():
    log = (
        "[serve] turn=initial done finish='stop' summary=None elapsed=746.12s\n"
        "[serve] assessment complete=True premature=False reasons=[]\n"
    )
    assert parse_serve_elapsed_seconds(log) == 746.12


@pytest.mark.asyncio
async def test_simple_task_orchestrator_is_seconds_not_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_jira, reporter
):
    """Real clone + processor build path; fake agent. Git/processor must be seconds."""
    from src.config import settings
    import src.issue_git_spec as git_spec

    origin = _make_local_origin(tmp_path / "git")
    repo_url = origin.resolve().as_uri()
    work_root = tmp_path / "run"
    work_root.mkdir()
    monkeypatch.chdir(work_root)
    monkeypatch.setattr(settings, "temp_dir_base", Path(".temp"))
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    monkeypatch.setattr(settings, "gitlab_host_pats", "")
    monkeypatch.setattr(settings, "gitlab_allowed_hosts", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    orig_url_ok = git_spec._looks_like_git_url

    def _allow_file_origin(url: str) -> bool:
        raw = (url or "").strip()
        if raw.lower().startswith("file://") and len(raw) > 8:
            return True
        return orig_url_ok(url)

    monkeypatch.setattr(git_spec, "_looks_like_git_url", _allow_file_origin)

    sm = JiraStateManager(state_dir=tmp_path / "state")
    key = "E2E-ADD-1"
    desc = _issue_description(
        repo=repo_url,
        source=f"feature/{key}",
        target="develop",
        body="5+4",
    )
    sm.create_state(key, "adfasfd", desc)

    captured: Dict[str, Any] = {}

    async def _instant_agent(self, task, **kwargs):
        captured["prompt"] = task.prompt or ""
        captured["agent"] = task.agent
        captured["issue_key"] = task.issue_key
        return {
            "task_id": task.task_id,
            "returncode": 0,
            "stdout": "one-line edit done (fake agent)",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "ses_e2e_fake",
            "progress": 100,
        }

    monkeypatch.setattr(AgentRunner, "run_agent", _instant_agent)

    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.reporter = reporter
    proc.jira_client = fake_jira
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)

    t0 = time.monotonic()
    await proc._start_execution_workflow(sm.get_state(key))
    total_s = time.monotonic() - t0

    st = sm.get_state(key)
    assert st is not None
    assert st.status in {TaskStatus.COMPLETED, TaskStatus.EXECUTING, TaskStatus.ERROR}
    # Fake agent succeeds → workflow should complete (or at least leave a job).
    assert captured.get("prompt"), "agent was never invoked"
    assert "5+4" in captured["prompt"] or "5 + 4" in captured["prompt"]
    assert "Build mode" in captured["prompt"]
    assert len(captured["prompt"]) > 4000, (
        "production BUILD_PROMPT should be multi-KB; "
        f"got {len(captured['prompt'])} chars"
    )

    report = {
        "total_seconds": round(total_s, 3),
        "prompt_chars": len(captured["prompt"]),
        "agent": captured.get("agent"),
        "issue_status": st.status.value,
    }
    print(f"\n[hermetic simple-task e2e] {json.dumps(report, indent=2)}", flush=True)

    assert total_s < 45.0, (
        f"Orchestrator path (clone + fake agent) took {total_s:.1f}s; "
        "expected well under 45s. Minutes of wall time are not git/processor."
    )


@pytest.mark.asyncio
async def test_live_simple_task_atlas_build_prompt_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Real serve + Atlas + production BUILD_PROMPT on a one-file C++ tree.

    Opt-in: ``VD_LIVE_SIMPLE_TASK=1``. Prints phase timings (the point of the test).
    Hard-rules in the prompt forbid downloading compilers (KAN-21 12min trap).
    """
    flag = (os.environ.get("VD_LIVE_SIMPLE_TASK") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        pytest.skip("Set VD_LIVE_SIMPLE_TASK=1 to run live atlas timing e2e")
    if not _opencode_bin():
        pytest.skip("opencode binary not found on PATH")

    from src.config import settings

    work = tmp_path / "workspace"
    _init_tiny_cpp_repo(work)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    serve_log = tmp_path / "opencode-serve.log"
    proc = None

    monkeypatch.setattr(settings, "opencode_run_mode", "serve")
    monkeypatch.setattr(settings, "opencode_serve_url", base)
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "default_agent", "atlas")

    prompt = PromptBuilder.build_build_prompt(
        issue_key="E2E-ADD-LIVE",
        summary="adfasfd",
        description="5+4",
        work_branch="feature/E2E-ADD-LIVE",
    )
    prompt += (
        "\n\n## Timing-e2e hard rules (do not skip the Jira edit)\n"
        "- Change `main.cpp` so it prints `5+4` (or `5 + 4`).\n"
        "- Commit on the current branch.\n"
        "- Do **not** install packages, download `.deb` toolchains, use apt, "
        "yum, docker, or sudo.\n"
        "- If `g++` is missing, skip compile/run and still commit the source edit.\n"
    )

    report: Dict[str, Any] = {
        "serve_url": base,
        "prompt_chars": len(prompt),
        "agent": "atlas",
        "model": getattr(settings, "default_model", ""),
        "phases": {},
    }

    try:
        t_serve = time.monotonic()
        proc = _start_serve(port, serve_log)
        health = await _wait_health(base, timeout=90.0)
        report["phases"]["serve_ready_seconds"] = round(time.monotonic() - t_serve, 3)
        report["serve_health"] = {
            "healthy": health.get("healthy"),
            "version": health.get("version"),
        }
        assert health.get("healthy") is True, health

        runner = AgentRunner(working_directory=work)
        task = AgentTask(
            description="E2E-ADD-LIVE: 5+4",
            prompt=prompt,
            agent="atlas",
            issue_key="E2E-ADD-LIVE",
        )

        t_agent = time.monotonic()
        result = await runner.run_agent(task)
        report["phases"]["agent_seconds"] = round(time.monotonic() - t_agent, 3)
        report["agent_returncode"] = result.get("returncode")
        report["opencode_session_id"] = result.get("opencode_session_id")
        session_file = result.get("session_file")
        if session_file and Path(session_file).is_file():
            text = Path(session_file).read_text(encoding="utf-8", errors="replace")
            report["serve_elapsed_seconds"] = parse_serve_elapsed_seconds(text)
            report["session_log_chars"] = len(text)
            report["session_snip"] = text[:400]

        cpp = (work / "main.cpp").read_text(encoding="utf-8")
        report["main_cpp_mentions_5_plus_4"] = bool(
            re.search(r"5\s*\+\s*4", cpp)
        )
        report["total_seconds"] = round(
            report["phases"].get("serve_ready_seconds", 0)
            + report["phases"].get("agent_seconds", 0),
            3,
        )
        print(
            "\n[live simple-task e2e timing]\n"
            + json.dumps(report, indent=2, default=str),
            flush=True,
        )

        assert result.get("returncode") == 0, result
        agent_s = float(report["phases"]["agent_seconds"])
        serve_ready_s = float(report["phases"]["serve_ready_seconds"])
        # Orchestrator/serve boot is not the multi-minute cost.
        assert serve_ready_s < 90.0, report
        # Agent dominates once serve is up (documents the KAN-20/21 finding).
        if agent_s >= 30.0:
            assert agent_s > serve_ready_s, report
        # Guardrail: this e2e forbids toolchain downloads; 15 min is a hang.
        assert agent_s < 900.0, (
            f"Live atlas turn ran {agent_s:.0f}s — likely hung or repeating "
            f"KAN-21 compiler download. report={report}"
        )
    finally:
        _stop_serve(proc)
        out = tmp_path / "simple-task-live-report.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"[live simple-task e2e] wrote {out}", flush=True)
