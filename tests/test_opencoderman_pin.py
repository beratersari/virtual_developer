"""OpenCoderman submodule pin for releases."""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "opencoderman_pin.py"


def _load():
    spec = importlib.util.spec_from_file_location("opencoderman_pin", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["opencoderman_pin"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_resolve_matches_gitlink():
    pin = _load()
    info = pin.resolve(ROOT)
    assert len(info["commit"]) == 40
    assert info["commit"].startswith(info["short"])
    assert info["url"].endswith(info["commit"])
    gitlink = pin.gitlink_sha(ROOT)
    if gitlink:
        assert info["commit"] == gitlink


def test_write_pin_and_zip(tmp_path: Path):
    pin = _load()
    info = pin.resolve(ROOT)
    dest = tmp_path / "opencoderman.pin"
    pin.write_pin(dest, info)
    text = dest.read_text(encoding="utf-8")
    assert f"OPENCODERMAN_COMMIT={info['commit']}" in text
    assert "OPENCODERMAN_URL=" in text

    zpath = tmp_path / f"opencoderman-{info['short']}.zip"
    pin.zip_snapshot(ROOT, zpath, info)
    assert zpath.is_file()
    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
    assert "opencoderman/install.py" in names
    assert "opencoderman/opencoderman.pin" in names
    assert "opencoderman/OPENCODERMAN_COMMIT" in names
    assert not any(
        n == "opencoderman/.git"
        or n.startswith("opencoderman/.git/")
        for n in names
    )


def test_cli_writes_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pin = _load()
    dest = tmp_path / "opencoderman.pin"
    rc = pin.main(["--repo-root", str(ROOT), "--write-pin", str(dest)])
    assert rc == 0
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8").startswith("OPENCODERMAN_COMMIT=")


def test_workflows_snapshot_opencoderman():
    for rel in (
        ".github/workflows/executables.yml",
        ".github/workflows/windows-dist.yml",
        ".github/workflows/linux-dist.yml",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "opencoderman_pin.py" in text, rel
        assert "opencoderman-*.zip" in text, rel
        assert "dist/RELEASE_NOTES.md" in text, rel
