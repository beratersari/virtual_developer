"""AgentBackend protocol: OpenCode default + Codex CLI adapter."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backends.base import (
    BACKEND_CODEX,
    BACKEND_OPENCODE,
    AgentRunRequest,
    AgentRunResult,
    normalize_backend_name,
)
from src.backends.codex import (
    build_codex_argv,
    build_codex_config_toml,
    models_from_codex_config,
    parse_codex_thread_id,
    resolve_codex_cli,
    seed_isolated_codex_home,
    summarize_codex_exec_line,
)
from src.backends.registry import get_agent_backend, resolve_backend_name
from src.issue_git_spec import parse_issue_git_spec, upsert_params_backend


def test_normalize_backend_name_aliases():
    assert normalize_backend_name("") == ""
    assert normalize_backend_name("  OpenCode ") == BACKEND_OPENCODE
    assert normalize_backend_name("oh-my-openagent") == BACKEND_OPENCODE
    assert normalize_backend_name("openai-codex") == BACKEND_CODEX
    assert normalize_backend_name("openai") == BACKEND_CODEX
    assert normalize_backend_name("codex") == BACKEND_CODEX
    assert normalize_backend_name("not-a-backend") == ""


def test_resolve_backend_name_prefers_task_then_issue_then_settings(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "agent_backend", "opencode")
    assert (
        resolve_backend_name(task_backend="codex", issue_backend="opencode")
        == BACKEND_CODEX
    )
    assert resolve_backend_name(issue_backend="codex") == BACKEND_CODEX
    assert resolve_backend_name() == BACKEND_OPENCODE
    monkeypatch.setattr(settings, "agent_backend", "codex")
    assert resolve_backend_name() == BACKEND_CODEX
    monkeypatch.setattr(settings, "agent_backend", "")
    assert resolve_backend_name() == BACKEND_OPENCODE


def test_get_agent_backend_returns_named_impl():
    assert get_agent_backend("codex").name == BACKEND_CODEX
    assert get_agent_backend("opencode").name == BACKEND_OPENCODE
    assert get_agent_backend("unknown").name == BACKEND_OPENCODE


def test_resolve_codex_cli_prefers_official_windows_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    official = tmp_path / "Local" / "Programs" / "OpenAI" / "Codex" / "bin"
    official.mkdir(parents=True)
    exe = official / "codex.exe"
    exe.write_text("official", encoding="utf-8")
    native = official / "codex"
    native.write_text("official-native", encoding="utf-8")
    vendor = tmp_path / "vendor" / "bin"
    vendor.mkdir(parents=True)
    (vendor / "codex.exe").write_text("vendor", encoding="utf-8")
    (vendor / "codex").write_text("vendor-native", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    found = resolve_codex_cli("codex")
    if os.name == "nt":
        assert found == str(exe)
    else:
        assert found == str(native)


def test_resolve_codex_cli_prefers_offline_vendor(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    explicit = tmp_path / "custom-codex"
    explicit.write_text("x", encoding="utf-8")
    assert resolve_codex_cli(str(explicit)) == str(explicit)
    vendor = tmp_path / "vendor" / "bin"
    vendor.mkdir(parents=True)
    native = vendor / "codex"
    native.write_text("bin", encoding="utf-8")
    assert resolve_codex_cli("codex") == str(native)


def test_pinned_codex_version_in_versions_env():
    text = (
        Path(__file__).resolve().parents[1] / "packaging" / "windows" / "versions.env"
    ).read_text(encoding="utf-8")
    assert "CODEX_VERSION=0.149.0" in text
    assert "CODEX_WINDOWS_ASSET=codex-package-x86_64-pc-windows-msvc.tar.gz" in text


def test_build_codex_argv_has_no_secrets_and_prompt_last():
    argv = build_codex_argv(
        cli="codex",
        prompt="do the work",
        model="my-custom-model",
    )
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--skip-git-repo-check" in argv
    assert "--json" in argv
    assert argv[-1].endswith("do the work")
    assert "UNATTENDED JOB:" in argv[-1]
    assert "-m" in argv
    assert "my-custom-model" in argv
    joined = " ".join(argv)
    assert "sk-" not in joined
    assert "API_KEY" not in joined


def test_build_codex_argv_resume_skips_model_flag():
    argv = build_codex_argv(
        cli="codex",
        prompt="continue",
        model="ignored-on-resume",
        resume_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    assert "resume" in argv
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in argv
    assert "-m" not in argv


def test_models_from_codex_config_reads_declared_ids():
    text = "\n".join(
        [
            'model = "gpt-5"',
            "# model = \"ignored\"",
            "",
            "[profiles.zen]",
            'model = "muse-spark-1.2-contributor-free"',
        ]
    )
    assert models_from_codex_config(text) == [
        "gpt-5",
        "muse-spark-1.2-contributor-free",
    ]
    assert models_from_codex_config("") == []


def test_build_models_response_codex_skips_opencode_inventory(tmp_path, monkeypatch):
    from src.config import settings
    from src.dashboard.service import build_models_response

    home = tmp_path / ".codex"
    home.mkdir()
    (home / "config.toml").write_text(
        'model = "muse-spark-1.2-contributor-free"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("src.backends.codex.user_codex_home", lambda: home)
    monkeypatch.setattr(settings, "default_model", "settings-default")
    with patch("src.dashboard.service.list_available_models") as mocked:
        resp = build_models_response(backend="codex")
        mocked.assert_not_called()
    ids = [m.id for m in resp.models]
    assert resp.backend == BACKEND_CODEX
    assert "settings-default" in ids
    assert "muse-spark-1.2-contributor-free" in ids
    assert all("opencode/" not in (m.id or "") for m in resp.models if m.source == "cli")


def test_codex_config_merges_user_file_and_overrides_model():
    user = "\n".join(
        [
            'model = "old/from-user"',
            'approval_policy = "on-request"',
            'model_provider = "yaver"',
            "",
            "[model_providers.yaver]",
            'name = "Yaver"',
            'base_url = "https://llm.example.com/v1"',
            'wire_api = "responses"',
            'env_key = "OPENAI_API_KEY"',
        ]
    )
    toml = build_codex_config_toml(model="acme/qwen", user_config=user)
    assert 'model = "acme/qwen"' in toml
    assert 'model = "old/from-user"' not in toml
    assert 'approval_policy = "never"' in toml
    assert 'sandbox_mode = "danger-full-access"' in toml
    assert 'model_provider = "yaver"' in toml
    assert 'base_url = "https://llm.example.com/v1"' in toml
    assert "sk-" not in toml
    assert "CODEX_API_KEY" not in toml
    minimal = build_codex_config_toml(model="muse-spark-1.2-contributor-free")
    assert 'model = "muse-spark-1.2-contributor-free"' in minimal
    assert "model_providers" not in minimal
    assert "env_key" not in minimal


def test_seed_isolated_codex_home_copies_auth(tmp_path, monkeypatch):
    user = tmp_path / "operator-codex"
    user.mkdir()
    (user / "auth.json").write_text('{"token":"keep-me"}', encoding="utf-8")
    (user / "config.toml").write_text(
        'model = "user/default"\nmodel_provider = "openai"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("src.backends.codex.user_codex_home", lambda: user)
    home = tmp_path / "job-home"
    seed_isolated_codex_home(home, model="job/model")
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"token":"keep-me"}'
    written = (home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "job/model"' in written
    assert 'model_provider = "openai"' in written
    assert 'approval_policy = "never"' in written


def test_parse_codex_thread_id_from_jsonl():
    blob = "\n".join(
        [
            '{"type":"thread.started","thread_id":"11111111-2222-3333-4444-555555555555"}',
            "noise",
            '{"thread":{"id":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}',
        ]
    )
    assert parse_codex_thread_id(blob) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert parse_codex_thread_id("") is None


def test_summarize_codex_exec_line_steps():
    assert summarize_codex_exec_line(
        '{"type":"thread.started","thread_id":"tid-1"}'
    ) == "[codex] thread tid-1"
    assert summarize_codex_exec_line(
        '{"type":"item.started","item":{"type":"command_execution","command":"ls","status":"in_progress"}}'
    ) == "[codex] running: ls"
    assert summarize_codex_exec_line(
        '{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}'
    ) == "[codex] assistant: Done"
    assert summarize_codex_exec_line('{"type":"turn.started"}') is None
    assert summarize_codex_exec_line("[codex] cwd=/tmp") == "[codex] cwd=/tmp"


@pytest.mark.asyncio
async def test_codex_run_classifies_writer_lock_not_incomplete(tmp_path, monkeypatch):
    from src.backends.base import AgentRunRequest
    from src.backends.codex import CodexBackend

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jira-agent").mkdir()
    lock_line = (
        "Error: thread/resume failed: thread "
        "01a03397-15ff-7941-a5e7-17e23b3d7b82 already has an active writer"
    )

    class _FakeProc:
        pid = 4242
        returncode = 1
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()

        def __init__(self):
            self.stdout.feed_data((lock_line + "\n").encode())
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return 1

    async def _fake_exec(*_a, **_k):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    result = await CodexBackend().run(
        AgentRunRequest(
            prompt="continue",
            session_id="01a03397-15ff-7941-a5e7-17e23b3d7b82",
            working_directory=tmp_path,
            timeout_seconds=5,
        )
    )
    assert result.extra.get("thread_locked") is True
    assert result.incomplete is False
    assert "codex thread locked" in result.incomplete_reasons
    assert result.returncode != 0


def test_is_codex_thread_lock_error_detects_kan12371_log():
    from src.backends.codex import is_codex_thread_lock_error

    blob = (
        "ERROR codex_core::session::session: failed to initialize thread "
        "persistence: thread-store conflict: thread "
        "01a03397-15ff-7941-a5e7-17e23b3d7b82 already has an active writer\n"
        "Error: thread/resume: thread/resume failed: thread "
        "01a03397-15ff-7941-a5e7-17e23b3d7b82 already has an active writer "
        "(code -32600)"
    )
    assert is_codex_thread_lock_error(blob) is True
    assert is_codex_thread_lock_error("[codex] exit 1") is False
    assert is_codex_thread_lock_error("") is False


def test_codex_cmd_for_log_hides_prompt():
    from src.backends.codex import _codex_cmd_for_log

    shown = _codex_cmd_for_log(
        ["codex", "exec", "--json", "-m", "gpt", "UNATTENDED JOB: do the work"]
    )
    assert "UNATTENDED" not in shown
    assert "<prompt" in shown
    assert "codex exec --json -m gpt" in shown


def test_bind_log_context_sets_job_id_for_filter():
    from src.log_context import clear_log_context, get_job_id, get_issue_key
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    clear_log_context()
    try:
        task = AgentTask(
            description="d",
            prompt="p",
            agent="build",
            issue_key="KAN-9",
            job_id="job_codex_filter_1",
        )
        AgentRunner._bind_log_context(task)
        assert get_job_id() == "job_codex_filter_1"
        assert get_issue_key() == "KAN-9"
    finally:
        clear_log_context()


def test_parse_and_upsert_params_backend():
    desc = """
{params}
Repository: https://gitlab.example.com/group/repo.git
Source branch: develop
Target branch: main
Mode: build
{params}
"""
    spec, err = parse_issue_git_spec("feat", desc)
    assert err is None
    assert spec is not None
    assert spec.backend is None

    one = upsert_params_backend(desc, "openai-codex")
    spec2, err2 = parse_issue_git_spec("", one)
    assert err2 is None
    assert spec2 is not None
    assert spec2.backend == BACKEND_CODEX
    two = upsert_params_backend(one, "opencode")
    spec3, err3 = parse_issue_git_spec("", two)
    assert err3 is None
    assert spec3 is not None
    assert spec3.backend == BACKEND_OPENCODE
    assert two.lower().count("backend:") == 1


@pytest.mark.asyncio
async def test_runner_dispatches_codex_not_serve(tmp_path, monkeypatch):
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jira-agent").mkdir()
    captured: dict = {}

    class _FakeCodex:
        name = BACKEND_CODEX

        async def run(self, request: AgentRunRequest) -> AgentRunResult:
            captured["prompt"] = request.prompt
            captured["model"] = request.model
            captured["cwd"] = str(request.working_directory)
            return AgentRunResult(
                returncode=0,
                stdout="ok",
                session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                backend=BACKEND_CODEX,
            )

        def cancel(self, handle):
            handle["cancel"] = True

    runner = AgentRunner(working_directory=tmp_path)
    serve = AsyncMock(return_value={"returncode": 99, "stderr": "should not run"})
    monkeypatch.setattr(runner, "_run_agent_via_serve", serve)
    monkeypatch.setattr(
        "src.backends.get_agent_backend",
        lambda name=None: _FakeCodex(),
    )
    monkeypatch.setattr(
        "src.backends.resolve_backend_name",
        lambda **kw: BACKEND_CODEX,
    )

    task = AgentTask(
        description="Build",
        prompt="implement it",
        agent="atlas",
        issue_key="KAN-1",
        backend="codex",
        model="acme/custom",
    )
    result = await runner.run_agent(task, timeout_seconds=30)
    serve.assert_not_called()
    assert result["returncode"] == 0
    assert result["backend"] == BACKEND_CODEX
    assert captured["prompt"] == "implement it"
    assert captured["model"] == "acme/custom"


@pytest.mark.asyncio
async def test_runner_opencode_still_uses_serve(tmp_path, monkeypatch):
    from src.orchestrator.agent_runner import AgentRunner, AgentTask

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".jira-agent").mkdir()
    runner = AgentRunner(working_directory=tmp_path)
    serve = AsyncMock(
        return_value={
            "task_id": "t",
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
            "backend": BACKEND_OPENCODE,
        }
    )
    monkeypatch.setattr(runner, "_run_agent_via_serve", serve)
    task = AgentTask(
        description="Build",
        prompt="implement it",
        agent="atlas",
        issue_key="KAN-1",
        backend="opencode",
    )
    result = await runner.run_agent(task, timeout_seconds=30)
    serve.assert_awaited_once()
    assert result["returncode"] == 0


def test_settings_update_rejects_unknown_backend():
    from pydantic import ValidationError

    from src.dashboard.schemas import SettingsUpdate

    with pytest.raises(ValidationError):
        SettingsUpdate(agent_backend="claude")


def test_settings_update_persists_backend_not_codex_provider(tmp_path, monkeypatch):
    from src.config import settings
    from src.dashboard.schemas import SettingsUpdate
    from src.dashboard.service import apply_settings_update, build_settings_view

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "agent_backend", "opencode")
    monkeypatch.setattr(settings, "default_model", "old/m")
    view = build_settings_view()
    dumped = view.model_dump()
    assert "codex_api_key" not in dumped
    assert "codex_base_url" not in dumped
    assert "codex_wire_api" not in dumped
    assert "codex_api_key_configured" not in dumped

    updated = apply_settings_update(
        SettingsUpdate(
            agent_backend="codex",
            default_model="acme/qwen",
        )
    )
    assert updated.agent_backend == BACKEND_CODEX
    assert updated.default_model == "acme/qwen"
    assert settings.agent_backend == BACKEND_CODEX
    assert settings.default_model == "acme/qwen"
    assert not hasattr(updated, "codex_api_key")
    env_path = tmp_path / ".env"
    if env_path.is_file():
        assert "CODEX_API_KEY" not in env_path.read_text(encoding="utf-8")


def test_create_scheduled_job_persists_backend(tmp_path):
    from datetime import datetime, timedelta

    from src.scheduler.service import create_scheduled_job
    from src.state.schedule_store import ScheduleStore

    store = ScheduleStore(schedules_dir=tmp_path / "schedules")
    client = MagicMock()
    client.create_issue.return_value = {"key": "KAN-200"}
    client.transition_to_in_progress.return_value = True
    out = create_scheduled_job(
        title="Codex job",
        description="body",
        repository_url="https://gitlab.com/a/b.git",
        source_branch="develop",
        target_branch="main",
        mode="build",
        model="acme/custom",
        backend="codex",
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
        project_key="KAN",
        source_branch_mode="custom",
        jira_client=client,
        store=store,
    )
    assert out["ok"] is True
    assert out["schedule"]["backend"] == BACKEND_CODEX
    desc = client.create_issue.call_args.kwargs.get("description") or ""
    assert "Backend: codex" in desc
    assert "Model: acme/custom" in desc


def test_backend_for_issue_prefers_params(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from src.processor import JobProcessor

    monkeypatch.chdir(tmp_path)
    jira = MagicMock()
    with patch("src.processor.create_jira_client", return_value=jira):
        proc = JobProcessor()
    monkeypatch.setattr("src.processor.settings.agent_backend", "opencode")
    desc = """
{params}
Repository: https://gitlab.com/a/b.git
Source branch: develop
Target branch: develop
Mode: build
Backend: codex
{params}
"""
    st = SimpleNamespace(issue_summary="s", description=desc)
    assert proc._backend_for_issue(st) == BACKEND_CODEX
    st2 = SimpleNamespace(issue_summary="s", description="no params")
    assert proc._backend_for_issue(st2) == BACKEND_OPENCODE
    monkeypatch.setattr("src.processor.settings.agent_backend", "codex")
    assert proc._backend_for_issue(None) == BACKEND_CODEX
