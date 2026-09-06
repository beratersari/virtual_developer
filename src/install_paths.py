"""Install vs bundled-resource roots.

Unfrozen (git checkout / zip + Python): both roots are the repo folder
(the directory that contains ``src/`` and ``VERSION``).

Frozen (PyInstaller ``yaver`` / ``yaver.exe``):

* ``install_root`` — folder next to the executable. ``.env`` lives here.
* ``resource_root`` — ``sys._MEIPASS`` (onedir ``_internal/``). Read-only
  bundled files: ``VERSION``, ``web/dist``, ``agent/``.

Do not look for a writable ``.env`` under ``resource_root`` when frozen.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) binary."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Read-only tree that ships inside the app (or the repo root)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    """Folder operators treat as the install (``.env`` next to the exe)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_web_dist() -> Path:
    return resource_root() / "web" / "dist"


def bundled_agent_dir() -> Path:
    return resource_root() / "agent"


def bundled_version_file() -> Path:
    return resource_root() / "VERSION"
