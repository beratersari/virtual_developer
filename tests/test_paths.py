"""Durable C: data/temp paths survive a zip reinstall."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.paths import (
    WSL_DATA_DIR,
    WSL_TEMP_DIR,
    agent_data_dir,
    coerce_win_path,
    default_data_dir,
    default_temp_dir,
    ensure_agent_data_dir,
    under_agent_data,
)


def test_coerce_win_path_on_posix():
    import os

    got = coerce_win_path(r"C:\vd\yaver")
    if os.name == "nt":
        assert "vd" in str(got) and "yaver" in str(got)
    else:
        assert got == Path("/mnt/c/vd/yaver")


def test_agent_data_dir_honors_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dest = tmp_path / "durable"
    monkeypatch.setenv("YAVER_DATA_DIR", str(dest))
    assert agent_data_dir() == dest


def test_ensure_migrates_legacy_jira_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / ".jira-agent"
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "old.log").write_text("keep\n", encoding="utf-8")
    dest = tmp_path / "vd-data"
    dest.mkdir()
    (dest / "jobs").mkdir()
    monkeypatch.setenv("YAVER_DATA_DIR", str(dest))
    out = ensure_agent_data_dir(migrate=True)
    assert out == dest
    assert (dest / "sessions" / "old.log").read_text(encoding="utf-8") == "keep\n"
    # Second call must not wipe dest
    (dest / "sessions" / "new.log").write_text("n\n", encoding="utf-8")
    ensure_agent_data_dir(migrate=True)
    assert (dest / "sessions" / "new.log").is_file()


def test_under_agent_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "data"))
    ensure_agent_data_dir()
    inside = agent_data_dir() / "sessions" / "a.log"
    inside.parent.mkdir(parents=True)
    inside.write_text("x", encoding="utf-8")
    assert under_agent_data(inside) is True
    assert under_agent_data(tmp_path / "secret.txt") is False


def test_pytest_stays_on_local_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("YAVER_DATA_DIR", raising=False)
    monkeypatch.delenv("VD_DATA_DIR", raising=False)
    monkeypatch.delenv("TEMP_DIR_BASE", raising=False)
    assert default_data_dir() == Path.cwd() / ".jira-agent"
    assert default_temp_dir() == Path(".temp")
    assert WSL_DATA_DIR.as_posix().endswith("vd/yaver")
    assert WSL_TEMP_DIR.as_posix().endswith("vd/t")
