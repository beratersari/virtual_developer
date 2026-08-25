"""Regression tests for critical security + lifecycle fixes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.git_manager import GitCloneError, GitManager
from src.orchestrator.agent_runner import AgentRunner, AgentTask
from src.processor import JobProcessor, _JobSlotLimiter
from src.state.models import RetryAttempt, TaskStatus
from tests.conftest import FakeJiraClient


@pytest.fixture
def processor(state_manager, reporter, fake_jira, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("src.processor.create_jira_client", return_value=fake_jira):
        proc = JobProcessor()
    proc.state_manager = state_manager
    proc.reporter = reporter
    proc.jira_client = fake_jira
    return proc


# ---------------------------------------------------------------------------
# 1–2 PAT host allowlist + no PAT in clone argv
# ---------------------------------------------------------------------------


def test_assert_remote_host_allowed_blocks_attacker(monkeypatch):
    monkeypatch.setattr(
        "src.git_manager.settings.gitlab_pat", "super-secret-pat"
    )
    monkeypatch.setattr(
        "src.git_manager.settings.gitlab_allowed_hosts", "gitlab.company.com"
    )
    # Property on settings object — patch the list property via hosts string
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_pat", "super-secret-pat")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.company.com")

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-1")
    gm.remote_url = "http://attacker.example:8443/any/repo.git"
    with pytest.raises(GitCloneError) as ei:
        gm._assert_remote_host_allowed(gm.remote_url)
    assert "attacker.example" in str(ei.value).lower() or "refused" in str(ei.value).lower()


def test_assert_remote_host_uses_legacy_pat_without_allowlist(monkeypatch):
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_pat", "pat")
    monkeypatch.setattr(real_settings, "gitlab_host_pats", "")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "")
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-2")
    gm._assert_remote_host_allowed("https://gitlab.company.com/g/r.git")
    assert gm._pat_for_remote("https://gitlab.company.com/g/r.git") == "pat"


def test_clone_uses_settings_pat_in_url_then_scrubs(tmp_path, monkeypatch):
    """Clone uses oauth2:PAT@ URL when settings PAT exists; never clears helpers."""
    from src.config import settings as real_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_settings, "gitlab_pat", "super-secret-pat-xyz")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map(
            {"gitlab.example.com": "super-secret-pat-xyz"}
        )
    monkeypatch.setattr(real_settings, "temp_dir_base", Path(".temp"))

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-3")
    gm.remote_url = "https://gitlab.example.com/group/repo.git"
    gm.temp_dir = tmp_path / "clone"
    gm.temp_dir.mkdir()

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env") or {}
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("src.git_manager.subprocess.run", side_effect=fake_run):
        with patch.object(gm, "_update_submodules"):
            with patch.object(gm, "_materialize_job_remote_refs"):
                with patch.object(gm, "_scrub_remote_credentials") as scrub:
                    gm._clone_into_temp()
                    scrub.assert_called()

    cmd = captured["cmd"]
    assert cmd[0] == "git"
    assert "clone" in cmd
    i = cmd.index("clone")
    assert cmd[i : i + 2] == ["clone", "--no-single-branch"]
    clone_url = cmd[i + 2]
    # PAT must not appear in argv (insteadOf + askpass in env)
    assert "oauth2:super-secret-pat-xyz@" not in clone_url
    env = captured.get("env") or {}
    assert clone_url == "https://gitlab.example.com/group/repo.git"
    rewrite = [
        env.get(k)
        for k in env
        if str(k).startswith("GIT_CONFIG_KEY_")
        and "insteadOf" in str(env.get(k) or "")
    ]
    assert any("oauth2:super-secret-pat-xyz@" in str(v) for v in rewrite), env
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GCM_INTERACTIVE") == "never"
    assert env.get("GIT_ASKPASS")
    assert env.get("VD_GIT_PASSWORD") == "super-secret-pat-xyz"
    assert "credential.helper=" in cmd


def test_push_applies_settings_pat_to_origin_without_clearing_helpers(
    tmp_path, monkeypatch
):
    """Push with settings PAT sets origin URL temporarily; helper GUI disabled."""
    from src.config import settings as real_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_settings, "gitlab_pat", "settings-pat-from-dashboard")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "gitlab.example.com")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map(
            {"gitlab.example.com": "settings-pat-from-dashboard"}
        )

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-PUSH")
    gm.remote_enabled = True
    gm.remote_url = "https://gitlab.example.com/group/repo.git"
    gm.temp_dir = tmp_path / "repo"
    gm.temp_dir.mkdir()
    gm.work_branch = "feature/SEC-PUSH"
    gm._assert_remote_host_allowed = MagicMock()

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("src.git_manager.subprocess.run", side_effect=fake_run):
        with patch.object(gm, "_with_auth_remote"):
            ok = gm.push("feature/SEC-PUSH")

    assert ok is True
    # set-url with oauth2:PAT before push
    set_urls = [c for c in captured if c[:3] == ["git", "remote", "set-url"]]
    assert any("oauth2:settings-pat-from-dashboard@" in " ".join(c) for c in set_urls)
    # Unattended: empty helper on argv so Windows GCM cannot pop a dialog
    push_cmds = [c for c in captured if "push" in c]
    assert push_cmds
    assert any("credential.helper=" in c for c in push_cmds)


def test_push_without_settings_pat_leaves_helpers_available(tmp_path, monkeypatch):
    """No settings PAT → push still runs (Windows/host helpers may authenticate)."""
    from src.config import settings as real_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(real_settings, "gitlab_pat", "")
    monkeypatch.setattr(real_settings, "gitlab_host_pats", "")
    monkeypatch.setattr(real_settings, "gitlab_allowed_hosts", "")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map({})

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-NOPAT")
    gm.remote_enabled = True
    gm.remote_url = "https://gitlab.example.com/group/repo.git"
    gm.temp_dir = tmp_path / "repo"
    gm.temp_dir.mkdir()
    gm.work_branch = "feature/x"
    gm._assert_remote_host_allowed = MagicMock()

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("src.git_manager.subprocess.run", side_effect=fake_run):
        with patch.object(gm, "_with_auth_remote"):
            with patch.object(gm, "_scrub_remote_credentials"):
                ok = gm.push("feature/x")

    assert ok is True
    assert any("push" in c for c in captured)
    # No oauth2 embed when no settings PAT
    assert not any("oauth2:" in " ".join(c) for c in captured)


def test_https_url_with_settings_pat_builds_oauth2_url(tmp_path, monkeypatch):
    from src.config import settings as real_settings

    monkeypatch.setattr(real_settings, "gitlab_pat", "glpat-abc")
    if hasattr(real_settings, "set_gitlab_host_pat_map"):
        real_settings.set_gitlab_host_pat_map({"gitlab.example.com": "glpat-abc"})

    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-URL")
    gm.remote_url = "https://gitlab.example.com/g/r.git"
    out = gm._https_url_with_settings_pat()
    assert out == "https://oauth2:glpat-abc@gitlab.example.com/g/r.git"


def test_build_clone_url_does_not_embed_pat():
    with patch.object(GitManager, "_setup_temp_working_dir"):
        gm = GitManager(issue_key="SEC-4")
    assert gm._build_clone_url("https://h/r.git", "pat") == "https://h/r.git"
    assert gm._build_clone_url("http://h/r.git", "pat") == "http://h/r.git"


# ---------------------------------------------------------------------------
# 3 cancel / retry abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_stops_when_aborted_during_backoff(monkeypatch):
    runner = AgentRunner(working_directory=Path("."))
    calls = {"n": 0}
    aborted = {"v": False}

    async def fail_once(*a, **k):
        calls["n"] += 1
        return {
            "task_id": "t",
            "returncode": 1,
            "stdout": "",
            "stderr": "err",
            "session_file": None,
            "opencode_session_id": None,
            "timed_out": False,
        }

    with patch.object(runner, "run_agent", side_effect=fail_once):
        with patch("src.orchestrator.agent_runner.settings") as s:
            s.agent_task_max_retries = 3
            s.agent_task_retry_delay_seconds = 0.05
            s.agent_task_retry_backoff_multiplier = 1.0
            s.agent_task_retry_on_timeout = True
            s.agent_task_retry_on_error = True
            task = AgentTask(description="d", prompt="p", agent="a")

            def on_retry(*args):
                aborted["v"] = True

            result = await runner.run_agent_with_retry(
                task,
                max_retries=3,
                on_retry=on_retry,
                should_abort=lambda: aborted["v"],
            )

    assert result.get("aborted") is True
    assert calls["n"] == 1  # no second attempt after abort


@pytest.mark.asyncio
async def test_on_retry_does_not_clobber_cancelled(
    processor, state_manager, tmp_path
):
    state = state_manager.create_state("CX-R1", "s", "d")
    state_manager.update_state("CX-R1", status=TaskStatus.EXECUTING)
    # Simulate cancel winning the race before record_retry
    state_manager.update_state("CX-R1", status=TaskStatus.CANCELLED)

    processor._record_agent_retry(
        "CX-R1",
        attempt_number=1,
        delay_seconds=0.1,
        reason="error",
        new_task_id="task_new",
        session_id="ses_x",
    )
    loaded = state_manager.get_state("CX-R1")
    assert loaded.status == TaskStatus.CANCELLED
    # should not revive executing or overwrite with stale full set_state
    assert loaded.current_task_id is None or loaded.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_planning_cancel_during_agent_stays_cancelled(
    processor, state_manager, tmp_path
):
    """Cancel mid-plan (no GitLab push in plan mode) must not flip to PLAN_READY."""
    state = state_manager.create_state("PL-C1", "plan something", "d")
    git = MagicMock()
    git.ensure_feature_branch.return_value = "feature/PL-C1"
    git.work_branch = "feature/PL-C1"
    git.get_working_directory.return_value = tmp_path
    git.get_current_branch.return_value = "feature/PL-C1"
    git.cleanup.return_value = True

    runner = MagicMock()

    async def agent_ok(task, **kwargs):
        # Simulate dashboard cancel while agent runs
        state_manager.update_state("PL-C1", status=TaskStatus.CANCELLED)
        return {
            "returncode": 0,
            "stdout": "done",
            "stderr": "",
            "session_file": None,
            "opencode_session_id": "s",
            "retry_info": {"attempts": 1, "max_retries": 0, "retried": False},
            "timed_out": False,
            "aborted": True,
        }

    runner.run_agent_with_retry = AsyncMock(side_effect=agent_ok)
    processor._contexts["PL-C1"] = {"git": git, "runner": runner}
    processor.git_manager = git
    processor.agent_runner = runner

    with patch.object(processor, "_init_git_manager", return_value=git):
        await processor._start_planning_workflow(state)

    assert state_manager.get_state("PL-C1").status == TaskStatus.CANCELLED


# ---------------------------------------------------------------------------
# 5 plan_ready start path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_plan_execution_from_api(processor, state_manager, tmp_path):
    state = state_manager.create_state("PR-1", "s", "d")
    state_manager.update_state(
        "PR-1", status=TaskStatus.PLAN_READY, plan_path=str(tmp_path / "p.md")
    )
    started = {"ok": False}

    async def fake_exec(st):
        started["ok"] = True
        state_manager.update_state("PR-1", status=TaskStatus.EXECUTING)

    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        result = await processor.start_plan_execution("PR-1")
    assert result["ok"] is True
    assert started["ok"] is True
    assert state_manager.get_state("PR-1").status == TaskStatus.EXECUTING


@pytest.mark.asyncio
async def test_plan_ready_label_starts_execution(processor, state_manager):
    state = state_manager.create_state("PR-2", "s", "d")
    state_manager.update_state("PR-2", status=TaskStatus.PLAN_READY)
    started = {"ok": False}

    async def fake_exec(st):
        started["ok"] = True

    event = {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "PR-2",
            "fields": {
                "status": {"name": "To Do", "statusCategory": {"key": "new"}},
                "labels": ["ai-assist", "ai-start-work"],
                "summary": "s",
            },
        },
    }
    with patch.object(processor, "_start_execution_workflow", side_effect=fake_exec):
        await processor._handle_issue_updated(event)
    assert started["ok"] is True


# ---------------------------------------------------------------------------
# 6 runner isolation — no foreign fallback
# ---------------------------------------------------------------------------


def test_runner_for_does_not_return_foreign_runner(processor, tmp_path):
    runner_a = AgentRunner(working_directory=tmp_path / "a")
    runner_b = AgentRunner(working_directory=tmp_path / "b")
    processor._contexts["A-1"] = {"git": None, "runner": runner_a}
    processor._contexts["B-2"] = {"git": MagicMock(issue_key="B-2"), "runner": runner_b}
    processor.agent_runner = runner_b
    processor.git_manager = processor._contexts["B-2"]["git"]

    assert processor._runner_for("A-1") is runner_a
    assert processor._runner_for("B-2") is runner_b
    # Unknown key with multi contexts → None (not B's runner)
    assert processor._runner_for("C-3") is None
    # Sandbox with git=None must not fall back to B's git
    assert processor._git_for("A-1") is None


# ---------------------------------------------------------------------------
# 7 job slot limiter resize wakes waiters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_slot_limiter_resize_unblocks_waiter():
    lim = _JobSlotLimiter(1)
    await lim.acquire()  # active=1

    async def waiter():
        await lim.acquire()

    t = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    lim.resize(2)
    await asyncio.wait_for(t, timeout=1.0)
    assert lim.active == 2
    await lim.release()
    await lim.release()
