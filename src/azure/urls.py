"""Azure DevOps / TFS URL helpers. Match Creasy 0.9.1."""

from __future__ import annotations

from urllib.parse import unquote, urlparse, urlunparse


def identity_root(url: str) -> str:
    """Server root for ``connectionData``.

    TFS rejects collection-scoped ``/_apis/connectionData`` with 400.
    ``https://host/tfs/Collection`` and ``https://host/tfs`` both resolve
    to ``https://host/tfs``. Azure DevOps Services keeps the org.
    """
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    parts = [item for item in unquote(parsed.path or "").split("/") if item]
    if host == "dev.azure.com" or host.endswith(".dev.azure.com"):
        path = f"/{parts[0]}" if parts else ""
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    if host.endswith(".visualstudio.com"):
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    if parts and parts[0].lower() == "tfs":
        return urlunparse((parsed.scheme, parsed.netloc, "/tfs", "", "", ""))
    path = f"/{parts[0]}" if len(parts) == 1 else ""
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def identity_roots(configured: str) -> list[str]:
    """connectionData lives on the TFS app root, not /tfs/<Collection>."""
    configured = (configured or "").rstrip("/")
    roots: list[str] = []
    ident = identity_root(configured)
    if ident:
        roots.append(ident.rstrip("/"))
    parsed = urlparse(configured)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        host_root = f"{parsed.scheme}://{parsed.netloc}"
        tfs_root = f"{host_root}/tfs"
        parts = [item for item in (parsed.path or "").split("/") if item]
        if not parts:
            roots.append(host_root)
            roots.append(tfs_root)
        elif parts[0].lower() != "tfs":
            roots.append(configured)
    if configured and configured not in roots:
        roots.append(configured)
    return list(dict.fromkeys(item for item in roots if item))
