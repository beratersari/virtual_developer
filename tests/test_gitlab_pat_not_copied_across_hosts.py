"""Saving one GitLab host must not copy that PAT onto a different host."""

from pathlib import Path

import pytest

from src.dashboard.schemas import GitlabHostCredentialUpdate, SettingsUpdate
from src.dashboard.service import apply_settings_update


def _isolate_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "install"
    work.mkdir()
    monkeypatch.chdir(work)
    (work / ".env").write_text("JIRA_HOST=https://jira.example.com\n", encoding="utf-8")
    runtime = tmp_path / "data" / "runtime_settings.json"
    runtime.parent.mkdir(parents=True)
    monkeypatch.setattr("src.config.runtime_settings_path", lambda: runtime)
    monkeypatch.setattr("src.paths.agent_data_dir", lambda: runtime.parent)


def test_empty_pat_on_a_new_host_does_not_reuse_another_hosts_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_settings(tmp_path, monkeypatch)
    from src.config import settings

    settings.gitlab_host_pats = '{"https://gitlab.example.com":"super-secret-pat"}'
    apply_settings_update(
        SettingsUpdate(
            gitlab_credentials=[
                GitlabHostCredentialUpdate(host="https://gitlab.example.com", pat=""),
                GitlabHostCredentialUpdate(host="https://other.example.com", pat=""),
            ]
        )
    )
    assert settings.gitlab_pat_for_host("gitlab.example.com") == "super-secret-pat"
    assert settings.gitlab_pat_for_host("other.example.com") == ""
