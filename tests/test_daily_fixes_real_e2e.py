"""Real end-to-end checks for the daily-usage fixes.

These tests drive git, HTTP, and agent subprocesses. They do not replace
the source-contract checks in ``tests/test_daily_surface_fixes.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_SESSION = "11111111-1111-4111-8111-111111111111"
_CODEX_CS = """
using System;
public class CodexE2E {
    public static int Main(string[] args) {
        string joined = string.Join(" ", args);
        string text = joined.IndexOf("E2E_ASK", StringComparison.Ordinal) >= 0
            ? "Which database should I use?"
            : "Implemented the login form.";
        Console.WriteLine("{\\"type\\":\\"item.completed\\",\\"item\\":{\\"type\\":\\"agent_message\\",\\"text\\":\\"" + text + "\\"}}");
        return 0;
    }
}
"""
_HEALTH_SCRIPT = """
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log_path = sys.argv[2]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(self.path + "\\n")
        except OSError:
            pass
        body = b'{"status":"healthy"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def _git_exe() -> str:
    found = shutil.which("git")
    if found:
        return found
    candidate = Path(
        r"C:\Users\BERAT\AppData\Local\grok\git\2.55.0.windows.5\cmd\git.exe"
    )
    if candidate.is_file():
        return str(candidate)
    pytest.fail("git is not on PATH")


def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("timeout", 30)
    if os.name == "nt":
        kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _params(mode: str = "") -> str:
    lines = [
        "{params}",
        "Repository: https://gitlab.example.com/acme/api.git",
        "Source branch: feature/E2E-1",
        "Target branch: develop",
    ]
    if mode:
        lines.append(mode)
    lines.append("{params}")
    return "\n".join(lines)


def test_wiki_bold_mode_routes_to_plan_and_omitted_mode_routes_to_build() -> None:
    from src.orchestrator.workflow_router import WorkflowRouter, WorkflowType
    from src.work_modes import lookup

    plan_text = _params("*Mode:* plan")
    plan = WorkflowRouter.route_issue("E2E-1", "Login", plan_text)
    assert plan == WorkflowType.PLANNING
    assert WorkflowRouter.agent_for_issue("Login", plan_text, plan) == lookup("plan")["agent"]

    build_text = _params()
    build = WorkflowRouter.route_issue("E2E-1", "Login", build_text)
    assert build == WorkflowType.EXECUTION
    assert (
        WorkflowRouter.agent_for_issue("Login", build_text, build)
        == lookup("build")["agent"]
    )

    assert (
        WorkflowRouter.route_issue("E2E-1", "Login", "Add a login form.")
        == WorkflowType.PLANNING
    )


def _store_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: int) -> None:
    from src.config import apply_runtime_settings_to, save_runtime_settings, settings

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    if "AGENT_TASK_TIMEOUT_SECONDS" in os.environ:
        monkeypatch.setenv(
            "AGENT_TASK_TIMEOUT_SECONDS", os.environ["AGENT_TASK_TIMEOUT_SECONDS"]
        )
    else:
        monkeypatch.delenv("AGENT_TASK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(settings, "agent_task_max_retries", 0, raising=False)
    monkeypatch.setattr(
        settings, "agent_task_max_incomplete_retries", 0, raising=False
    )
    save_runtime_settings({"agent_task_timeout_seconds": value})
    apply_runtime_settings_to(settings)


def _begin_run(state_manager: Any, job_store: Any, issue_key: str) -> Any:
    from src.orchestrator.agent_runner import AgentTask
    from src.processor import JobProcessor
    from src.state.models import TaskStatus

    state = state_manager.create_state(issue_key, "timeout budget", "")
    assert state is not None
    proc = object.__new__(JobProcessor)
    proc.state_manager = state_manager
    proc.job_store = job_store
    proc._active_jobs = {}
    fails: List[str] = []
    proc._fail_issue = lambda *args, **kwargs: fails.append(repr(args))
    job_id = proc._begin_workflow_run(
        state,
        status=TaskStatus.EXECUTING,
        task=AgentTask(
            description="timeout budget",
            prompt="implement the ticket",
            agent="derman-build",
        ),
        workflow_type="execution",
        agent="derman-build",
        job_status="running",
    )
    assert job_id, fails
    live = state_manager.get_state(issue_key)
    assert live is not None
    return live


def _not_stuck(age: float, limit: float) -> bool:
    """Same comparison as the daemon watchdog: age <= limit stays in flight."""
    return age <= limit


def test_stored_timeout_zero_is_the_default_budget_on_a_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_manager: Any,
    isolate_jira_agent_artifacts: Dict[str, Any],
) -> None:
    from src.config import compute_stuck_limit_seconds, live_agent_timeout_seconds, settings

    _store_timeout(monkeypatch, tmp_path, 0)
    assert settings.agent_task_timeout_seconds == 0
    assert live_agent_timeout_seconds() == 1800
    live = _begin_run(
        state_manager, isolate_jira_agent_artifacts["job_store"], "E2E-TIMEOUT-0"
    )
    assert live.timeout_seconds == 1800
    limit = compute_stuck_limit_seconds(
        live.timeout_seconds,
        live.max_retries,
        extra_attempts=int((live.metadata or {}).get("max_incomplete_retries") or 0),
    )
    assert limit == 2700
    assert _not_stuck(30, limit)


def test_stored_timeout_under_thirty_seconds_stays_on_a_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_manager: Any,
    isolate_jira_agent_artifacts: Dict[str, Any],
) -> None:
    from src.config import compute_stuck_limit_seconds, live_agent_timeout_seconds

    _store_timeout(monkeypatch, tmp_path, 15)
    assert live_agent_timeout_seconds() == 15
    live = _begin_run(
        state_manager, isolate_jira_agent_artifacts["job_store"], "E2E-TIMEOUT-15"
    )
    assert live.timeout_seconds == 15
    limit = compute_stuck_limit_seconds(
        live.timeout_seconds,
        live.max_retries,
        extra_attempts=int((live.metadata or {}).get("max_incomplete_retries") or 0),
    )
    assert limit == 22.5
    assert _not_stuck(10, limit)
    assert not _not_stuck(30, limit)


def _processor(state_manager: Any, queue_store: Any) -> Any:
    from src.processor import JobProcessor

    proc = object.__new__(JobProcessor)
    proc.state_manager = state_manager
    proc.queue_store = queue_store
    proc._shutting_down = False
    proc._queue_dispatch_lock = None
    proc._queue_dispatch_again = False
    proc._contexts = {}
    proc._workspace_lock_holders = set()
    return proc


async def _watch_until(supervisor: Any, ready, timeout: float = 20) -> None:
    running = {"go": True}
    task = asyncio.create_task(supervisor.watch(lambda: running["go"]))
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("reload watch did not reach the expected state")
    finally:
        running["go"] = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        supervisor.stop_owned()


def _kill_pids(pids: List[int]) -> None:
    for pid in pids:
        if pid:
            _run(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=15)


def test_idle_reload_wakes_the_real_queue(
    tmp_path: Path,
    state_manager: Any,
    isolate_jira_agent_artifacts: Dict[str, Any],
) -> None:
    from src.opencode_serve_supervisor import OpenCodeServeSupervisor

    port = _free_port()
    script = tmp_path / "health.py"
    log = tmp_path / "health-hits.txt"
    script.write_text(_HEALTH_SCRIPT, encoding="utf-8")
    procs: List[subprocess.Popen] = []

    def healthy() -> bool:
        try:
            with _opener().open(
                f"http://127.0.0.1:{port}/global/health", timeout=1
            ) as response:
                return response.status < 500 and b"healthy" in response.read()
        except Exception:
            return False

    def spawn() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, str(script), str(port), str(log)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
        procs.append(proc)
        return proc

    def kill(pid: int) -> None:
        _kill_pids([pid])

    jobs = ["KAN-1"]
    supervisor = OpenCodeServeSupervisor(
        healthy=healthy,
        spawn=spawn,
        kill=kill,
        listener_pids=lambda _port: [],
        live_jobs=lambda: list(jobs),
        interval=0.05,
        reload_marker=tmp_path / "reload.marker",
    )
    queue = isolate_jira_agent_artifacts["queue_store"]
    row = queue.enqueue(source="jira", issue_key="E2E-QUEUE", summary="waiting")
    seen: List[str] = []
    proc = _processor(state_manager, queue)

    async def run_item(item: Dict[str, Any]) -> None:
        seen.append(str(item.get("queue_id") or ""))

    proc._run_queue_item = run_item

    def kick() -> None:
        asyncio.get_running_loop().create_task(proc.dispatch_queue())

    supervisor.set_after_idle_reload(kick)
    try:
        deferred = supervisor.request_reload()
        assert deferred["status"] == "deferred"
        assert procs == []
        jobs.clear()

        async def drive() -> None:
            await _watch_until(
                supervisor,
                lambda: bool(seen) and "/global/health" in log.read_text(encoding="utf-8")
                if log.is_file()
                else False,
            )

        asyncio.run(drive())
        stored = queue.get(row["queue_id"])
        assert stored is not None
        assert stored["status"] == "running"
        assert seen == [row["queue_id"]]
    finally:
        supervisor.stop_owned()
        _kill_pids([proc.pid for proc in procs if proc.poll() is None])


def test_failed_reload_does_not_wake_the_queue(
    tmp_path: Path,
    state_manager: Any,
    isolate_jira_agent_artifacts: Dict[str, Any],
) -> None:
    from src.opencode_serve_supervisor import OpenCodeServeSupervisor

    clock = {"now": 0.0}
    procs: List[subprocess.Popen] = []

    def spawn() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(1)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW if os.name == "nt" else 0,
        )
        procs.append(proc)
        return proc

    jobs = ["KAN-1"]
    kicks: List[str] = []
    supervisor = OpenCodeServeSupervisor(
        healthy=lambda: False,
        spawn=spawn,
        kill=lambda pid: _kill_pids([pid]),
        listener_pids=lambda _port: [],
        live_jobs=lambda: list(jobs),
        sleep=lambda delay: clock.__setitem__("now", clock["now"] + delay),
        monotonic=lambda: clock["now"],
        interval=0.05,
        reload_marker=tmp_path / "reload.marker",
    )
    queue = isolate_jira_agent_artifacts["queue_store"]
    row = queue.enqueue(source="jira", issue_key="E2E-FAIL", summary="stay queued")
    supervisor.set_after_idle_reload(lambda: kicks.append("kick"))
    started = time.perf_counter()
    try:
        assert supervisor.request_reload()["status"] == "deferred"
        jobs.clear()

        async def drive() -> None:
            await _watch_until(
                supervisor,
                lambda: str(supervisor.status().get("status") or "") == "failed",
                timeout=15,
            )

        asyncio.run(drive())
        assert time.perf_counter() - started < 10
        assert kicks == []
        stored = queue.get(row["queue_id"])
        assert stored is not None
        assert stored["status"] == "queued"
        assert procs, "failed reload never started a process"
    finally:
        supervisor.stop_owned()
        _kill_pids([proc.pid for proc in procs if proc.poll() is None])


def _write_fake_claude(directory: Path) -> Tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fake_claude.py"
    seen = directory / "prompts.txt"
    script.write_text(
        "\n".join(
            [
                "import json, sys",
                f"SESSION = {json.dumps(_SESSION)}",
                f"seen = open({json.dumps(str(seen))}, 'w', encoding='utf-8')",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': SESSION}), flush=True)",
                "turn = 0",
                "for line in sys.stdin:",
                "    line = line.strip()",
                "    if not line:",
                "        continue",
                "    turn += 1",
                "    text = ''",
                "    try:",
                "        msg = json.loads(line).get('message') or {}",
                "        content = msg.get('content') or []",
                "        if content:",
                "            text = content[0].get('text') or ''",
                "    except Exception:",
                "        text = line",
                "    seen.write(text + '\\n---\\n')",
                "    seen.flush()",
                "    reply = 'Which database should I use?' if turn == 1 else 'Review or tests written.'",
                "    print(json.dumps({",
                "        'type': 'result', 'subtype': 'success', 'is_error': False,",
                "        'session_id': SESSION, 'result': reply,",
                "    }), flush=True)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    if sys.platform == "win32":
        launcher = directory / "claude.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
        return launcher, seen
    launcher = directory / "claude"
    launcher.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher, seen


async def _claude_nudge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str) -> str:
    from src.backends.base import AgentRunRequest
    from src.backends.claude import ClaudeBackend
    from src.config import settings

    cli, seen = _write_fake_claude(tmp_path)
    monkeypatch.setattr(settings, "claude_cli", str(cli), raising=False)
    result = await ClaudeBackend().run(
        AgentRunRequest(
            prompt=f"work for {agent}",
            agent=agent,
            working_directory=tmp_path,
            timeout_seconds=30,
        )
    )
    assert result.returncode == 0, result.stderr
    assert result.extra.get("unattended_nudge") is True
    return seen.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_claude_review_and_test_nudges_come_from_the_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.backends.claude import DEFAULT_CLAUDE_RESUME_PROMPT

    review = await _claude_nudge(tmp_path / "review", monkeypatch, "derman-reviewer")
    test = await _claude_nudge(tmp_path / "test", monkeypatch, "derman-test")
    assert "Finish the review only" in review
    assert "Do **not** implement" in review
    assert "Finish all remaining work" not in review
    assert "Finish the unit tests only" in test
    assert "Finish all remaining work" not in test
    assert "continue the work already started" in DEFAULT_CLAUDE_RESUME_PROMPT
    assert DEFAULT_CLAUDE_RESUME_PROMPT not in review
    assert DEFAULT_CLAUDE_RESUME_PROMPT not in test


def _compile_fake_codex(directory: Path) -> Path:
    source = directory / "CodexE2E.cs"
    output = directory / "codex-e2e.exe"
    source.write_text(_CODEX_CS, encoding="utf-8")
    candidates = [
        Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
        Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe"),
    ]
    compiler = next((path for path in candidates if path.is_file()), None)
    if compiler is None:
        pytest.fail("csc.exe is not available to build the Codex stand-in")
    proc = _run(
        [str(compiler), "/nologo", "/t:exe", f"/out:{output}", str(source)],
        timeout=60,
    )
    if proc.returncode != 0 or not output.is_file():
        pytest.fail((proc.stdout or "") + (proc.stderr or ""))
    return output


async def _run_fake_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prompt: str
) -> Any:
    from src.backends.base import AgentRunRequest
    from src.backends.codex import CodexBackend
    from src.config import settings

    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "yaver"))
    monkeypatch.setattr(
        settings, "codex_cli", str(_compile_fake_codex(tmp_path)), raising=False
    )
    return await CodexBackend().run(
        AgentRunRequest(
            prompt=prompt,
            agent="derman-build",
            working_directory=tmp_path,
            timeout_seconds=20,
        )
    )


@pytest.mark.asyncio
async def test_codex_exit_zero_question_is_incomplete_on_a_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run_fake_codex(tmp_path, monkeypatch, "E2E_ASK the login store")
    assert result.returncode == 2
    assert result.incomplete is True
    assert result.incomplete_reasons == ["assistant asked a clarifying question"]
    assert result.progress == 50
    assert result.extra.get("assistant_asked_question") is True


@pytest.mark.asyncio
async def test_codex_exit_zero_answer_stays_success_on_a_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _run_fake_codex(tmp_path, monkeypatch, "ship the login form")
    assert result.returncode == 0
    assert result.incomplete is False
    assert result.progress == 100
    assert "Implemented the login form." in (result.stdout or "")


def _git(cwd: Path, *args: str) -> None:
    git = _git_exe()
    proc = _run(
        [
            git,
            "-c",
            "user.email=e2e@example.com",
            "-c",
            "user.name=E2E",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd),
    )
    if proc.returncode != 0:
        pytest.fail((proc.stdout or "") + (proc.stderr or ""))


def _commit_repo(path: Path, filename: str, body: str, branch: Optional[str] = None) -> None:
    path.mkdir()
    _git(path, "init", "-b", "develop")
    target = path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    if branch:
        _git(path, "checkout", "-b", branch)
        extra = path / "src" / "api_only.py"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("api only marker\n", encoding="utf-8")
        _git(path, "add", ".")
        _git(path, "commit", "-m", "api change")


def test_review_merge_base_uses_the_matching_clone(
    tmp_path: Path, state_manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.gitlab.webhook import GitlabMrNoteEvent
    from src.processor import JobProcessor
    from src.review.gitdiff import merge_base, unified_diff

    web = tmp_path / "web"
    api = tmp_path / "api"
    _commit_repo(web, "src/web_only.py", "web only\n")
    _commit_repo(api, "src/readme.py", "readme\n", branch="feature/E2E-1")
    parent = tmp_path / "workspace"
    parent.mkdir()
    assert merge_base(parent, "develop") == ""
    web_base = merge_base(web, "develop")
    api_base = merge_base(api, "develop")
    assert web_base
    assert api_base
    assert "api_only.py" not in unified_diff(web, web_base)
    assert "api_only.py" in unified_diff(api, api_base)

    class Clone:
        def __init__(self, remote: str, folder: Path) -> None:
            self.remote_url = remote
            self.temp_dir = folder

    web_remote = "https://gitlab.example.com/acme/web.git"
    api_remote = "https://gitlab.example.com/acme/api.git"
    api_mr = "https://gitlab.example.com/acme/api/-/merge_requests/4"
    state = state_manager.create_state("E2E-1", "review", "")
    assert state is not None
    state_manager.update_state(
        "E2E-1",
        metadata={
            "merge_request_url": api_mr,
            "repository_url": api_remote,
            "target_branch": "develop",
        },
    )
    proc = object.__new__(JobProcessor)
    proc.state_manager = state_manager
    proc._contexts = {
        "E2E-1": {
            "git": type("Git", (), {})(),
        }
    }
    proc._contexts["E2E-1"]["git"].repo_checkouts = [
        Clone(web_remote, web),
        Clone(api_remote, api),
    ]
    proc._post_gitlab_mr_reply = lambda *args, **kwargs: None
    proc._finish_gitlab_reviewer = lambda *args, **kwargs: None

    bases: List[Path] = []
    diffs: List[str] = []
    import src.review.gitdiff as gitdiff

    def record_base(workdir: Path, target: str) -> str:
        bases.append(Path(workdir).resolve())
        return gitdiff.merge_base(workdir, target)

    def record_diff(workdir: Path, base: str, timeout: float = 60.0) -> str:
        text = gitdiff.unified_diff(workdir, base, timeout=timeout)
        diffs.append(text)
        return text

    monkeypatch.setattr("src.review.post.merge_base", record_base)
    monkeypatch.setattr("src.review.post.unified_diff", record_diff)

    event = GitlabMrNoteEvent(
        issue_key="E2E-1",
        note_id="1",
        note_body="@bot /review",
        prompt="review",
        author_username="dev",
        author_name="Dev",
        project_id=1,
        project_path="acme/api",
        repository_url=api_remote,
        host="gitlab.example.com",
        mr_iid=4,
        mr_title="feat",
        mr_description="",
        source_branch="feature/E2E-1",
        target_branch="develop",
        mr_url=api_mr,
        command="review",
    )
    answer = "\n".join(
        [
            "### Major",
            "#### 1. `src/api_only.py:1` — missing guard",
            "",
            "**Why it is an issue and where**",
            "The new file has no guard.",
        ]
    )
    proc._deliver_review_comment(state, event, answer, azure=False)
    assert bases == [api.resolve()]
    assert any("api_only.py" in text for text in diffs)
    # Web is children[0]. An address that matches neither clone must not
    # fall back to that folder or to the parent.
    other_mr = "https://gitlab.example.com/acme/other/-/merge_requests/9"
    other_remote = "https://gitlab.example.com/acme/other.git"
    other = dataclasses.replace(
        event,
        project_path="acme/other",
        repository_url=other_remote,
        mr_iid=9,
        mr_url=other_mr,
    )
    assert (
        proc._review_workdir(
            "E2E-1",
            other,
            {"merge_request_url": other_mr, "repository_url": other_remote},
        )
        is None
    )
    event.command = "ask"
    before = len(bases)
    proc._deliver_review_comment(state, event, answer, azure=False)
    assert len(bases) == before
