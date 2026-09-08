"""Assert a frozen Yaver onedir payload has the required layout."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

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
)

REQUIRED_OPENCODE_CONFIGS = (
    "opencode_configs/agents/derman-build.md",
    "opencode_configs/agents/derman-plan.md",
)
REQUIRED_OPENCODERMAN = (
    "opencoderman/agents/derman-build.md",
    "opencoderman/agents/derman-plan.md",
)
MIN_OPENCODE_SKILLS = 10


def exe_name() -> str:
    return "yaver.exe" if os.name == "nt" else "yaver"


def assert_payload(root: Path, *, platform: str | None = None) -> list[str]:
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
    for rel in REQUIRED_OPENCODE_CONFIGS:
        if not (root / rel).is_file():
            errors.append(f"missing {rel}")
    skills = root / "opencode_configs" / "skills"
    if not skills.is_dir():
        errors.append("missing opencode_configs/skills/")
    else:
        skill_mds = list(skills.rglob("SKILL.md"))
        if len(skill_mds) < MIN_OPENCODE_SKILLS:
            errors.append(
                f"opencode_configs/skills has {len(skill_mds)} SKILL.md "
                f"(need >= {MIN_OPENCODE_SKILLS})"
            )
    for rel in REQUIRED_OPENCODERMAN:
        if not (root / rel).is_file():
            errors.append(f"missing {rel}")
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
    if plat.startswith("win"):
        if not (root / "install-opencode-agents.bat").is_file():
            errors.append("missing install-opencode-agents.bat")
        if not (root / "Install-OpencodeAgents.ps1").is_file():
            errors.append("missing Install-OpencodeAgents.ps1")
    else:
        if not (root / "install-opencode-agents.sh").is_file():
            errors.append("missing install-opencode-agents.sh")
        if not (root / "install_opencode_agents.py").is_file():
            errors.append("missing install_opencode_agents.py")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert frozen Yaver payload layout")
    parser.add_argument("payload", type=Path)
    parser.add_argument("--platform", default="")
    args = parser.parse_args(argv)
    errors = assert_payload(args.payload, platform=args.platform or None)
    if errors:
        for err in errors:
            print(f"FAIL {err}", file=sys.stderr)
        return 1
    print(f"OK payload {args.payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
