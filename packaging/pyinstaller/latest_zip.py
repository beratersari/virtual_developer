"""Stable zip name for each frozen binary.

The office release site uploads these files. The version is the VERSION
file inside the zip. The filename only says which binary it is.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# Longer names first so a future shorter product id cannot steal a match.
_LATEST_BY_PRODUCT = (
    ("yaver-linux-x64-ubuntu-24.04", "yaver-ubuntu-24.04-latest.zip"),
    ("yaver-linux-x64-ubuntu-22.04", "yaver-ubuntu-22.04-latest.zip"),
    ("yaver-linux-x64-ubuntu-20.04", "yaver-ubuntu-20.04-latest.zip"),
    ("yaver-linux-x64-ubuntu-18.04", "yaver-ubuntu-18.04-latest.zip"),
    ("yaver-windows-x64", "yaver-windows-latest.zip"),
)


def latest_zip_name(dist_name: str) -> str:
    """Return ``yaver-windows-latest.zip`` or the matching Ubuntu name."""
    text = (dist_name or "").strip()
    for product, name in _LATEST_BY_PRODUCT:
        if text == product or text.startswith(product + "-"):
            return name
    raise ValueError(f"No latest zip name for {dist_name!r}.")


def write_latest_copy(zip_path: Path, dist_name: str) -> Path:
    """Copy the versioned binary zip to the stable latest name beside it."""
    dest = zip_path.parent / latest_zip_name(dist_name)
    shutil.copyfile(zip_path, dest)
    return dest
