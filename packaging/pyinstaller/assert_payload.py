"""Assert a frozen Yaver onedir payload has the required layout."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Per-Ubuntu glibc floors. A freeze must not need newer than its target.
UBUNTU_GLIBC_MAX = {
    "18.04": (2, 27),
    "20.04": (2, 31),
    "22.04": (2, 35),
    "24.04": (2, 39),
}

REQUIRED_FILES = (
    ".env.example",
    "START_HERE.txt",
    "VERSION",
    "versions.env",
    "opencoderman.pin",
)

REQUIRED_BUNDLED = (
    "web/dist/index.html",
    "agent/PLAN_PROMPT.md",
    "agent/BUILD_PROMPT.md",
    "agent/REVIEW_PROMPT.md",
)

REQUIRED_OPENCODERMAN = (
    "opencoderman/agents/derman-build.md",
    "opencoderman/agents/derman-plan.md",
    "opencoderman/agents/derman-test.md",
    "opencoderman/agents/derman-reviewer.md",
)
MIN_OPENCODE_SKILLS = 10
FORBIDDEN_DUPLICATE_CONFIGS = (
    "opencode_configs",
    "Install-OpencodeAgents.ps1",
    "install_opencode_agents.py",
)


def exe_name() -> str:
    return "yaver.exe" if os.name == "nt" else "yaver"


def parse_glibc_tuple(text: str) -> tuple[int, ...]:
    parts = text.split(".")
    return tuple(int(p) for p in parts if p.isdigit())


def parse_glibc_versions(text: str) -> list[tuple[int, ...]]:
    found: set[tuple[int, ...]] = set()
    for match in re.finditer(r"GLIBC_(\d+(?:\.\d+)*)", text):
        parsed = parse_glibc_tuple(match.group(1))
        if parsed:
            found.add(parsed)
    return sorted(found)


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def _glibc_dump(path: Path) -> str | None:
    objdump = shutil.which("objdump")
    if objdump:
        proc = subprocess.run(
            [objdump, "-T", str(path)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout
    readelf = shutil.which("readelf")
    if readelf:
        proc = subprocess.run(
            [readelf, "-V", str(path)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
        )
        if proc.returncode == 0:
            return (proc.stdout or "") + (proc.stderr or "")
    return None


def assert_linux_glibc(
    root: Path,
    *,
    max_ver: tuple[int, ...] | None,
    require: bool = False,
) -> list[str]:
    """Fail if any ELF needs a newer GLIBC than *max_ver*."""
    errors: list[str] = []
    if max_ver is None:
        return errors
    elves = [p for p in root.rglob("*") if p.is_file() and _is_elf(p)]
    if not elves:
        if require:
            errors.append("no ELF files found for glibc check")
        return errors
    if not (shutil.which("objdump") or shutil.which("readelf")):
        if require:
            errors.append("objdump/readelf missing; cannot check glibc symbols")
        return errors
    worst: tuple[int, ...] = (0,)
    worst_path = ""
    scanned = 0
    for path in elves:
        dump = _glibc_dump(path)
        if dump is None:
            continue
        scanned += 1
        versions = parse_glibc_versions(dump)
        if not versions:
            continue
        newest = versions[-1]
        if newest > worst:
            worst = newest
            worst_path = str(path.relative_to(root))
    if require and scanned == 0:
        errors.append("could not read glibc symbols from any ELF")
        return errors
    if worst > max_ver:
        pretty = ".".join(str(p) for p in worst)
        ceiling = ".".join(str(p) for p in max_ver)
        errors.append(
            f"{worst_path} needs GLIBC_{pretty} (max {ceiling}). "
            "Freeze this Ubuntu target inside matching ubuntu:X.YY."
        )
    return errors


def assert_payload(
    root: Path,
    *,
    platform: str | None = None,
    max_glibc: tuple[int, ...] | None = None,
    require_glibc_check: bool = False,
) -> list[str]:
    """Return a list of error strings (empty = ok)."""
    errors: list[str] = []
    if not root.is_dir():
        return [f"payload directory missing: {root}"]
    plat = (platform or ("windows" if os.name == "nt" else "linux")).lower()
    binary = "yaver.exe" if plat.startswith("win") else "yaver"
    exe = root / binary
    if not exe.is_file():
        errors.append(f"missing executable: {binary}")
    for rel in REQUIRED_FILES:
        if not (root / rel).is_file():
            errors.append(f"missing {rel}")
    internal = root / "_internal"
    search_roots = [root]
    if internal.is_dir():
        search_roots.append(internal)
    for rel in REQUIRED_BUNDLED:
        if not any((base / rel).is_file() for base in search_roots):
            errors.append(f"missing bundled {rel}")
    if not internal.is_dir():
        errors.append("missing _internal/ (onedir layout required)")
    for rel in REQUIRED_OPENCODERMAN:
        if not (root / rel).is_file():
            errors.append(f"missing {rel}")
    reviewer = root / "opencoderman" / "agents" / "gitlab-reviewer.md"
    if reviewer.is_file():
        errors.append("opencoderman/agents must not include gitlab-reviewer.md")
    ocm = root / "opencoderman"
    if ocm.is_dir():
        extra = sorted(
            p.name for p in ocm.iterdir() if p.name not in {"agents", "skills"}
        )
        if extra:
            errors.append(
                "opencoderman/ must only contain agents/ and skills/ "
                f"(found {extra})"
            )
    ocm_skills = root / "opencoderman" / "skills"
    if not ocm_skills.is_dir():
        errors.append("missing opencoderman/skills/")
    else:
        ocm_mds = list(ocm_skills.rglob("SKILL.md"))
        if len(ocm_mds) < MIN_OPENCODE_SKILLS:
            errors.append(
                f"opencoderman/skills has {len(ocm_mds)} SKILL.md "
                f"(need >= {MIN_OPENCODE_SKILLS})"
            )
    for rel in FORBIDDEN_DUPLICATE_CONFIGS:
        if (root / rel).exists():
            errors.append(f"duplicate OpenCode config must not ship: {rel}")
    if plat.startswith("win"):
        if not (root / "install-agents.bat").is_file():
            errors.append("missing install-agents.bat")
        if (root / "install-agents.sh").is_file():
            errors.append("Windows zip must not include install-agents.sh")
    else:
        if not (root / "install-agents.sh").is_file():
            errors.append("missing install-agents.sh")
        if (root / "install-agents.bat").is_file():
            errors.append("Linux zip must not include install-agents.bat")
        if max_glibc is not None or require_glibc_check:
            errors.extend(
                assert_linux_glibc(
                    root, max_ver=max_glibc, require=require_glibc_check
                )
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert frozen Yaver payload layout")
    parser.add_argument("payload", type=Path)
    parser.add_argument("--platform", default="")
    parser.add_argument(
        "--max-glibc",
        default="",
        help="Highest GLIBC the Linux freeze may need (e.g. 2.27 for Ubuntu 18.04)",
    )
    parser.add_argument(
        "--require-glibc-check",
        action="store_true",
        help="Fail if Linux ELFs cannot be scanned for GLIBC symbols",
    )
    args = parser.parse_args(argv)
    max_glibc = parse_glibc_tuple(args.max_glibc) if args.max_glibc else None
    errors = assert_payload(
        args.payload,
        platform=args.platform or None,
        max_glibc=max_glibc,
        require_glibc_check=args.require_glibc_check,
    )
    if errors:
        for err in errors:
            print(f"FAIL {err}", file=sys.stderr)
        return 1
    print(f"OK payload {args.payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
