"""Run from the install folder so ``.env`` next to the exe is found."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows consoles otherwise mojibake Turkish / git subjects in this process.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")

if getattr(sys, "frozen", False):
    install = Path(sys.executable).resolve().parent
    try:
        os.chdir(install)
    except OSError:
        pass
    os.environ.setdefault("YAVER_INSTALL_ROOT", str(install))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        os.environ.setdefault("YAVER_RESOURCE_ROOT", str(meipass))
