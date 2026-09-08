#!/usr/bin/env python3
"""Freeze Yaver into a Windows/Linux onedir executable.

Requires: Python 3.10+, PyInstaller (see versions.env), and a built SPA
at ``web/dist/index.html``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SPEC = HERE / "yaver.spec"
VERSIONS = HERE / "versions.env"


def _read_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    if not VERSIONS.is_file():
        return out
    for raw in VERSIONS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        out[key.strip()] = val.strip()
    return out


def _product_version() -> str:
    env = (os.environ.get("VD_PRODUCT_VERSION") or "").strip()
    if env:
        return env
    vf = ROOT / "VERSION"
    if vf.is_file():
        return vf.read_text(encoding="utf-8").strip()
    return "0.0.0-dev"


def _platform_slug() -> str:
    return "windows-x64" if os.name == "nt" else "linux-x64"


def _exe_name() -> str:
    return "yaver.exe" if os.name == "nt" else "yaver"


_SKIP_DIR_NAMES = {".git", "__pycache__", ".pytest_cache"}


def _copy_tree_filtered(src: Path, dest: Path) -> int:
    """Copy ``src`` → ``dest`` skipping VCS/cache dirs. Returns file count."""
    if not src.is_dir():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in src.rglob("*"):
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file():
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def stage_opencode_configs(bundled: Path, *, repo_root: Path | None = None) -> Path:
    """Copy derman-build / derman-plan and skills next to the exe."""
    root = repo_root or ROOT
    agents = root / "opencoderman" / "agents"
    skills = root / "opencoderman" / "skills"
    if not agents.is_dir() or not skills.is_dir():
        raise FileNotFoundError(
            "opencoderman/agents and opencoderman/skills are required "
            "(init the submodule: git submodule update --init --recursive)"
        )
    dest = bundled / "opencode_configs"
    if dest.exists():
        shutil.rmtree(dest)
    dest_agents = dest / "agents"
    dest_agents.mkdir(parents=True)
    n_agents = 0
    for name in ("derman-build.md", "derman-plan.md"):
        src = agents / name
        if not src.is_file():
            raise FileNotFoundError(f"required agent missing: {src}")
        shutil.copy2(src, dest_agents / name)
        n_agents += 1
    n_skills = _copy_tree_filtered(skills, dest / "skills")
    if n_agents != 2 or n_skills < 10:
        raise RuntimeError(
            f"opencode_configs too small: agents={n_agents} skills={n_skills}"
        )
    return dest


def stage_opencoderman(bundled: Path, *, repo_root: Path | None = None) -> Path:
    """Copy the OpenCoderman tree (no .git) next to the exe."""
    root = repo_root or ROOT
    src = root / "opencoderman"
    if not (src / "agents").is_dir() or not (src / "skills").is_dir():
        raise FileNotFoundError(
            "opencoderman/agents and opencoderman/skills are required "
            "(init the submodule: git submodule update --init --recursive)"
        )
    dest = bundled / "opencoderman"
    if dest.exists():
        shutil.rmtree(dest)
    n = _copy_tree_filtered(src, dest)
    if n < 12:
        raise RuntimeError(f"opencoderman tree too small: files={n}")
    if not (dest / "agents" / "derman-build.md").is_file():
        raise RuntimeError("opencoderman/agents/derman-build.md missing after copy")
    if not (dest / "agents" / "derman-plan.md").is_file():
        raise RuntimeError("opencoderman/agents/derman-plan.md missing after copy")
    return dest


def stage_agent_installers(bundled: Path) -> None:
    """Ship the detect-and-copy helpers next to the binary."""
    bat = ROOT / "install-opencode-agents.bat"
    ps1 = ROOT / "packaging" / "windows" / "Install-OpencodeAgents.ps1"
    sh = ROOT / "install-opencode-agents.sh"
    py = ROOT / "packaging" / "install_opencode_agents.py"
    if bat.is_file():
        shutil.copy2(bat, bundled / "install-opencode-agents.bat")
    if ps1.is_file():
        shutil.copy2(ps1, bundled / "Install-OpencodeAgents.ps1")
    if sh.is_file():
        dest_sh = bundled / "install-opencode-agents.sh"
        shutil.copy2(sh, dest_sh)
        if os.name != "nt":
            dest_sh.chmod(dest_sh.stat().st_mode | 0o111)
    if py.is_file():
        shutil.copy2(py, bundled / "install_opencode_agents.py")


def _archive(src_dir: Path, dest_base: Path) -> list[Path]:
    """Write zip (and tar.gz on POSIX) of ``src_dir`` next to ``dest_base``."""
    written: list[Path] = []
    # dest_base is like yaver-windows-x64-0.2.0 — Path.with_suffix would
    # treat ".0" as the extension and produce a truncated name.
    zip_path = dest_base.parent / f"{dest_base.name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in src_dir.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(src_dir.parent))
    written.append(zip_path)
    if os.name != "nt":
        tar_base = str(dest_base)
        shutil.make_archive(tar_base, "gztar", root_dir=src_dir.parent, base_dir=src_dir.name)
        written.append(Path(tar_base + ".tar.gz"))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the standalone Yaver executable")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    parser.add_argument("--dist-name", default="")
    parser.add_argument("--clean", action="store_true", help="Wipe previous PyInstaller work dirs")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 10):
        print("Python 3.10+ is required to freeze Yaver", file=sys.stderr)
        return 1
    if not SPEC.is_file():
        print(f"Missing spec: {SPEC}", file=sys.stderr)
        return 1
    spa = ROOT / "web" / "dist" / "index.html"
    if not spa.is_file():
        print(
            "web/dist is missing — build the SPA first: cd web && npm ci && npm run build",
            file=sys.stderr,
        )
        return 1

    versions = _read_versions()
    mode = (versions.get("PYINSTALLER_MODE") or "onedir").strip().lower()
    if mode != "onedir":
        print(f"Only onedir is supported (got PYINSTALLER_MODE={mode})", file=sys.stderr)
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        pin = versions.get("PYINSTALLER_VERSION") or "6.16.0"
        print(
            f"PyInstaller is not installed. pip install pyinstaller=={pin}",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "pyinstaller"
    dist_work = ROOT / "dist" / "pyinstaller"
    if args.clean:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(dist_work, ignore_errors=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(dist_work),
        "--workpath",
        str(work),
        str(SPEC),
    ]
    print(" ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    bundled = dist_work / "yaver"
    exe = bundled / _exe_name()
    if not exe.is_file():
        print(f"Freeze finished but {exe} is missing", file=sys.stderr)
        return 1
    if os.name != "nt":
        exe.chmod(exe.stat().st_mode | 0o111)

    shutil.copy2(ROOT / ".env.example", bundled / ".env.example")
    shutil.copy2(HERE / "START_HERE.txt", bundled / "START_HERE.txt")
    shutil.copy2(ROOT / "VERSION", bundled / "VERSION")
    shutil.copy2(HERE / "versions.env", bundled / "versions.env")
    pin_script = ROOT / "packaging" / "opencoderman_pin.py"
    if pin_script.is_file():
        subprocess.run(
            [
                sys.executable,
                str(pin_script),
                "--repo-root",
                str(ROOT),
                "--write-pin",
                str(bundled / "opencoderman.pin"),
            ],
            check=True,
            cwd=str(ROOT),
        )
    stage_opencode_configs(bundled)
    stage_opencoderman(bundled)
    stage_agent_installers(bundled)

    version = _product_version()
    version_safe = version.replace("+", ".")
    dist_name = (
        args.dist_name
        or (os.environ.get("VD_DIST_NAME") or "").strip()
        or f"yaver-{_platform_slug()}-{version_safe}"
    )

    payload = out_dir / "stage" / dist_name
    if payload.exists():
        shutil.rmtree(payload)
    payload.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundled, payload)

    archives = _archive(payload, out_dir / dist_name)
    print(f"payload={payload}")
    for path in archives:
        print(f"archive={path}")
    print(f"exe={payload / _exe_name()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
