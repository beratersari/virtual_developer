"""Detect OpenCode home and copy agents/skills without installing the CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "install-opencode-agents.bat"
PS1 = ROOT / "packaging" / "windows" / "Install-OpencodeAgents.ps1"
SH = ROOT / "install-opencode-agents.sh"
PY = ROOT / "packaging" / "install_opencode_agents.py"


def _load():
    import importlib.util

    spec = importlib.util.spec_from_file_location("install_opencode_agents", PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_install_opencode_agents_files_exist():
    assert BAT.is_file()
    assert PS1.is_file()
    assert SH.is_file()
    assert PY.is_file()


def test_bat_does_not_redirect_with_echo_arrow():
    text = BAT.read_text(encoding="utf-8")
    assert "echo" in text.lower()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("echo") and "->" in stripped:
            pytest.fail(f"cmd.exe echo redirect landmine: {stripped}")
    assert "-File" in text
    assert "Install-OpencodeAgents.ps1" in text


def test_ps1_avoids_automatic_variable_names():
    text = PS1.read_text(encoding="utf-8")
    assert "param(" in text
    lowered = text.lower()
    assert "param([string]$args" not in lowered.replace(" ", "")
    assert "$pid =" not in lowered
    assert "OPENCODE_HOME" in text
    assert ".opencode" in text
    assert ".config\\opencode" not in text or "Never writes" in text or "never writes" in text.lower()


def test_find_source_prefers_opencoderman(tmp_path: Path):
    mod = _load()
    ocm = tmp_path / "opencoderman"
    (ocm / "agents").mkdir(parents=True)
    (ocm / "skills").mkdir()
    (ocm / "agents" / "derman-build.md").write_text("b\n", encoding="utf-8")
    (ocm / "skills" / "x.md").write_text("s\n", encoding="utf-8")
    agents, skills = mod.find_source(tmp_path)
    assert agents == ocm / "agents"
    assert skills == ocm / "skills"


def test_find_opencode_home_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load()
    home = tmp_path / "custom-oc"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "opencode.exe").write_bytes(b"x")
    monkeypatch.setenv("OPENCODE_HOME", str(home))
    monkeypatch.delenv("USERPROFILE", raising=False)
    found = mod.find_opencode_home()
    assert found == home


def test_find_opencode_home_from_path_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load()
    home = tmp_path / ".opencode"
    bindir = home / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "opencode.exe"
    exe.write_bytes(b"x")
    monkeypatch.delenv("OPENCODE_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "nouser"))
    found = mod.find_opencode_home(path_entries=[str(bindir)])
    assert found == home


def test_find_opencode_home_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mod = _load()
    monkeypatch.delenv("OPENCODE_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError, match="not installed"):
        mod.find_opencode_home(path_entries=[])


def test_install_agents_copies_into_home(tmp_path: Path):
    mod = _load()
    src = tmp_path / "zip"
    agents = src / "opencoderman" / "agents"
    skills = src / "opencoderman" / "skills" / "python"
    agents.mkdir(parents=True)
    skills.mkdir(parents=True)
    (agents / "derman-build.md").write_text("build\n", encoding="utf-8")
    (agents / "derman-plan.md").write_text("plan\n", encoding="utf-8")
    (agents / "gitlab-reviewer.md").write_text("review\n", encoding="utf-8")
    for i in range(10):
        d = src / "opencoderman" / "skills" / f"s{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("x\n", encoding="utf-8")
    home = tmp_path / "oc-home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "opencode.exe").write_bytes(b"x")
    dest = mod.install_agents(source_root=src, opencode_home=home)
    assert dest == home
    assert (home / "agents" / "derman-build.md").read_text(encoding="utf-8") == "build\n"
    assert (home / "agents" / "derman-plan.md").is_file()
    assert not (home / "agents" / "gitlab-reviewer.md").exists()
    assert len(list((home / "skills").rglob("SKILL.md"))) == 10


def test_cli_uses_source_root_and_home(tmp_path: Path):
    mod = _load()
    src = tmp_path / "zip"
    (src / "opencoderman" / "agents").mkdir(parents=True)
    (src / "opencoderman" / "skills" / "s0").mkdir(parents=True)
    (src / "opencoderman" / "agents" / "derman-build.md").write_text("b\n", encoding="utf-8")
    (src / "opencoderman" / "agents" / "derman-plan.md").write_text("p\n", encoding="utf-8")
    for i in range(10):
        d = src / "opencoderman" / "skills" / f"s{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("x\n", encoding="utf-8")
    home = tmp_path / "oc"
    home.mkdir()
    (home / "opencode.json").write_text("{}", encoding="utf-8")
    assert (
        mod.main(
            [
                "--source-root",
                str(src),
                "--opencode-home",
                str(home),
            ]
        )
        == 0
    )
    assert (home / "agents" / "derman-plan.md").is_file()
