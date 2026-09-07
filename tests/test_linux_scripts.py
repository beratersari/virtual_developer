"""Linux install/start scripts exist, parse, and do not reintroduce the plugin."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "packaging" / "linux"

SCRIPTS = (
    "install-dashboard.sh",
    "install-backends.sh",
    "install-codex.sh",
    "start.sh",
    "start-backend.sh",
    "start-frontend.sh",
    "start-opencode.sh",
    "start-opencode-serve.sh",
    "stop.sh",
    "ensure-opencode-serve.sh",
    "assert-payload.sh",
    "lib.sh",
    "build-dist.sh",
    "resolve-version.sh",
)


def test_linux_scripts_exist_and_parse():
    assert (LINUX / "README.md").is_file()
    assert (LINUX / "opencode.json").is_file()
    cfg = (LINUX / "opencode.json").read_text(encoding="utf-8")
    assert '"plugin": []' in cfg
    assert "oh-my-openagent" not in cfg
    for name in SCRIPTS:
        path = LINUX / name
        assert path.is_file(), name
        parsed = subprocess.run(
            ["bash", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert parsed.returncode == 0, f"{name}: {parsed.stderr}"
    wrappers = (
        "install.sh",
        "install-dashboard.sh",
        "install-backends.sh",
        "install-codex.sh",
        "start.sh",
        "start-backend.sh",
        "start-frontend.sh",
        "start-opencode.sh",
        "start-opencode-serve.sh",
        "stop.sh",
    )
    for name in wrappers:
        path = ROOT / name
        assert path.is_file(), name
        parsed = subprocess.run(
            ["bash", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert parsed.returncode == 0, f"{name}: {parsed.stderr}"


def test_wsl_integration_probe_has_thirty_named_requests():
    text = (LINUX / "wsl_integration_probe.py").read_text(encoding="utf-8")
    names = re.findall(r'p\.req\(\s*"([^"]+)"', text)
    assert (LINUX / "opencode.integration.json").is_file()
    cfg = (LINUX / "opencode.integration.json").read_text(encoding="utf-8")
    assert "mimo-v2.5-free" in cfg
    assert '"plugin": []' in cfg
    assert len(names) >= 30, names


def test_linux_dist_ci_and_offline_vendor_hooks():
    wf = ROOT / ".github" / "workflows" / "linux-dist.yml"
    assert wf.is_file()
    text = wf.read_text(encoding="utf-8")
    assert "packaging/linux/build-dist.sh" in text
    assert "packaging/linux/assert-payload.sh" in text
    assert_sh = (LINUX / "assert-payload.sh").read_text(encoding="utf-8")
    assert "vendor/opencode-home.zip" in assert_sh
    assert "vendor/python-wheels" in assert_sh
    assert ".env.example" in assert_sh
    assert "include-hidden-files: true" in text
    versions = (ROOT / "packaging" / "windows" / "versions.env").read_text(
        encoding="utf-8"
    )
    assert "OPENCODE_LINUX_ASSET=" in versions
    assert "CODEX_LINUX_ASSET=" in versions
    be = (LINUX / "install-backends.sh").read_text(encoding="utf-8")
    assert "vendor/opencode-home.zip" in be
    assert "opencoderman" in be
    assert "Linux Distribution" in be
    assert_sh = (LINUX / "assert-payload.sh").read_text(encoding="utf-8")
    assert "opencoderman/install.py" in assert_sh
    assert "opencoderman.pin" in assert_sh
    cx = (LINUX / "install-codex.sh").read_text(encoding="utf-8")
    assert "vendor/codex" in cx
    build = LINUX / "build-dist.sh"
    parsed = subprocess.run(
        ["bash", "-n", str(build)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert parsed.returncode == 0, parsed.stderr
    parsed = subprocess.run(
        ["bash", "-n", str(LINUX / "resolve-version.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert parsed.returncode == 0, parsed.stderr
    parsed = subprocess.run(
        ["bash", "-n", str(LINUX / "assert-payload.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert parsed.returncode == 0, parsed.stderr


def test_linux_installers_do_not_install_oh_my_plugin():
    text = (LINUX / "install-backends.sh").read_text(encoding="utf-8")
    assert "oh-my-openagent" not in text
    assert "oh-my-opencode" not in text
    dash = (LINUX / "install-dashboard.sh").read_text(encoding="utf-8")
    assert "oh-my-opencode" not in dash
    assert "python3 -m venv" in dash or "venv" in dash
    old = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "oh-my-opencode" not in old
    assert "install-dashboard.sh" in old
    be = (LINUX / "start-frontend.sh").read_text(encoding="utf-8")
    assert "src.daemon" in be
    assert "does not kill" in be.lower() or "backend stays" in be.lower()
