"""Offline Linux installers extract vendor/ into HOME without the network."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    """No-op: conftest walks ``.jira-agent`` on /mnt/c and can stall WSL 9p."""
    yield


ROOT = Path(__file__).resolve().parents[1]


def _fake_bin_text(name: str) -> bytes:
    return f"#!/bin/sh\necho {name}-ok\n".encode("utf-8")


def _write_opencode_home_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("bin/opencode", _fake_bin_text("opencode"))
        zf.writestr("bin/glab", _fake_bin_text("glab"))
        zf.writestr(
            "opencode.json",
            '{\n  "autoupdate": false,\n  "plugin": []\n}\n',
        )


def _write_codex_tar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _fake_bin_text("codex")
    info = tarfile.TarInfo(name="codex-x86_64-unknown-linux-musl")
    info.size = len(data)
    info.mode = 0o755
    with tarfile.open(path, "w:gz") as tf:
        tf.addfile(info, io.BytesIO(data))


def test_offline_install_backends_and_codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    vendor = tmp_path / "vendor"
    _write_opencode_home_zip(vendor / "opencode-home.zip")
    _write_codex_tar(vendor / "codex-x86_64-unknown-linux-musl.tar.gz")
    (vendor / "bin").mkdir()
    (vendor / "bin" / "opencode").write_bytes(_fake_bin_text("opencode"))
    (vendor / "bin" / "opencode").chmod(0o755)
    (vendor / "bin" / "codex").write_bytes(_fake_bin_text("codex"))
    (vendor / "bin" / "codex").chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{home / '.local' / 'bin'}:{env.get('PATH', '')}"
    env["VD_REPO_ROOT"] = str(tmp_path)

    proc = subprocess.run(
        ["bash", str(ROOT / "packaging" / "linux" / "install-backends.sh"), "opencode"],
        cwd=str(tmp_path),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    oc = home / ".opencode" / "bin" / "opencode"
    assert oc.is_file(), proc.stdout
    assert oc.stat().st_mode & stat.S_IXUSR
    cfg = home / ".opencode" / "opencode.json"
    assert cfg.is_file()
    assert '"plugin": []' in cfg.read_text(encoding="utf-8")
    assert (home / ".opencode" / "agents" / "gitlab-reviewer.md").is_file()
    assert not (home / ".config" / "opencode" / "opencode.json").exists()

    proc2 = subprocess.run(
        ["bash", str(ROOT / "packaging" / "linux" / "install-codex.sh")],
        cwd=str(tmp_path),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc2.returncode == 0, proc2.stdout + "\n" + proc2.stderr
    codex = home / ".local" / "bin" / "codex"
    assert codex.is_file(), proc2.stdout
    assert "codex-ok" in subprocess.check_output([str(codex)], text=True)


def test_assert_payload_script_accepts_minimal_tree(tmp_path):
    p = tmp_path / "payload"
    for rel in (
        "install-dashboard.sh",
        "install-backends.sh",
        "install-codex.sh",
        "start.sh",
        "start-backend.sh",
        "start-frontend.sh",
        "start-opencode.sh",
        "start-opencode-serve.sh",
        "stop.sh",
        "cli.py",
        "src/daemon.py",
        "web/dist/index.html",
        "web/dist/assets/index-abc.js",
        "vendor/opencode-home.zip",
        "vendor/bin/opencode",
        "opencoderman/install.py",
        "opencoderman.pin",
        "opencoderman/agents/gitlab-reviewer.md",
        "opencoderman/vendor/bin/linux/opencode",
        "packaging/install_opencode.py",
        "vendor/bin/glab",
        "vendor/bin/codex",
        "vendor/python-wheels/.keep",
        "vendor/codex-x86_64-unknown-linux-musl.tar.gz",
        "packaging/linux/opencode.json",
        "packaging/windows/serve_frontend.py",
    ):
        dest = p / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("x\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(ROOT / "packaging" / "linux" / "assert-payload.sh"), str(p)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Payload layout OK" in proc.stdout
