"""Dotenv bootstrap + full agent env inheritance."""

from __future__ import annotations

import os
from pathlib import Path


def test_bootstrap_dotenv_loads_unknown_keys_into_environ(tmp_path, monkeypatch):
    from src.config import bootstrap_dotenv_into_environ

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MVCC_HOME=/opt/mvcc\n"
        "AWS_REGION=eu-west-1\n"
        "JIRA_API_TOKEN=should-load-too\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MVCC_HOME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    n = bootstrap_dotenv_into_environ(env_file, override=False)
    assert n >= 2
    assert os.environ.get("MVCC_HOME") == "/opt/mvcc"
    assert os.environ.get("AWS_REGION") == "eu-west-1"


def test_bootstrap_does_not_override_existing(tmp_path, monkeypatch):
    from src.config import bootstrap_dotenv_into_environ

    env_file = tmp_path / ".env"
    env_file.write_text("MVCC_HOME=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MVCC_HOME", "already-set")
    bootstrap_dotenv_into_environ(env_file, override=False)
    assert os.environ.get("MVCC_HOME") == "already-set"


def test_agent_env_passes_all_process_vars(monkeypatch):
    from src.orchestrator.agent_runner import _agent_subprocess_env

    monkeypatch.setenv("GITLAB_HOST_PATS", '{"gitlab.com":"glpat-x"}')
    monkeypatch.setenv("NPM_TOKEN", "npm-from-env")
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-from-process")
    monkeypatch.setenv("PATH", "/bin")
    env = _agent_subprocess_env()
    assert env.get("GITLAB_HOST_PATS") == '{"gitlab.com":"glpat-x"}'
    assert env.get("NPM_TOKEN") == "npm-from-env"
    assert env.get("CODEX_API_KEY") == "sk-codex-from-process"
    path = env.get("PATH") or ""
    assert path == "/bin" or path.endswith(os.pathsep + "/bin")


def test_agent_env_fills_codex_key_from_pc_system(monkeypatch):
    from src.orchestrator import agent_runner as ar

    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.setattr(
        ar,
        "read_host_system_environ",
        lambda: {"CODEX_API_KEY": "sk-from-windows", "EMPTY": "  "},
    )
    env = ar._agent_subprocess_env()
    assert env.get("CODEX_API_KEY") == "sk-from-windows"


def test_agent_env_process_value_wins_over_pc_system(monkeypatch):
    from src.orchestrator import agent_runner as ar

    monkeypatch.setenv("CODEX_API_KEY", "sk-from-process")
    monkeypatch.setattr(
        ar,
        "read_host_system_environ",
        lambda: {"CODEX_API_KEY": "sk-from-windows"},
    )
    env = ar._agent_subprocess_env()
    assert env.get("CODEX_API_KEY") == "sk-from-process"


def test_parse_cmd_set_output_keeps_equals_in_value():
    from src.orchestrator.agent_runner import _parse_cmd_set_output

    parsed = _parse_cmd_set_output(
        "CODEX_API_KEY=sk-abc=def\n"
        "OPENAI_API_KEY=sk-openai\n"
        "NOVALUE\n"
        "=ignore\n"
    )
    assert parsed["CODEX_API_KEY"] == "sk-abc=def"
    assert parsed["OPENAI_API_KEY"] == "sk-openai"
    assert "" not in parsed


def test_maybe_wsl_windows_path():
    from src.orchestrator.agent_runner import _maybe_wsl_windows_path

    assert _maybe_wsl_windows_path(r"C:\Windows\System32\cmd.exe") == (
        "/mnt/c/Windows/System32/cmd.exe"
    )
    assert _maybe_wsl_windows_path("/mnt/c/Windows/System32/cmd.exe") == (
        "/mnt/c/Windows/System32/cmd.exe"
    )
