"""PyInstaller entry: same commands as ``python cli.py``."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

multiprocessing.freeze_support()

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cli import cli  # noqa: E402

if __name__ == "__main__":
    cli()
