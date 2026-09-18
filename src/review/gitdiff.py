"""Clone-side git refs and unified diff for inline review threads."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


def _git(workdir: Path, *args: str, timeout: float = 60.0) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def head_sha(workdir: Path) -> str:
    return _git(workdir, "rev-parse", "HEAD")


def merge_base(workdir: Path, target_branch: str) -> str:
    target = (target_branch or "").strip()
    if not target:
        return ""
    for ref in (f"origin/{target}", f"refs/remotes/origin/{target}", target):
        sha = _git(workdir, "merge-base", "HEAD", ref)
        if sha:
            return sha
    return ""


def unified_diff(workdir: Path, base: str, timeout: float = 60.0) -> str:
    if not base:
        return ""
    return _git(workdir, "diff", "--no-color", f"{base}...HEAD", timeout=timeout)


def resolve_workdir(raw: Optional[object]) -> Optional[Path]:
    if raw is None:
        return None
    path = Path(str(raw)).expanduser()
    if path.is_dir() and (path / ".git").exists():
        return path
    if path.is_dir():
        return path
    return None
