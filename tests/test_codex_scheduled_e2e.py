"""E2E: real Codex CLI via scheduled jobs, on a free Zen model.

Free model: ``muse-spark-1.2-contributor-free``
  OpenCode Zen Responses API — ``https://opencode.ai/zen/v1``
  (``hy3-free`` / ``deepseek-v4-flash-free`` are chat-only; Codex 0.149+
  dropped ``wire_api=chat``.)

These tests run the real ``codex exec`` binary. They skip when the Linux
CLI is missing or the Zen endpoint is unreachable. No fake Codex stub.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.backends.base import BACKEND_CODEX, AgentRunRequest
from src.backends.codex import CodexBackend, resolve_codex_cli
from src.dashboard.api import create_dashboard_app
from src.issue_git_spec import parse_issue_git_spec
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.orchestrator.prompt_builder import PromptBuilder
from src.processor import JobProcessor
from src.reporter.jira_reporter import JiraReporter
from src.scheduler.service import (
    build_issue_description,
    create_scheduled_job,
    dispatch_due_schedules,
    preview_existing_issue,
    schedule_existing_issue,
    wait_inflight_dispatches,
)
from src.state.job_store import JobStore
from src.state.manager import JiraStateManager
from src.state.models import TaskStatus
from src.state.schedule_store import ScheduleStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FREE_CODEX_MODEL = "muse-spark-1.2-contributor-free"
ZEN_BASE_URL = "https://opencode.ai/zen/v1"
REAL_TIMEOUT_S = 180.0


def _due(minutes_ago: int = 1) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds"
    )


def _future() -> str:
    return (datetime.now() + timedelta(hours=2)).isoformat(timespec="seconds")


def _params(
    *,
    repo: str = "https://gitlab.com/org/app.git",
    source: str = "develop",
    target: str = "develop",
    mode: str = "build",
    model: str = FREE_CODEX_MODEL,
    backend: str = "codex",
) -> str:
    model_line = f"Model: {model}\n" if model else ""
    backend_line = f"Backend: {backend}\n" if backend else ""
    return (
        "{params}\n"
        f"Repository: {repo}\n"
        f"Source branch: {source}\n"
        f"Target branch: {target}\n"
        f"Mode: {mode}\n"
        f"{model_line}"
        f"{backend_line}"
        "{params}"
    )


def _issue(
    key: str = "KAN-CX1",
    *,
    summary: str = "Codex e2e",
    description: str | None = None,
) -> dict:
    return {
        "key": key,
        "id": "1",
        "fields": {
            "summary": summary,
            "description": description if description is not None else _params(),
            "status": {"name": "To Do"},
            "issuetype": {"name": "Task"},
            "labels": [],
            "assignee": None,
        },
    }


def _jira_ok(key: str = "KAN-CX") -> MagicMock:
    client = MagicMock()
    client.create_issue.return_value = {"key": key, "id": "9"}
    client.transition_to_in_progress.return_value = True
    client.add_labels.return_value = True
    client.update_issue.return_value = True
    client.add_comment.return_value = {"id": "1", "body": "ok"}
    client.get_issue.return_value = None
    client.is_cloud = False
    client.last_error = None
    return client


def _real_codex_cli() -> Optional[str]:
    env = (os.environ.get("CODEX_CLI") or "").strip()
    candidates = [
        env,
        resolve_codex_cli(env or "codex"),
        str(Path.home() / ".local" / "bin" / "codex"),
        shutil.which("codex") or "",
    ]
    for raw in candidates:
        p = Path(raw) if raw else None
        if p is None or not p.is_file():
            continue
        if p.suffix.lower() == ".exe" and os.name != "nt":
            continue
        return str(p)
    return None


def _require_real_codex() -> str:
    cli = _real_codex_cli()
    if not cli:
        pytest.skip("Linux Codex CLI not found (install rust-v0.149.0 musl binary)")
    return cli


def _zen_reachable() -> bool:
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://opencode.ai/zen/v1/models",
            headers={"User-Agent": "yaver-codex-e2e"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return int(resp.status) == 200
    except Exception:
        return False


def _point_settings_at_real_codex(monkeypatch: pytest.MonkeyPatch, cli: str) -> None:
    from src.config import settings

    monkeypatch.setattr(settings, "codex_cli", cli)
    monkeypatch.setattr(settings, "codex_api_key", "")
    monkeypatch.setattr(settings, "codex_base_url", ZEN_BASE_URL)
    monkeypatch.setattr(settings, "codex_wire_api", "responses")
    monkeypatch.setattr(settings, "default_model", FREE_CODEX_MODEL)
    monkeypatch.setattr(settings, "agent_backend", "opencode")
    monkeypatch.setattr(settings, "agent_task_max_retries", 0)
    monkeypatch.setattr(settings, "agent_task_max_incomplete_retries", 0)
    monkeypatch.setattr(settings, "agent_task_timeout_seconds", int(REAL_TIMEOUT_S))


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _make_local_origin(root: Path) -> Path:
    src = root / "seed"
    src.mkdir(parents=True)
    (src / "README.md").write_text("codex e2e seed\n", encoding="utf-8")
    _git(src, "init")
    _git(src, "config", "user.email", "devbot@example.com")
    _git(src, "config", "user.name", "DevBot")
    _git(src, "checkout", "-b", "develop")
    _git(src, "add", ".")
    _git(src, "commit", "-m", "chore: seed")
    bare = root / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    return bare


def _allow_file_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.issue_git_spec as git_spec

    orig = git_spec._looks_like_git_url

    def _ok(url: str) -> bool:
        raw = (url or "").strip()
        if raw.lower().startswith("file://") and len(raw) > 8:
            return True
        return orig(url)

    monkeypatch.setattr(git_spec, "_looks_like_git_url", _ok)


async def _wait_issue_status(
    sm: JiraStateManager,
    key: str,
    wanted: Set[TaskStatus],
    *,
    timeout: float,
) -> Optional[Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = sm.get_state(key)
        if last is not None and last.status in wanted:
            return last
        await asyncio.sleep(0.2)
    return last


# ---------------------------------------------------------------------------
# Real Codex CLI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_codex_exec_free_zen_model(tmp_path, monkeypatch):
    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")
    _point_settings_at_real_codex(monkeypatch, cli)
    wd = tmp_path / "ws"
    wd.mkdir()
    (wd / "README.md").write_text("real codex\n", encoding="utf-8")
    result = await CodexBackend().run(
        AgentRunRequest(
            prompt=(
                "Reply with the single word pong. Do not use tools "
                "and do not edit files."
            ),
            model=FREE_CODEX_MODEL,
            working_directory=wd,
            timeout_seconds=REAL_TIMEOUT_S,
        )
    )
    blob = (result.stdout or "") + "\n" + (result.stderr or "")
    assert result.backend == BACKEND_CODEX
    assert result.timed_out is False, blob[-1500:]
    assert result.returncode == 0, blob[-1500:]
    assert "pong" in blob.lower()
    assert "opencode serve" not in blob
    assert result.session_id


@pytest.mark.asyncio
async def test_real_codex_runner_never_calls_serve(tmp_path, monkeypatch):
    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")
    _point_settings_at_real_codex(monkeypatch, cli)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jira-agent").mkdir()
    (tmp_path / "README.md").write_text("runner\n", encoding="utf-8")
    runner = AgentRunner(working_directory=tmp_path)
    serve = AsyncMock(return_value={"returncode": 99, "stderr": "serve must not run"})
    monkeypatch.setattr(runner, "_run_agent_via_serve", serve)
    task = AgentTask(
        description="Real Codex pong",
        prompt="Reply with the single word pong. Do not use tools or edit files.",
        agent="build",
        issue_key="KAN-99",
        backend="codex",
        model=FREE_CODEX_MODEL,
    )
    result = await runner.run_agent(task, timeout_seconds=REAL_TIMEOUT_S)
    serve.assert_not_called()
    assert result["backend"] == BACKEND_CODEX
    assert result["returncode"] == 0, (result.get("stderr") or "")[-1500:]
    out = (result.get("stdout") or "") + (result.get("stderr") or "")
    assert "pong" in out.lower()


@pytest.mark.asyncio
async def test_real_codex_resume_same_thread(tmp_path, monkeypatch):
    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")
    _point_settings_at_real_codex(monkeypatch, cli)
    wd = tmp_path / "ws"
    wd.mkdir()
    first = await CodexBackend().run(
        AgentRunRequest(
            prompt="Reply with the single word ping. Do not use tools or edit files.",
            model=FREE_CODEX_MODEL,
            working_directory=wd,
            timeout_seconds=REAL_TIMEOUT_S,
        )
    )
    assert first.returncode == 0, (first.stderr or first.stdout or "")[-1500:]
    assert first.session_id
    second = await CodexBackend().run(
        AgentRunRequest(
            prompt="Reply with the single word pong. Do not use tools or edit files.",
            model=FREE_CODEX_MODEL,
            session_id=first.session_id,
            working_directory=wd,
            timeout_seconds=REAL_TIMEOUT_S,
        )
    )
    assert second.returncode == 0, (second.stderr or second.stdout or "")[-1500:]
    assert second.session_id == first.session_id
    assert "pong" in ((second.stdout or "") + (second.stderr or "")).lower()


# ---------------------------------------------------------------------------
# Scheduled job create / existing / HTTP (no agent)
# ---------------------------------------------------------------------------


def test_e2e_create_scheduled_codex_job_writes_free_model(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = _jira_ok("KAN-CF")
    out = create_scheduled_job(
        title="Codex free-model build",
        description="Ship the feature",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model=FREE_CODEX_MODEL,
        backend="codex",
        scheduled_at=_future(),
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    rec = out["schedule"]
    assert rec["backend"] == BACKEND_CODEX
    assert rec["model"] == FREE_CODEX_MODEL
    desc = client.create_issue.call_args.kwargs["description"]
    assert "Backend: codex" in desc
    assert f"Model: {FREE_CODEX_MODEL}" in desc
    spec, err = parse_issue_git_spec("Codex free-model build", desc)
    assert err is None and spec is not None
    assert spec.backend == BACKEND_CODEX
    assert spec.model == FREE_CODEX_MODEL


@pytest.mark.parametrize("alias", ["codex", "openai-codex", "openai"])
def test_e2e_create_accepts_codex_backend_aliases(tmp_path, alias):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = _jira_ok("KAN-AL")
    out = create_scheduled_job(
        title="alias",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="main",
        mode="plan",
        model=FREE_CODEX_MODEL,
        backend=alias,
        scheduled_at=_future(),
        project_key="KAN",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["backend"] == BACKEND_CODEX
    assert "Backend: codex" in client.create_issue.call_args.kwargs["description"]


def test_e2e_schedule_existing_and_preview_free_model(tmp_path):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = _jira_ok()
    client.get_issue.return_value = _issue(
        "KAN-EX", description=_params(model="", backend="")
    )
    out = schedule_existing_issue(
        "KAN-EX",
        scheduled_at=_future(),
        model=FREE_CODEX_MODEL,
        backend="openai-codex",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["backend"] == BACKEND_CODEX
    assert out["schedule"]["model"] == FREE_CODEX_MODEL
    client.get_issue.return_value = _issue("KAN-PV")
    prev = preview_existing_issue("KAN-PV", jira_client=client)
    assert prev["ok"] is True
    assert prev["backend"] == BACKEND_CODEX
    assert prev["model"] == FREE_CODEX_MODEL


def test_e2e_api_create_from_issue_preview_cancel_codex(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    client = _jira_ok("KAN-API")
    client.get_issue.return_value = _issue("KAN-OLD")

    with monkeypatch.context() as m:
        m.setattr("src.dashboard.api.schedule_store", store)
        m.setattr(
            "src.dashboard.api.create_scheduled_job",
            lambda **kw: create_scheduled_job(**kw, jira_client=client, store=store),
        )
        m.setattr(
            "src.dashboard.api.schedule_existing_issue",
            lambda issue_key, scheduled_at, store=None, **kw: schedule_existing_issue(
                issue_key,
                scheduled_at=scheduled_at,
                jira_client=client,
                store=store or store,
                **kw,
            ),
        )
        m.setattr(
            "src.dashboard.api.preview_existing_issue",
            lambda issue_key: preview_existing_issue(issue_key, jira_client=client),
        )
        app = create_dashboard_app(processor=None, state_manager=sm)
        tc = TestClient(app)

        r = tc.post(
            "/api/schedules",
            json={
                "title": "API Codex",
                "repository_url": "https://gitlab.com/org/app.git",
                "source_branch": "develop",
                "target_branch": "develop",
                "mode": "build",
                "model": FREE_CODEX_MODEL,
                "backend": "codex",
                "scheduled_at": _future(),
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["schedule"]["backend"] == BACKEND_CODEX
        assert r.json()["schedule"]["model"] == FREE_CODEX_MODEL

        r2 = tc.get("/api/schedules/preview", params={"issue_key": "KAN-OLD"})
        assert r2.status_code == 200
        assert r2.json()["model"] == FREE_CODEX_MODEL

        r3 = tc.post(
            "/api/schedules/from-issue",
            json={
                "issue_key": "KAN-OLD",
                "scheduled_at": _future(),
                "model": FREE_CODEX_MODEL,
                "backend": "codex",
            },
        )
        assert r3.status_code == 200, r3.text
        sid = r3.json()["schedule"]["schedule_id"]
        r4 = tc.post(f"/api/schedules/{sid}/cancel")
        assert r4.status_code == 200
        assert r4.json()["schedule"]["status"] == "cancelled"


def test_build_issue_description_codex_free_model():
    text = build_issue_description(
        description="Do it",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model=FREE_CODEX_MODEL,
        backend="openai-codex",
    )
    assert f"Model: {FREE_CODEX_MODEL}" in text
    assert "Backend: codex" in text


# ---------------------------------------------------------------------------
# Schedule fire → real Codex (tiny prompt, real binary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_scheduled_build_real_codex_writes_file(tmp_path, monkeypatch):
    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")
    _allow_file_urls(monkeypatch)
    origin = _make_local_origin(tmp_path / "git")
    repo_url = origin.resolve().as_uri()
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    _point_settings_at_real_codex(monkeypatch, cli)

    from src.config import settings

    monkeypatch.setattr(settings, "temp_dir_base", Path(".temp"))
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "gitlab_pat", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    monkeypatch.setattr(
        PromptBuilder,
        "build_build_prompt",
        staticmethod(
            lambda **_kw: (
                "Create a file named pong.txt containing the single word pong. "
                "Git add and commit it. Do not ask questions. Do not push."
            )
        ),
    )

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    js = JobStore(jobs_dir=tmp_path / "jobs")
    jira = _jira_ok("KAN-42")
    created = create_scheduled_job(
        title="Real Codex scheduled build",
        description="Write pong.txt",
        repository_url=repo_url,
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model=FREE_CODEX_MODEL,
        backend="codex",
        scheduled_at=_due(),
        project_key="KAN",
        jira_client=jira,
        store=store,
    )
    assert created["ok"] is True
    desc = jira.create_issue.call_args.kwargs["description"]
    jira.get_issue.return_value = _issue("KAN-42", description=desc)

    serve_calls: list[int] = []

    async def _serve_must_not_run(*_a, **_k):
        serve_calls.append(1)
        return {"returncode": 99, "stderr": "opencode serve must not run"}

    monkeypatch.setattr(AgentRunner, "_run_agent_via_serve", _serve_must_not_run)

    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = js
    proc.jira_client = jira
    proc.reporter = JiraReporter(client=jira)
    proc._mark_jira_in_progress = MagicMock(return_value=True)
    proc._push_and_create_mr = AsyncMock(return_value=True)

    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=jira
    )
    await wait_inflight_dispatches()
    assert result["launched"] == 1, result
    assert store.get(created["schedule"]["schedule_id"])["status"] == "dispatched"

    state = await _wait_issue_status(
        sm,
        "KAN-42",
        {TaskStatus.COMPLETED, TaskStatus.ERROR},
        timeout=REAL_TIMEOUT_S + 30,
    )
    assert serve_calls == []
    assert state is not None
    assert state.status == TaskStatus.COMPLETED, state.error_message
    assert proc._backend_for_issue(state) == BACKEND_CODEX
    assert proc._model_for_issue(state) == FREE_CODEX_MODEL


@pytest.mark.asyncio
async def test_e2e_scheduled_plan_real_codex_reaches_plan_ready(
    tmp_path, monkeypatch
):
    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")
    _allow_file_urls(monkeypatch)
    origin = _make_local_origin(tmp_path / "git")
    repo_url = origin.resolve().as_uri()
    work = tmp_path / "run"
    work.mkdir()
    monkeypatch.chdir(work)
    _point_settings_at_real_codex(monkeypatch, cli)

    from src.config import settings

    monkeypatch.setattr(settings, "temp_dir_base", Path(".temp"))
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "sisyphus_plans_dir", Path(".sisyphus/plans"))
    monkeypatch.setattr(settings, "gitlab_pat", "")
    if hasattr(settings, "set_gitlab_host_pat_map"):
        settings.set_gitlab_host_pat_map({})

    monkeypatch.setattr(
        PromptBuilder,
        "build_plan_prompt",
        staticmethod(
            lambda **kw: (
                "Write a short markdown plan to "
                f".sisyphus/plans/{kw.get('issue_key') or 'KAN-88'}.md "
                "with a heading and two bullets. Do not ask questions."
            )
        ),
    )

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    js = JobStore(jobs_dir=tmp_path / "jobs")
    jira = _jira_ok("KAN-88")
    created = create_scheduled_job(
        title="Real Codex scheduled plan",
        description="Write a plan file",
        repository_url=repo_url,
        source_branch="develop",
        target_branch="develop",
        mode="plan",
        model=FREE_CODEX_MODEL,
        backend="codex",
        scheduled_at=_due(),
        project_key="KAN",
        jira_client=jira,
        store=store,
    )
    desc = jira.create_issue.call_args.kwargs["description"]
    jira.get_issue.return_value = _issue("KAN-88", description=desc)

    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = sm
    proc.job_store = js
    proc.jira_client = jira
    proc.reporter = JiraReporter(client=jira)
    proc._mark_jira_in_progress = MagicMock(return_value=True)

    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=jira
    )
    await wait_inflight_dispatches()
    assert result["launched"] == 1, result
    state = await _wait_issue_status(
        sm,
        "KAN-88",
        {TaskStatus.PLAN_READY, TaskStatus.ERROR},
        timeout=REAL_TIMEOUT_S + 30,
    )
    assert state is not None
    assert state.status == TaskStatus.PLAN_READY, state.error_message


@pytest.mark.asyncio
async def test_e2e_dispatch_inflight_is_not_false_success(tmp_path, monkeypatch):
    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    monkeypatch.chdir(tmp_path)
    jira = _jira_ok("KAN-LIVE")
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    proc.state_manager = JiraStateManager(state_dir=tmp_path / "state")
    proc.reporter = JiraReporter(client=jira)
    sm = proc.state_manager
    sm.create_state("KAN-LIVE", "busy", _params())
    sm.update_state("KAN-LIVE", status=TaskStatus.EXECUTING)
    rec = store.create(
        title="busy",
        description="",
        repository_url="https://gitlab.com/org/app.git",
        source_branch="develop",
        target_branch="develop",
        mode="build",
        model=FREE_CODEX_MODEL,
        backend="codex",
        scheduled_at=_due(),
        issue_key="KAN-LIVE",
        issue_description=_params(),
        source="existing",
    )
    proc._start_execution_workflow = AsyncMock()
    proc._start_planning_workflow = AsyncMock()
    result = await dispatch_due_schedules(
        processor=proc, store=store, jira_client=None
    )
    await wait_inflight_dispatches()
    assert result["launched"] == 1
    refreshed = store.get(rec["schedule_id"])
    assert refreshed["status"] == "error"
    assert "in progress" in (refreshed.get("error_message") or "").lower()
    proc._start_execution_workflow.assert_not_awaited()


# ---------------------------------------------------------------------------
# Live Jira: create a real ticket, then fire Codex in full-auto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_jira_ticket_codex_auto_plan(tmp_path, monkeypatch):
    """Create a real Cloud Jira issue and run unattended Codex (no Q&A).

    Opt-in: ``VD_LIVE_JIRA_CODEX=1``.
    """
    flag = (os.environ.get("VD_LIVE_JIRA_CODEX") or "").strip().lower()
    if flag not in {"1", "true", "yes"}:
        pytest.skip("Set VD_LIVE_JIRA_CODEX=1 to create a real Jira ticket")

    cli = _require_real_codex()
    if not _zen_reachable():
        pytest.skip("OpenCode Zen endpoint not reachable")

    from src.config import settings
    from src.jira.client import create_jira_client

    if not (settings.jira_host or "").strip() or not (settings.jira_api_token or "").strip():
        pytest.skip("Jira is not configured")

    _point_settings_at_real_codex(monkeypatch, cli)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "temp_dir_base", Path(".temp"))
    monkeypatch.setattr(settings, "agent_prompts_dir", REPO_ROOT / "agent")
    monkeypatch.setattr(settings, "sisyphus_plans_dir", Path(".sisyphus/plans"))
    monkeypatch.setattr(settings, "agent_backend", "codex")

    monkeypatch.setattr(
        PromptBuilder,
        "build_plan_prompt",
        staticmethod(
            lambda **kw: (
                "UNATTENDED JOB: do not ask questions. "
                "Write a short markdown plan to "
                f".sisyphus/plans/{kw.get('issue_key') or 'ISSUE'}.md "
                "with a heading and two bullets, then stop."
            )
        ),
    )

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    sm = JiraStateManager(state_dir=tmp_path / "state")
    js = JobStore(jobs_dir=tmp_path / "jobs")
    client = create_jira_client()
    try:
        created = create_scheduled_job(
            title="[e2e] Codex auto-plan (no Q&A)",
            description=(
                "Automated Codex e2e. Write a two-bullet plan. "
                "Do not ask questions."
            ),
            repository_url="https://github.com/octocat/Hello-World.git",
            source_branch="master",
            target_branch="master",
            mode="plan",
            model=FREE_CODEX_MODEL,
            backend="codex",
            scheduled_at=_due(),
            project_key=(settings.jira_projects_list or ["KAN"])[0],
            jira_client=client,
            store=store,
        )
        assert created["ok"] is True, created.get("error")
        key = created["issue_key"]
        assert key
        print(f"\n[live jira] created {key}", flush=True)

        live = client.get_issue(key)
        assert live is not None
        desc = (live.get("fields") or {}).get("description") or ""
        assert "Backend: codex" in desc
        assert f"Model: {FREE_CODEX_MODEL}" in desc

        with patch("src.processor.create_jira_client", return_value=client):
            proc = JobProcessor()
        proc.state_manager = sm
        proc.job_store = js
        proc.jira_client = client
        proc.reporter = JiraReporter(client=client)

        result = await dispatch_due_schedules(
            processor=proc, store=store, jira_client=client
        )
        await wait_inflight_dispatches()
        assert result["launched"] == 1, result

        state = await _wait_issue_status(
            sm,
            key,
            {TaskStatus.PLAN_READY, TaskStatus.ERROR},
            timeout=REAL_TIMEOUT_S + 60,
        )
        assert state is not None
        assert state.status == TaskStatus.PLAN_READY, state.error_message
        assert proc._backend_for_issue(state) == BACKEND_CODEX
    finally:
        try:
            client.close()
        except Exception:
            pass
