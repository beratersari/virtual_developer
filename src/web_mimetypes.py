"""Force correct MIME types for SPA static assets.

On some Windows machines the registry maps ``.js`` → ``text/plain``. Python's
``mimetypes`` module (used by Starlette ``StaticFiles`` / ``FileResponse``)
honours that mapping, so ``<script type="module" src="...js">`` fails with:

    Failed to load module script: Expected a JavaScript-or-Wasm module script
    but the server responded with a MIME type of "text/plain".

This is **not** caused by using ``pip install -r requirements.txt`` instead of
``install-dashboard.bat`` — it is machine MIME-map + static server behaviour.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Dict, Optional

# IANA-preferred types browsers accept for ES modules / SPA assets
_SPA_MIME: Dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".cjs": "text/javascript",
    ".css": "text/css",
    ".wasm": "application/wasm",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".map": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".html": "text/html",
    ".htm": "text/html",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

def ensure_spa_mimetypes() -> None:
    """Override Python/OS MIME map so ``.js`` is never served as text/plain.

    Re-applied every call (cheap): other code or the Windows registry can put
    ``text/plain`` back on ``.js`` between requests in long-running processes.
    """
    # Load registry/db first, then force our overrides on top
    mimetypes.init()
    for ext, mime in _SPA_MIME.items():
        mimetypes.add_type(mime, ext, strict=True)
        mimetypes.types_map[ext] = mime
        # Non-strict / common maps (some Starlette paths consult these)
        try:
            mimetypes.common_types[ext] = mime  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass



def media_type_for_path(path: Path | str) -> Optional[str]:
    """Return explicit media type for a file path, or None if unknown."""
    ensure_spa_mimetypes()
    suffix = Path(path).suffix.lower()
    if suffix in _SPA_MIME:
        return _SPA_MIME[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed
