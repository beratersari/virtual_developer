"""Linux release ``.env.example`` uses a directory every account can write."""

from __future__ import annotations

import sys
from pathlib import Path

LINUX_BASE_DIR = "/var/tmp/yaver"
_PLACEHOLDER = "# YAVER_BASE_DIR="
_LINUX_COMMENT = (
    "#   Linux:   ~/.local/share/yaver   (or $XDG_DATA_HOME/yaver)\n"
)
_LINUX_RELEASE_COMMENT = (
    "#   Linux:   /var/tmp/yaver   (shared by every account, no sudo)\n"
)


def linux_release_env_example(text: str) -> str:
    """Set ``YAVER_BASE_DIR=/var/tmp/yaver`` in the shipped example."""
    if _PLACEHOLDER not in text:
        raise ValueError("YAVER_BASE_DIR placeholder missing from .env.example")
    if _LINUX_COMMENT in text:
        text = text.replace(_LINUX_COMMENT, _LINUX_RELEASE_COMMENT, 1)
    return text.replace(
        _PLACEHOLDER + "\n",
        f"YAVER_BASE_DIR={LINUX_BASE_DIR}\n",
        1,
    )


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
