# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Yaver CLI + daemon (onedir).

Config (versions, mode) lives in packaging/pyinstaller/versions.env.
Do not switch this spec to onefile without a product decision — the
daemon is long-running and onefile re-extracts on every launch.
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPECDIR = Path(SPECPATH)
ROOT = SPECDIR.parent.parent

datas: list = [
    (str(ROOT / "VERSION"), "."),
    (str(ROOT / ".env.example"), "."),
    (str(ROOT / "agent"), "agent"),
    (str(ROOT / "web" / "dist"), "web/dist"),
]
if (ROOT / "sample_project").is_dir():
    datas.append((str(ROOT / "sample_project"), "sample_project"))

binaries: list = []
hiddenimports: list = list(collect_submodules("src"))

# Runtime packages the daemon/CLI actually imports. celery/redis are in
# requirements.txt but unused — keep them out of the binary.
_COLLECT_PACKAGES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "pydantic",
    "pydantic_settings",
    "pydantic_core",
    "httpx",
    "httpcore",
    "anyio",
    "click",
    "rich",
    "structlog",
    "watchdog",
    "dotenv",
    "multipart",
    "python_multipart",
    "websockets",
    "httptools",
    "watchfiles",
    "aiofiles",
    "atlassian",
    "flask",
    "flask_cors",
    "schedule",
    "certifi",
    "idna",
    "h11",
    "sniffio",
    "annotated_types",
    "typing_extensions",
    "yaml",
)

for pkg in _COLLECT_PACKAGES:
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        hiddenimports.append(pkg)
        continue
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "src.dashboard.api",
    "src.daemon",
]

excludes = [
    "celery",
    "redis",
    "pytest",
    "IPython",
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "PIL",
    "notebook",
]

a = Analysis(
    [str(SPECDIR / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(SPECDIR / "runtime_hook.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yaver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="yaver",
)
