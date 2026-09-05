"""OpenCode install goes through the opencoderman submodule."""

from __future__ import annotations

import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OCM = ROOT / "opencoderman"
_SPEC = importlib.util.spec_from_file_location(
    "vd_install_opencode",
    ROOT / "packaging" / "install_opencode.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_install = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_install)
binary_name = _install.binary_name
cli_from_home_zip = _install.cli_from_home_zip
default_opencoderman = _install.default_opencoderman
install_opencode = _install.install_opencode
opencode_versions = _install.opencode_versions
vendor_cli = _install.vendor_cli


@pytest.fixture(autouse=True)
def isolate_jira_agent_artifacts():
    yield


def test_submodule_is_present():
    assert (OCM / "install.py").is_file()
    assert (OCM / "agents" / "gitlab-reviewer.md").is_file()
    assert (OCM / "packaging" / "versions.env").is_file()
    assert (OCM / "skills" / "python" / "SKILL.md").is_file()


def test_opencode_versions_prefer_submodule():
    yaver = (ROOT / "packaging" / "windows" / "versions.env").read_text(encoding="utf-8")
    ocm = (OCM / "packaging" / "versions.env").read_text(encoding="utf-8")
    merged = opencode_versions(ROOT, OCM)
    assert merged["OPENCODE_VERSION"]
    assert f"OPENCODE_VERSION={merged['OPENCODE_VERSION']}" in ocm
    assert f"OPENCODE_VERSION={merged['OPENCODE_VERSION']}" in yaver
    assert merged["OPENCODE_WINDOWS_ASSET"] == "opencode-windows-x64.zip"
    assert merged["OPENCODE_LINUX_ASSET"] == "opencode-linux-x64.tar.gz"


def test_default_opencoderman_path():
    assert default_opencoderman(ROOT) == OCM


def test_vendor_cli_and_home_zip(tmp_path):
    vendor = tmp_path / "vendor"
    (vendor / "bin").mkdir(parents=True)
    fake = vendor / "bin" / binary_name()
    fake.write_bytes(b"fake-opencode")
    assert vendor_cli(vendor) == fake

    zpath = vendor / "opencode-home.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(f"bin/{binary_name()}", b"zip-opencode")
    extracted = cli_from_home_zip(zpath, tmp_path / "extract")
    assert extracted is not None
    assert extracted.read_bytes() == b"zip-opencode"


def test_install_opencode_writes_agents_skills_and_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    vendor = tmp_path / "vendor"
    (vendor / "bin").mkdir(parents=True)
    cli = vendor / "bin" / binary_name()
    cli.write_bytes(b"#!/bin/sh\necho opencode-ok\n")
    if os.name != "nt":
        cli.chmod(0o755)
    rg_name = "rg.exe" if os.name == "nt" else "rg"
    (vendor / "bin" / rg_name).write_bytes(b"rg")

    dest = install_opencode(
        repo_root=ROOT,
        opencoderman_root=OCM,
        vendor_root=vendor,
        user_home=home,
        require_binary=True,
    )
    assert dest.is_file()
    assert dest.name == "gitlab-reviewer.md"
    oc = home / ".opencode"
    assert (oc / "bin" / binary_name()).is_file()
    assert (oc / "agents" / "gitlab-reviewer.md").is_file()
    assert (oc / "agents" / "derman-build.md").is_file()
    assert (oc / "agents" / "derman-plan.md").is_file()
    assert (oc / "skills" / "python" / "SKILL.md").is_file()
    cfg = (oc / "opencode.json").read_text(encoding="utf-8")
    assert '"plugin": []' in cfg
    assert '"autoupdate": false' in cfg
    assert not (home / ".config" / "opencode" / "opencode.json").exists()
    assert (oc / "bin" / rg_name).is_file()


def test_install_opencode_requires_cli_when_asked(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    vendor = tmp_path / "empty-vendor"
    vendor.mkdir()
    with pytest.raises(FileNotFoundError, match="No OpenCode CLI"):
        install_opencode(
            repo_root=ROOT,
            opencoderman_root=OCM,
            vendor_root=vendor,
            user_home=home,
            require_binary=True,
        )
