"""Standalone executable packaging: spec, versions, CI, payload assert."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "pyinstaller"
WORKFLOW = ROOT / ".github" / "workflows" / "executables.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_freeze_config_files_exist():
    for rel in (
        "versions.env",
        "yaver.spec",
        "entrypoint.py",
        "runtime_hook.py",
        "build.py",
        "assert_payload.py",
        "START_HERE.txt",
        "README.md",
    ):
        assert (PKG / rel).is_file(), rel
    assert WORKFLOW.is_file()


def test_versions_env_pins():
    text = (PKG / "versions.env").read_text(encoding="utf-8")
    for key in (
        "PYTHON_VERSION=",
        "NODE_VERSION=",
        "PYINSTALLER_VERSION=",
        "PYINSTALLER_MODE=onedir",
    ):
        assert key in text, key
    assert "PYINSTALLER_MODE=onefile" not in text


def test_spec_is_onedir_and_bundles_runtime_files():
    spec = (PKG / "yaver.spec").read_text(encoding="utf-8")
    assert "COLLECT(" in spec
    assert 'name="yaver"' in spec
    assert "entrypoint.py" in spec
    assert "runtime_hook.py" in spec
    assert "VERSION" in spec
    assert ".env.example" in spec
    assert '"agent"' in spec or "'agent'" in spec
    assert "web/dist" in spec
    assert "exclude_binaries=True" in spec
    for banned in ("celery", "redis"):
        assert banned in spec  # listed in excludes
    assert "onefile" not in spec.lower() or "Do not switch this spec to onefile" in spec


def test_spec_excludes_unused_heavy_deps():
    spec = (PKG / "yaver.spec").read_text(encoding="utf-8")
    block_start = spec.index("excludes")
    block = spec[block_start : spec.index("]", block_start)]
    assert '"celery"' in block
    assert '"redis"' in block


def test_entrypoint_uses_cli_and_freeze_support():
    text = (PKG / "entrypoint.py").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support" in text
    assert "from cli import cli" in text


def test_runtime_hook_chdirs_when_frozen():
    text = (PKG / "runtime_hook.py").read_text(encoding="utf-8")
    assert "os.chdir" in text
    assert "YAVER_INSTALL_ROOT" in text
    assert "PYTHONUTF8" in text


def test_workflow_builds_both_platforms():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "windows-latest" in text
    assert "ubuntu-latest" in text
    assert "packaging/pyinstaller/versions.env" in text
    assert "packaging/pyinstaller/build.py" in text
    assert "packaging/pyinstaller/assert_payload.py" in text
    assert "yaver.exe" in text
    assert "--version" in text
    assert "--help" in text
    assert "npm run build" in text
    assert "pyinstaller==" in text
    assert "softprops/action-gh-release" in text
    assert "packaging/RELEASE_NOTES.md" in text
    assert "windows-dist.yml" in text or "does not replace" in text.lower() or "Additive" in text
    assert "Upload zip archive" in text
    assert "Upload tar.gz archive (Linux)" in text


def test_assert_payload_accepts_onedir(tmp_path: Path):
    ap = _load("yaver_assert_payload", PKG / "assert_payload.py")
    payload = tmp_path / "yaver-windows-x64-0.2.0"
    internal = payload / "_internal"
    (internal / "web" / "dist").mkdir(parents=True)
    (internal / "agent").mkdir(parents=True)
    (internal / "web" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    (internal / "agent" / "PLAN_PROMPT.md").write_text("plan", encoding="utf-8")
    (internal / "agent" / "BUILD_PROMPT.md").write_text("build", encoding="utf-8")
    (payload / "yaver.exe").write_bytes(b"MZ")
    (payload / ".env.example").write_text("JIRA_HOST=\n", encoding="utf-8")
    (payload / "START_HERE.txt").write_text("start", encoding="utf-8")
    (payload / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (payload / "versions.env").write_text("PYINSTALLER_MODE=onedir\n", encoding="utf-8")
    assert ap.assert_payload(payload, platform="windows") == []


def test_assert_payload_reports_missing(tmp_path: Path):
    ap = _load("yaver_assert_payload_missing", PKG / "assert_payload.py")
    empty = tmp_path / "empty"
    empty.mkdir()
    errors = ap.assert_payload(empty, platform="linux")
    assert any("yaver" in e for e in errors)
    assert any("_internal" in e for e in errors)
    assert any(".env.example" in e for e in errors)


def test_archive_name_keeps_patch_version(tmp_path: Path):
    build = _load("yaver_build_archive", PKG / "build.py")
    src = tmp_path / "yaver-windows-x64-0.2.0"
    src.mkdir()
    (src / "yaver.exe").write_bytes(b"x")
    written = build._archive(src, tmp_path / "yaver-windows-x64-0.2.0")
    names = {p.name for p in written}
    assert "yaver-windows-x64-0.2.0.zip" in names
    assert "yaver-windows-x64-0.2.zip" not in names


def test_build_script_requires_spa_and_onedir():
    text = (PKG / "build.py").read_text(encoding="utf-8")
    assert "web/dist" in text
    assert "PYINSTALLER_MODE" in text
    assert "onedir" in text
    assert "--out-dir" in text
    assert 'f"{dest_base.name}.zip"' in text
    assert "dest_base.with_suffix" not in text


def test_tag_workflows_share_release_notes():
    notes = ROOT / "packaging" / "RELEASE_NOTES.md"
    changelog = ROOT / "CHANGELOG.md"
    assert notes.is_file()
    assert changelog.is_file()
    assert "yaver-windows-x64-" in notes.read_text(encoding="utf-8")
    assert ".env.example" in notes.read_text(encoding="utf-8")
    for rel in (
        ".github/workflows/executables.yml",
        ".github/workflows/windows-dist.yml",
        ".github/workflows/linux-dist.yml",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "softprops/action-gh-release" in text, rel
        assert "packaging/RELEASE_NOTES.md" in text, rel


def test_start_here_does_not_claim_opencode_is_bundled():
    text = (PKG / "START_HERE.txt").read_text(encoding="utf-8")
    assert "OpenCode" in text
    assert "not" in text.lower()
    assert ".env.example" in text
    assert "yaver start" in text or "yaver.exe start" in text
