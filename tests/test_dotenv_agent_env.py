"""Dotenv bootstrap + simple agent env filter."""

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


def test_agent_env_strips_gitlab_host_pats(monkeypatch):
    from src.orchestrator.agent_runner import _agent_subprocess_env

    monkeypatch.setenv("GITLAB_HOST_PATS", '{"gitlab.com":"glpat-x"}')
    monkeypatch.setenv("PATH", "/bin")
    env = _agent_subprocess_env()
    assert "GITLAB_HOST_PATS" not in env
    assert env.get("PATH") == "/bin"
