#!/usr/bin/env python3
"""Resolve the OpenCoderman submodule commit for a Yaver build / release.

The parent gitlink (``HEAD:opencoderman``) is the pin that shipped with this
Yaver commit. That SHA is written next to installers and onto the GitHub
Release so a later submodule bump cannot rewrite history for an old tag.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional

OCM_GITHUB = "https://github.com/beratersari/opencoderman"
PIN_NAME = "opencoderman.pin"


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def gitlink_sha(repo_root: Path) -> str:
    """SHA recorded in the parent tree (works even if the submodule is empty)."""
    return _run(["git", "rev-parse", "HEAD:opencoderman"], repo_root)


def checkout_sha(repo_root: Path) -> str:
    ocm = repo_root / "opencoderman"
    if not ocm.is_dir():
        return ""
    return _run(["git", "rev-parse", "HEAD"], ocm)


def commit_subject(repo_root: Path, sha: str) -> str:
    ocm = repo_root / "opencoderman"
    if ocm.is_dir():
        subj = _run(["git", "log", "-1", "--format=%s", sha], ocm)
        if subj:
            return subj
    return _run(["git", "log", "-1", "--format=%s", sha], repo_root)


def resolve(repo_root: Path) -> dict[str, str]:
    root = Path(repo_root).resolve()
    commit = gitlink_sha(root) or checkout_sha(root)
    if not commit or len(commit) < 7:
        raise SystemExit(
            "Could not resolve opencoderman commit. "
            "Need a git checkout (gitlink HEAD:opencoderman) or opencoderman/.git"
        )
    short = commit[:7]
    subject = commit_subject(root, commit)
    url = f"{OCM_GITHUB}/commit/{commit}"
    return {
        "commit": commit,
        "short": short,
        "subject": subject,
        "url": url,
        "tree_url": f"{OCM_GITHUB}/tree/{commit}",
    }


def pin_text(info: dict[str, str]) -> str:
    lines = [
        f"OPENCODERMAN_COMMIT={info['commit']}",
        f"OPENCODERMAN_COMMIT_SHORT={info['short']}",
        f"OPENCODERMAN_URL={info['url']}",
        f"OPENCODERMAN_TREE={info['tree_url']}",
    ]
    if info.get("subject"):
        lines.append(f"OPENCODERMAN_SUBJECT={info['subject']}")
    lines.append("")
    return "\n".join(lines)


def write_pin(path: Path, info: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pin_text(info), encoding="utf-8")
    return path


def notes_section(info: dict[str, str], zip_name: str = "") -> str:
    zip_line = f"- Snapshot on this release: `{zip_name}`\n" if zip_name else ""
    subj = info.get("subject") or "(subject unavailable)"
    return (
        "\n## OpenCoderman pin\n\n"
        "This Yaver release was built against this **exact** submodule commit. "
        "Later bumps of `opencoderman` on `develop` do not change this tag.\n\n"
        f"- Commit: [`{info['short']}`]({info['url']}) `{info['commit']}`\n"
        f"- Subject: {subj}\n"
        f"- Tree: {info['tree_url']}\n"
        f"{zip_line}"
    )


def compose_notes(base: Path, info: dict[str, str], zip_name: str, dest: Path) -> Path:
    body = base.read_text(encoding="utf-8") if base.is_file() else ""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body.rstrip() + "\n" + notes_section(info, zip_name), encoding="utf-8")
    return dest


def zip_snapshot(repo_root: Path, dest: Path, info: dict[str, str]) -> Path:
    ocm = Path(repo_root) / "opencoderman"
    if not (ocm / "install.py").is_file():
        raise SystemExit(f"opencoderman tree missing at {ocm} (need submodule checkout)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("opencoderman/" + PIN_NAME, pin_text(info))
        zf.writestr("opencoderman/OPENCODERMAN_COMMIT", info["commit"] + "\n")
        for path in sorted(ocm.rglob("*")):
            if not path.is_file():
                continue
            parts = path.relative_to(ocm).parts
            if ".git" in parts or "__pycache__" in parts:
                continue
            if path.suffix == ".pyc":
                continue
            zf.write(path, Path("opencoderman") / path.relative_to(ocm))
    return dest


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pin the OpenCoderman submodule commit")
    parser.add_argument("--repo-root", default="", help="Yaver repo root (default: two parents up)")
    parser.add_argument("--write-pin", default="", help="Write KEY=VAL pin file here")
    parser.add_argument("--zip", default="", help="Write opencoderman-<short>.zip here (file or dir)")
    parser.add_argument("--compose-notes", default="", help="Base RELEASE_NOTES.md to append to")
    parser.add_argument("--out-notes", default="", help="Where to write composed release notes")
    parser.add_argument("--github-output", action="store_true", help="Append commit/short/zip to GITHUB_OUTPUT")
    args = parser.parse_args(argv)

    here = Path(__file__).resolve().parent
    root = Path(args.repo_root).resolve() if args.repo_root else here.parent
    info = resolve(root)

    zip_path: Optional[Path] = None
    if args.zip:
        z = Path(args.zip)
        dest = z if z.suffix.lower() == ".zip" else z / f"opencoderman-{info['short']}.zip"
        zip_path = zip_snapshot(root, dest, info)

    if args.write_pin:
        write_pin(Path(args.write_pin), info)

    zip_name = zip_path.name if zip_path else f"opencoderman-{info['short']}.zip"
    if args.out_notes:
        compose_notes(
            Path(args.compose_notes) if args.compose_notes else root / "packaging" / "RELEASE_NOTES.md",
            info,
            zip_name if zip_path else zip_name,
            Path(args.out_notes),
        )

    print(f"commit={info['commit']}")
    print(f"short={info['short']}")
    print(f"url={info['url']}")
    if zip_path:
        print(f"zip={zip_path}")
        print(f"zip_name={zip_path.name}")

    if args.github_output:
        gh = os.environ.get("GITHUB_OUTPUT")
        if gh:
            with open(gh, "a", encoding="utf-8") as fh:
                fh.write(f"commit={info['commit']}\n")
                fh.write(f"short={info['short']}\n")
                fh.write(f"url={info['url']}\n")
                fh.write(f"zip_name={zip_name}\n")
                if zip_path:
                    fh.write(f"zip={zip_path.as_posix()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
