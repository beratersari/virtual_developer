"""Linux release ``.env.example`` leaves the data folder unset.

The app then uses ``~/yaver_data``. A value already in the operator ``.env``
still wins. Packages through 0.9.81 wrote ``/var/tmp/yaver`` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLACEHOLDER = "# YAVER_BASE_DIR="
_LINUX_COMMENT = "#   Linux:   ~/yaver_data\n"
_WINDOWS_COMMENT = "#   Windows: C:\\yaver_data\n"


def linux_release_env_example(text: str) -> str:
    """Keep ``YAVER_BASE_DIR`` commented so the app default applies."""
    if _PLACEHOLDER not in text:
        raise ValueError("YAVER_BASE_DIR placeholder missing from .env.example")
    if _LINUX_COMMENT not in text or _WINDOWS_COMMENT not in text:
        raise ValueError("yaver_data default missing from .env.example")
    if "\nYAVER_BASE_DIR=" in "\n" + text:
        raise ValueError("YAVER_BASE_DIR must stay unset in the release example")
    return text


def apply_linux_env_example(path: Path) -> None:
    path.write_text(
        linux_release_env_example(path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: env_example.py .env.example", file=sys.stderr)
        return 2
    apply_linux_env_example(Path(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
