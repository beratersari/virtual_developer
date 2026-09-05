#!/usr/bin/env python3
"""Install OpenCode through the opencoderman submodule.

Yaver keeps Codex, the dashboard, and start scripts. The OpenCode CLI,
agents, skills, PATH rewrite, and stock ``opencode.json`` come from
``opencoderman/install.py``.

CLI sources (first hit wins):

1. ``opencoderman/vendor/bin/<os>/opencode[.exe]``
2. ``<vendor>/bin/opencode[.exe]``
3. ``<vendor>/opencode-home.zip`` (``bin/opencode`` inside)

``--online`` runs ``opencoderman/packaging/build_artifact.py --in-place``
first (official GitHub release, pinned in the submodule ``versions.env``).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from types import ModuleType


def repo_root_from_here() -> Path:
    return Path(__file__).resolve().parents[1]


def default_opencoderman(repo_root: Path) -> Path:
    return Path(repo_root) / "opencoderman"


def load_install(opencoderman_root: Path) -> ModuleType:
    path = Path(opencoderman_root) / "install.py"
    if not path.is_file():
        raise FileNotFoundError(
            f"opencoderman install.py missing at {path}. "
            "Run: git submodule update --init --recursive"
        )
    spec = importlib.util.spec_from_file_location("opencoderman_install", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_versions_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def opencode_versions(repo_root: Path, opencoderman_root: Path | None = None) -> dict[str, str]:
    """OpenCode pins from the submodule, overlaid on Yaver versions.env."""
    ocm = Path(opencoderman_root) if opencoderman_root else default_opencoderman(repo_root)
    merged = read_versions_file(Path(repo_root) / "packaging" / "windows" / "versions.env")
    merged.update(read_versions_file(ocm / "packaging" / "versions.env"))
    return merged


def binary_name() -> str:
    return "opencode.exe" if os.name == "nt" else "opencode"


def find_named_binary(root: Path, name: str) -> Path | None:
    direct = Path(root) / name
    if direct.is_file():
        return direct
    if not Path(root).is_dir():
        return None
    for dirpath, _dirnames, filenames in os.walk(root):
        if name in filenames:
            return Path(dirpath) / name
    return None


def existing_ocm_binary(install_mod: ModuleType, opencoderman_root: Path) -> Path | None:
    found = install_mod.vendor_binary(Path(opencoderman_root))
    return Path(found) if found is not None else None


def vendor_cli(vendor_root: Path) -> Path | None:
    name = binary_name()
    vendor_bin = Path(vendor_root) / "bin"
    for candidate in (vendor_bin / name, vendor_bin / "opencode.exe", vendor_bin / "opencode"):
        if candidate.is_file():
            return candidate
    return None


def cli_from_home_zip(zip_path: Path, dest_dir: Path) -> Path | None:
    if not zip_path.is_file():
        return None
    name = binary_name()
    alt = "opencode.exe" if name == "opencode" else "opencode"
    with zipfile.ZipFile(zip_path) as zf:
        match = None
        for entry in zf.namelist():
            base = Path(entry).name.lower()
            if base == name.lower():
                match = entry
                break
            if match is None and base == alt.lower():
                match = entry
        if match is None:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        zf.extract(match, dest_dir)
        return dest_dir / match


def resolve_cli(
    *,
    install_mod: ModuleType,
    opencoderman_root: Path,
    vendor_root: Path,
    extract_dir: Path,
) -> Path | None:
    existing = existing_ocm_binary(install_mod, opencoderman_root)
    if existing is not None:
        return existing
    from_vendor = vendor_cli(vendor_root)
    if from_vendor is not None:
        return from_vendor
    return cli_from_home_zip(Path(vendor_root) / "opencode-home.zip", extract_dir)


def stage_pack(opencoderman_root: Path, cli_src: Path | None, work_dir: Path) -> Path:
    """Return a pack root that install.py can consume (agents, skills, optional CLI)."""
    ocm = Path(opencoderman_root)
    if cli_src is None:
        return ocm
    try:
        if cli_src.resolve().is_relative_to(ocm.resolve()):  # type: ignore[attr-defined]
            return ocm
    except AttributeError:
        ocm_s = os.path.normcase(str(ocm.resolve()))
        cli_s = os.path.normcase(str(cli_src.resolve()))
        if cli_s.startswith(ocm_s + os.sep):
            return ocm
    dest = Path(work_dir) / "pack"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name in ("agents", "skills"):
        src = ocm / name
        if not src.is_dir():
            raise FileNotFoundError(f"missing {src}")
        shutil.copytree(src, dest / name)
    tag = "windows" if os.name == "nt" else "linux"
    if sys.platform == "darwin":
        import platform

        machine = platform.machine().lower()
        tag = "darwin-arm64" if machine in {"arm64", "aarch64"} else "darwin-x64"
    target = dest / "vendor" / "bin" / tag / binary_name()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cli_src, target)
    if os.name != "nt":
        target.chmod(target.stat().st_mode | 0o111)
    return dest


def vendor_extra_names() -> tuple[tuple[str, str], ...]:
    if os.name == "nt":
        return (("rg.exe", "rg.exe"), ("glab.exe", "glab.exe"))
    return (("rg", "rg"), ("glab", "glab"))


def apply_yaver_extras(*, user_home: Path | None, vendor_root: Path) -> None:
    """Seed ripgrep / glab and disable models.dev fetch (Yaver offline extras)."""
    if user_home is not None:
        home = Path(user_home)
    elif os.name == "nt" and os.environ.get("USERPROFILE"):
        home = Path(os.environ["USERPROFILE"])
    else:
        home = Path.home()
    oc_bin = home / ".opencode" / "bin"
    cache_bin = home / ".cache" / "opencode" / "bin"
    vendor_bin = Path(vendor_root) / "bin"
    for src_name, dest_name in vendor_extra_names():
        src = vendor_bin / src_name
        if not src.is_file():
            continue
        if dest_name.startswith("rg"):
            cache_bin.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, cache_bin / dest_name)
        oc_bin.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, oc_bin / dest_name)
        if os.name != "nt":
            dest = oc_bin / dest_name
            dest.chmod(dest.stat().st_mode | 0o111)
        print(f"[OK] Seeded {dest_name}")
    os.environ["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    if os.name == "nt" and user_home is None:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
            )
            try:
                winreg.SetValueEx(key, "OPENCODE_DISABLE_MODELS_FETCH", 0, winreg.REG_SZ, "1")
            finally:
                key.Close()
            print("[OK] User env OPENCODE_DISABLE_MODELS_FETCH=1")
        except OSError as exc:
            print(f"[WARN] could not set user env OPENCODE_DISABLE_MODELS_FETCH: {exc}")


def run_online_vendor(opencoderman_root: Path) -> None:
    script = Path(opencoderman_root) / "packaging" / "build_artifact.py"
    if not script.is_file():
        raise FileNotFoundError(f"missing {script}")
    import subprocess

    print("Fetching OpenCode CLI via opencoderman (network)...")
    subprocess.run(
        [sys.executable, str(script), "--in-place", "--root", str(opencoderman_root)],
        check=True,
    )


def install_opencode(
    *,
    repo_root: Path,
    opencoderman_root: Path | None = None,
    vendor_root: Path | None = None,
    user_home: Path | None = None,
    require_binary: bool = True,
    online: bool = False,
) -> Path:
    repo_root = Path(repo_root).expanduser().resolve()
    ocm = Path(opencoderman_root) if opencoderman_root else default_opencoderman(repo_root)
    ocm = ocm.expanduser().resolve()
    vendor = Path(vendor_root) if vendor_root else repo_root / "vendor"
    if online:
        run_online_vendor(ocm)
    install_mod = load_install(ocm)
    with tempfile.TemporaryDirectory(prefix="vd-ocm-") as tmp:
        cli = resolve_cli(
            install_mod=install_mod,
            opencoderman_root=ocm,
            vendor_root=vendor,
            extract_dir=Path(tmp) / "zip-cli",
        )
        if require_binary and cli is None:
            raise FileNotFoundError(
                "No OpenCode CLI found. Need one of: "
                f"{ocm / 'vendor' / 'bin'}, {vendor / 'bin' / binary_name()}, "
                f"or {vendor / 'opencode-home.zip'}. "
                "Use the CI zip, or run install-opencode-online "
                "(python packaging/install_opencode.py --online)."
            )
        pack = stage_pack(ocm, cli, Path(tmp))
        dest = install_mod.install(
            pack,
            user_home=user_home,
            require_binary=require_binary if cli is not None else False,
        )
    apply_yaver_extras(user_home=user_home, vendor_root=vendor)
    return Path(dest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install OpenCode using the opencoderman submodule (CLI + agents + skills)."
    )
    parser.add_argument("--repo-root", default=str(repo_root_from_here()))
    parser.add_argument("--opencoderman-root", default="")
    parser.add_argument("--vendor-root", default="")
    parser.add_argument("--user-home", default="", help="Override home (tests / CI)")
    parser.add_argument(
        "--require-binary",
        action="store_true",
        help="Fail if no vendored OpenCode CLI can be staged",
    )
    parser.add_argument(
        "--online",
        action="store_true",
        help="Download the pinned CLI into the submodule via build_artifact.py --in-place",
    )
    args = parser.parse_args(argv)
    user_home = Path(args.user_home).expanduser() if str(args.user_home).strip() else None
    ocm = Path(args.opencoderman_root) if str(args.opencoderman_root).strip() else None
    vendor = Path(args.vendor_root) if str(args.vendor_root).strip() else None
    try:
        dest = install_opencode(
            repo_root=Path(args.repo_root),
            opencoderman_root=ocm,
            vendor_root=vendor,
            user_home=user_home,
            require_binary=bool(args.require_binary) or bool(args.online),
            online=bool(args.online),
        )
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] OpenCode ready via opencoderman: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
