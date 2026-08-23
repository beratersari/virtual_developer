"""install-backends.bat / Install-Backends.ps1 stay offline and do not clobber files."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BAT = ROOT / "install-backends.bat"
PS1 = ROOT / "packaging" / "windows" / "Install-Backends.ps1"


def test_install_backends_files_exist():
    assert BAT.is_file()
    assert PS1.is_file()


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
    assert "opencode-home.zip" in text
    assert "$args" not in text
    assert "$pid" not in text
    assert "venv" in text.lower()
    assert "not installed by this script" in text
