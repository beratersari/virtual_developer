#!/usr/bin/env python3
"""Copy derman-build / derman-plan and skills into an existing OpenCode home.

Does not install the OpenCode CLI. Detects the home, then copies only
``derman-build.md``, ``derman-plan.md``, and ``derman-test.md`` plus
``skills/``. Other OpenCoderman agents (for example gitlab-reviewer)
are left in the zip.

Home detection (first match):

1. ``--opencode-home`` / ``OPENCODE_HOME``
2. ``%USERPROFILE%\\.opencode`` or ``~/.opencode`` when it looks installed
3. Directory of ``opencode`` / ``opencode.exe`` on PATH
   (``.../.opencode/bin/opencode`` → ``.../.opencode``)

Never writes ``~/.config/opencode``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

AGENT_MARKERS = ("derman-build.md", "derman-plan.md", "derman-test.md")
HOME_MARKERS = (
    Path("bin") / "opencode.exe",
    Path("bin") / "opencode",
    Path("opencode.json"),
    Path("agents"),
)


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def looks_like_opencode_home(path: Path) -> bool:
    if not _is_dir(path):
        return False
    return any(_is_file(path / m) or _is_dir(path / m) for m in HOME_MARKERS)


def source_trees(root: Path) -> Optional[Tuple[Path, Path]]:
    """Return (agents, skills) under ``root`` if both exist."""
    for rel in ("opencoderman", "opencode_configs", ""):
        base = root / rel if rel else root
        agents = base / "agents"
        skills = base / "skills"
        if _is_dir(agents) and _is_dir(skills):
            return agents, skills
    return None


def find_source(start: Path) -> Tuple[Path, Path]:
    """Walk from ``start`` (script/zip root) to find agents + skills."""
    seen: set[Path] = set()
    candidates: List[Path] = []
    for raw in (start, start.parent, Path.cwd()):
        try:
            resolved = raw.resolve()
        except OSError:
            resolved = raw
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    for root in candidates:
        found = source_trees(root)
        if found:
            return found
    raise FileNotFoundError(
        "Could not find opencoderman/agents and opencoderman/skills "
        f"(looked next to {start})"
    )


def home_from_binary(binary: Path) -> Optional[Path]:
    """Map an opencode executable path to its install home."""
    try:
        path = binary.resolve()
    except OSError:
        path = binary
    parent = path.parent
    if parent.name.lower() == "bin":
        home = parent.parent
        if home.name.lower() in {".opencode", "opencode"}:
            return home
        if looks_like_opencode_home(home):
            return home
    if looks_like_opencode_home(parent):
        return parent
    return None


def which_opencode(path_entries: Optional[Iterable[str]] = None) -> Optional[Path]:
    names = ("opencode.exe", "opencode.cmd", "opencode")
    if path_entries is None:
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
    for raw in path_entries:
        folder = Path(os.path.expandvars(os.path.expanduser((raw or "").strip().strip('"'))))
        if not _is_dir(folder):
            continue
        for name in names:
            cand = folder / name
            if _is_file(cand):
                return cand
    return None


def default_user_opencode_home() -> Path:
    if os.name == "nt":
        profile = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(profile) / ".opencode"
    return Path.home() / ".opencode"


def find_opencode_home(
    *,
    explicit: Optional[str] = None,
    environ: Optional[Sequence[str]] = None,
    path_entries: Optional[Iterable[str]] = None,
) -> Path:
    if explicit and str(explicit).strip():
        home = Path(os.path.expandvars(os.path.expanduser(str(explicit).strip())))
        if looks_like_opencode_home(home) or _is_dir(home):
            return home
        raise FileNotFoundError(f"OpenCode home not found: {home}")

    env_home = os.environ.get("OPENCODE_HOME", "").strip()
    if env_home:
        home = Path(os.path.expandvars(os.path.expanduser(env_home)))
        if looks_like_opencode_home(home) or _is_dir(home):
            return home

    default = default_user_opencode_home()
    if looks_like_opencode_home(default):
        return default

    binary = which_opencode(path_entries)
    if binary is not None:
        derived = home_from_binary(binary)
        if derived is not None:
            return derived

    raise FileNotFoundError(
        "OpenCode is not installed (no OPENCODE_HOME, no "
        f"{default}, and opencode is not on PATH). "
        "Install OpenCode first, then re-run this script."
    )


def _copy_plan_build_agents(src: Path, dest: Path) -> int:
    """Copy derman-build / derman-plan / derman-test into dest."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for name in AGENT_MARKERS:
        src_file = src / name
        if not _is_file(src_file):
            raise FileNotFoundError(f"required agent missing: {src_file}")
        shutil.copy2(src_file, dest / name)
        count += 1
    return count


def _copy_tree(src: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def install_agents(
    *,
    source_root: Path,
    opencode_home: Optional[Path] = None,
    explicit_home: Optional[str] = None,
) -> Path:
    agents, skills = find_source(source_root)
    home = opencode_home or find_opencode_home(explicit=explicit_home)
    n_agents = _copy_plan_build_agents(agents, home / "agents")
    n_skills = _copy_tree(skills, home / "skills")
    missing = [name for name in AGENT_MARKERS if not (home / "agents" / name).is_file()]
    if missing:
        raise RuntimeError(f"Copy finished but missing {missing} under {home / 'agents'}")
    if n_agents != len(AGENT_MARKERS) or n_skills < 10:
        raise RuntimeError(
            f"Copy too small: agents={n_agents} skills={n_skills} -> {home}"
        )
    print(f"[OK] OpenCode home : {home}")
    print(f"[OK] agents        : {n_agents} files -> {home / 'agents'}")
    print(f"[OK] skills        : {n_skills} files -> {home / 'skills'}")
    return home


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy derman-build, derman-plan, and skills into the OpenCode home."
    )
    parser.add_argument(
        "--source-root",
        default="",
        help="Folder that contains opencoderman/agents and opencoderman/skills",
    )
    parser.add_argument(
        "--opencode-home",
        default="",
        help="Existing OpenCode install directory (default: detect)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    here = Path(__file__).resolve().parent
    source = Path(args.source_root).resolve() if str(args.source_root).strip() else here
    # Script lives in packaging/ in the repo; zip root is the parent.
    if source.name.lower() == "packaging" and source_trees(source.parent):
        source = source.parent
    try:
        install_agents(
            source_root=source,
            explicit_home=str(args.opencode_home).strip() or None,
        )
    except (FileNotFoundError, RuntimeError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
