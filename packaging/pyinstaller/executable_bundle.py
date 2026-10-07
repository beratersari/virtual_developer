"""Pack the five versioned executable zips into one release file.

A tagged GitHub release attaches ``yaver-executables-<version>.zip``.
The Ubuntu zips stay inside that file. The office site reads the version
from these names and still serves each platform on its own.

``RELEASE_NOTES.txt`` is the ``# Yaver <version>`` section from
``packaging/RELEASE_NOTES.md``. The office site shows that text as the
release note for the version.

Keep the names aligned with ``yaver_releases.platforms`` in the
yaver-releases repository.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$")
_VERSIONED: tuple[tuple[str, str], ...] = (
    ("windows", "yaver-windows-x64-{version}.zip"),
    ("ubuntu-18.04", "yaver-linux-x64-ubuntu-18.04-{version}.zip"),
    ("ubuntu-20.04", "yaver-linux-x64-ubuntu-20.04-{version}.zip"),
    ("ubuntu-22.04", "yaver-linux-x64-ubuntu-22.04-{version}.zip"),
    ("ubuntu-24.04", "yaver-linux-x64-ubuntu-24.04-{version}.zip"),
)
_LATEST = {
    "yaver-windows-latest.zip",
    "yaver-ubuntu-18.04-latest.zip",
    "yaver-ubuntu-20.04-latest.zip",
    "yaver-ubuntu-22.04-latest.zip",
    "yaver-ubuntu-24.04-latest.zip",
}
_NOTE_NAME = "RELEASE_NOTES.txt"
_SECTION = re.compile(r"^# Yaver (\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?)\s*$")


class BundleError(ValueError):
    """The input directory is not the five versioned executable zips."""


def bundle_name(version: str) -> str:
    return f"yaver-executables-{version}.zip"


def match_versioned(filename: str) -> tuple[str, str] | None:
    """Return ``(platform, version)`` for a versioned executable zip name."""
    base = Path(filename or "").name
    found: list[tuple[str, str]] = []
    for platform, pattern in _VERSIONED:
        prefix, suffix = pattern.split("{version}")
        if not base.startswith(prefix) or not base.endswith(suffix):
            continue
        middle = base[len(prefix) : len(base) - len(suffix)]
        if _VERSION.fullmatch(middle):
            found.append((platform, middle))
    if len(found) == 1:
        return found[0]
    return None


def collect_versioned_zips(root: Path) -> tuple[str, list[Path]]:
    """Find the five versioned zips under ``root``. Ignore ``*-latest.zip``."""
    if not root.is_dir():
        raise BundleError(f"{root} is not a directory.")
    found: dict[str, Path] = {}
    version = ""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".zip":
            continue
        if path.name in _LATEST or path.name.endswith("-latest.zip"):
            continue
        if path.name.startswith("yaver-executables-"):
            continue
        matched = match_versioned(path.name)
        if matched is None:
            raise BundleError(f"{path.name} is not a versioned Yaver executable zip.")
        platform, member_version = matched
        if platform in found:
            raise BundleError(f"Two files for {platform}.")
        if version and member_version != version:
            raise BundleError("These file names use more than one version.")
        version = member_version
        found[platform] = path
    missing = [
        pattern.format(version=version or "X.Y.Z")
        for platform, pattern in _VERSIONED
        if platform not in found
    ]
    if missing:
        raise BundleError("Missing " + ", ".join(missing) + ".")
    ordered = [found[platform] for platform, _pattern in _VERSIONED]
    return version, ordered


def notes_path() -> Path:
    """``packaging/RELEASE_NOTES.md`` next to this script."""
    return Path(__file__).resolve().parents[1] / "RELEASE_NOTES.md"


def release_section(version: str, path: Path) -> str:
    """Return the ``# Yaver <version>`` section, including its heading."""
    if not _VERSION.fullmatch(version):
        raise BundleError(f"{version} is not a version.")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BundleError(f"Could not read {path.name}.") from exc
    except UnicodeDecodeError as exc:
        raise BundleError(f"{path.name} is not UTF-8 text.") from exc
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start: int | None = None
    for index, line in enumerate(lines):
        match = _SECTION.match(line.strip())
        if match and match.group(1) == version:
            start = index
            break
    if start is None:
        raise BundleError(f"Release notes have no section for {version}.")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _SECTION.match(lines[index].strip()):
            end = index
            break
    body = "\n".join(lines[start:end]).strip()
    if not body or body == f"# Yaver {version}":
        raise BundleError(f"Release notes for {version} are empty.")
    return body + "\n"


def write_bundle(root: Path, dest_dir: Path, notes: Path | None = None) -> Path:
    """Write ``yaver-executables-<version>.zip`` into ``dest_dir``.

    ``notes`` defaults to ``packaging/RELEASE_NOTES.md``. The zip contains
    the five packages and ``RELEASE_NOTES.txt`` for that version.
    """
    version, paths = collect_versioned_zips(root)
    note = release_section(version, notes if notes is not None else notes_path())
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / bundle_name(version)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path in paths:
            archive.write(path, arcname=path.name)
        info = zipfile.ZipInfo(_NOTE_NAME)
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, note.encode("utf-8"))
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pack the five Yaver executable zips.")
    parser.add_argument("--from", dest="source", required=True)
    parser.add_argument("--out", required=True, help="Directory for the bundle zip.")
    parser.add_argument(
        "--notes",
        default="",
        help="Markdown file of # Yaver X.Y.Z sections. Defaults to packaging/RELEASE_NOTES.md.",
    )
    args = parser.parse_args(argv)
    try:
        chosen = Path(args.notes) if str(args.notes).strip() else None
        dest = write_bundle(Path(args.source), Path(args.out), chosen)
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
