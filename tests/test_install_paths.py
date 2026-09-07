"""Frozen vs source install/resource roots."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.install_paths import (
    bundled_agent_dir,
    bundled_version_file,
    bundled_web_dist,
    install_root,
    is_frozen,
    resource_root,
)

ROOT = Path(__file__).resolve().parents[1]


def test_unfrozen_roots_are_repo():
    assert is_frozen() is False
    assert resource_root() == ROOT
    assert install_root() == ROOT
    assert bundled_version_file() == ROOT / "VERSION"
    assert bundled_web_dist() == ROOT / "web" / "dist"
    assert bundled_agent_dir() == ROOT / "agent"
    assert bundled_version_file().is_file()
    assert (bundled_agent_dir() / "BUILD_PROMPT.md").is_file()


def test_frozen_splits_install_and_resource(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    exe_dir = tmp_path / "install"
    exe_dir.mkdir()
    exe = exe_dir / "yaver.exe"
    exe.write_bytes(b"")
    meipass = tmp_path / "_internal"
    meipass.mkdir()

    import src.install_paths as paths

    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(exe))

    assert paths.is_frozen() is True
    assert paths.install_root() == exe_dir
    assert paths.resource_root() == meipass
    assert paths.bundled_web_dist() == meipass / "web" / "dist"
    assert paths.bundled_agent_dir() == meipass / "agent"
    assert paths.bundled_version_file() == meipass / "VERSION"


def test_frozen_without_meipass_uses_exe_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    exe = tmp_path / "yaver"
    exe.write_bytes(b"")
    import src.install_paths as paths

    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable", str(exe))
    if hasattr(paths.sys, "_MEIPASS"):
        monkeypatch.delattr(paths.sys, "_MEIPASS", raising=False)

    assert paths.resource_root() == tmp_path
    assert paths.install_root() == tmp_path


def test_dotenv_bootstrap_includes_install_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import os

    from src.config import bootstrap_dotenv_into_environ
    import src.install_paths as paths

    (tmp_path / ".env").write_text(
        "YAVER_TEST_BOOTSTRAP_KEY=from-install-root\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "install_root", lambda: tmp_path)
    monkeypatch.delenv("YAVER_TEST_BOOTSTRAP_KEY", raising=False)

    applied = bootstrap_dotenv_into_environ()
    assert applied >= 1
    assert os.environ.get("YAVER_TEST_BOOTSTRAP_KEY") == "from-install-root"
    monkeypatch.delenv("YAVER_TEST_BOOTSTRAP_KEY", raising=False)
