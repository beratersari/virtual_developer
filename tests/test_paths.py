"""Durable C: data/temp paths survive a zip reinstall."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield

from src.paths import (
    agent_data_dir,
    coerce_win_path,
    default_base_dir,
    default_data_dir,
    default_temp_dir,
    ensure_agent_data_dir,
    plans_dir,
    under_agent_data,
)


def test_coerce_win_path_on_posix():
    import os

    from src.paths import default_linux_data_dir

    got = coerce_win_path(r"C:\vd\yaver")
    if os.name == "nt":
        assert "vd" in str(got) and "yaver" in str(got)
    elif Path("/mnt/c").is_dir():
        assert got == Path("/mnt/c/vd/yaver")
    else:
        assert got == default_linux_data_dir()


def test_agent_data_dir_honors_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dest = tmp_path / "durable"
    monkeypatch.setenv("YAVER_DATA_DIR", str(dest))
    assert agent_data_dir() == dest


def test_base_dir_creates_yaver_and_t(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.paths import agent_temp_dir, configured_base_dir

    base = tmp_path / "vd"
    monkeypatch.delenv("YAVER_DATA_DIR", raising=False)
    monkeypatch.delenv("VD_DATA_DIR", raising=False)
    monkeypatch.delenv("TEMP_DIR_BASE", raising=False)
    monkeypatch.setenv("YAVER_BASE_DIR", str(base))
    assert configured_base_dir() == base
    assert agent_data_dir() == base / "yaver"
    assert agent_temp_dir() == base / "t"


def test_legacy_data_dir_wins_over_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "vd"
    explicit = tmp_path / "old-data"
    monkeypatch.setenv("YAVER_BASE_DIR", str(base))
    monkeypatch.setenv("YAVER_DATA_DIR", str(explicit))
    assert agent_data_dir() == explicit


def test_legacy_pair_shares_one_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.paths import configured_base_dir

    base = tmp_path / "vd"
    monkeypatch.delenv("YAVER_BASE_DIR", raising=False)
    monkeypatch.setenv("YAVER_DATA_DIR", str(base / "yaver"))
    monkeypatch.setenv("TEMP_DIR_BASE", str(base / "t"))
    assert configured_base_dir() == base


def test_absolute_temp_dir_wins_over_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.paths import agent_temp_dir

    base = tmp_path / "base"
    clones = tmp_path / "custom-clones"
    monkeypatch.setenv("YAVER_BASE_DIR", str(base))
    monkeypatch.setenv("TEMP_DIR_BASE", str(clones))
    assert agent_temp_dir() == clones


def test_split_legacy_paths_are_not_one_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.paths import configured_base_dir

    monkeypatch.delenv("YAVER_BASE_DIR", raising=False)
    monkeypatch.setenv("YAVER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TEMP_DIR_BASE", str(tmp_path / "clones"))
    assert configured_base_dir() is None


def test_init_writes_base_dir_and_creates_both_folders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from click.testing import CliRunner

    from cli import cli
    from src.config import settings

    base = tmp_path / "base"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YAVER_DATA_DIR", raising=False)
    monkeypatch.delenv("VD_DATA_DIR", raising=False)
    monkeypatch.delenv("TEMP_DIR_BASE", raising=False)
    monkeypatch.setenv("YAVER_BASE_DIR", str(base))
    monkeypatch.setattr(settings, "temp_dir_base", base / "t")
    result = CliRunner().invoke(cli, ["init"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "YAVER_BASE_DIR=" in text
    assert "base" in text
    assert "YAVER_DATA_DIR=" not in text
    assert "TEMP_DIR_BASE=" not in text
    assert (base / "yaver").is_dir()
    assert (base / "t").is_dir()


def test_windows_base_without_localappdata(monkeypatch: pytest.MonkeyPatch):
    from src.paths import default_windows_base_dir

    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert default_windows_base_dir() == Path.home() / "AppData" / "Local" / "Yaver"


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


def test_linux_default_is_xdg_data_home(monkeypatch: pytest.MonkeyPatch):
    from src import paths as paths_mod

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert paths_mod.default_linux_base_dir() == Path.home() / ".local" / "share" / "yaver"
    assert paths_mod.default_linux_data_dir() == Path.home() / ".local" / "share" / "yaver" / "yaver"
    assert paths_mod.default_linux_temp_dir() == Path.home() / ".local" / "share" / "yaver" / "t"
    custom = Path.home() / "custom-xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(custom))
    assert paths_mod.default_linux_base_dir() == custom / "yaver"


def test_windows_default_is_local_appdata(monkeypatch: pytest.MonkeyPatch):
    from src import paths as paths_mod

    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\dev\AppData\Local")
    assert paths_mod.default_windows_base_dir() == Path(r"C:\Users\dev\AppData\Local\Yaver")


def test_coerce_win_path_native_linux_without_mnt(monkeypatch: pytest.MonkeyPatch):
    from src import paths as paths_mod

    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    got = paths_mod._linux_path_from_win_rest("vd/yaver")
    assert got == Path.home() / ".local" / "share" / "yaver" / "yaver"
    got_t = paths_mod._linux_path_from_win_rest("vd/t")
    assert got_t == Path.home() / ".local" / "share" / "yaver" / "t"


def test_pytest_stays_on_local_defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("YAVER_DATA_DIR", raising=False)
    monkeypatch.delenv("VD_DATA_DIR", raising=False)
    monkeypatch.delenv("TEMP_DIR_BASE", raising=False)
    monkeypatch.delenv("YAVER_BASE_DIR", raising=False)
    assert default_data_dir() == Path.cwd() / ".jira-agent"
    assert default_temp_dir() == Path(".temp")
    assert default_base_dir().name in {"Yaver", "yaver"}


def test_plans_dir_is_under_yaver_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dest = tmp_path / "yaver"
    monkeypatch.setenv("YAVER_DATA_DIR", str(dest))
    assert plans_dir() == dest / "plans"
