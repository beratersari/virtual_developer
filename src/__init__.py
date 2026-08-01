"""JIRA Virtual Developer - Python integration for Oh My OpenAgent."""

from pathlib import Path


def _read_version() -> str:
    """Read SemVer product version from the repo/root VERSION file."""
    candidates = [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path.cwd() / "VERSION",
    ]
    for path in candidates:
        try:
            if path.is_file():
                ver = path.read_text(encoding="utf-8").strip()
                if ver:
                    return ver
        except OSError:
            continue
    return "0.0.0-dev"


__version__ = _read_version()
