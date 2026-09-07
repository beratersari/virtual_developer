"""install-backends.bat / Install-Backends.ps1 stay offline and do not clobber files."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "install-backends.bat"
PS1 = ROOT / "packaging" / "windows" / "Install-Backends.ps1"


def test_install_backends_files_exist():
    assert BAT.is_file()
    assert PS1.is_file()


def test_combined_install_bat_is_removed():
    """Dashboard owns Python; backends own OpenCode; install-codex owns Codex."""
    assert not (ROOT / "install.bat").exists()
    assert (ROOT / "install-dashboard.bat").is_file()
    assert (ROOT / "install-dashboard-system-python.bat").is_file()
    assert (ROOT / "install-codex.bat").is_file()
    dash = (ROOT / "install-dashboard.bat").read_text(encoding="utf-8")
    assert "python -m venv" in dash
    assert "python-wheels" in dash
    assert "opencode-home.zip" not in dash
    be = BAT.read_text(encoding="utf-8")
    assert "opencoderman" in be
    assert "opencode-home.zip" in be
    assert "venv" not in be.lower() or "no Python" in be


def test_install_codex_bat_is_offline_tar_only():
    bat = ROOT / "install-codex.bat"
    text = bat.read_text(encoding="utf-8")
    assert bat.is_file()
    assert "Install-Backends.ps1" in text
    assert "-Codex" in text
    assert "tar.exe" in text
    assert r"vendor\%ASSET%" in text or r"vendor\codex-package-x86_64-pc-windows-msvc.tar.gz" in text
    assert "config.toml" in text
    assert "github.com" not in text.lower()
    assert "curl" not in text.lower()
    assert "opencode-home.zip" not in text
    assert "python -m venv" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("echo ") and " -> " in stripped:
            raise AssertionError(f"cmd.exe redirect landmine: {stripped}")


def test_install_backends_bat_has_no_echo_redirect():
    text = BAT.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("echo ") and " -> " in stripped:
            raise AssertionError(f"cmd.exe redirect landmine: {stripped}")
    assert "Install-Backends.ps1" in text
    assert "maybe_pause" in text


def test_install_backends_ps1_uses_official_codex_path():
    text = PS1.read_text(encoding="utf-8")
    assert r"Programs\OpenAI\Codex\bin" in text
    assert "install_opencode.py" in text
    assert "opencoderman" in text
    assert "opencode-home.zip" in text
    assert "Expand-TarGz" in text
    assert "codex-package-x86_64-pc-windows-msvc.tar.gz" in text
    assert r".codex" in text
    assert "codex-config.toml" in text
    assert "github.com/openai/codex/releases" not in text
    assert "$args" not in text
    assert "$pid" not in text
    assert "venv" in text.lower()
    assert "not installed by this script" in text


def test_install_opencode_online_uses_opencoderman():
    bat = ROOT / "install-opencode-online.bat"
    text = bat.read_text(encoding="utf-8")
    assert bat.is_file()
    assert "opencoderman" in text
    assert "Install-OpencodeOnline.ps1" in text
    assert "vendor\\node\\node.exe" not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("echo ") and " -> " in stripped:
            raise AssertionError(f"cmd.exe redirect landmine: {stripped}")
    ps1 = ROOT / "packaging" / "windows" / "Install-OpencodeOnline.ps1"
    ptext = ps1.read_text(encoding="utf-8")
    assert "install_opencode.py" in ptext
    assert "--online" in ptext
    assert "oh-my-openagent" not in ptext


def test_dummy_codex_config_exists():
    cfg = ROOT / "packaging" / "windows" / "codex-config.toml"
    text = cfg.read_text(encoding="utf-8")
    assert cfg.is_file()
    assert "model" in text
    assert "api_key" not in text.lower()
    assert "sk-" not in text
