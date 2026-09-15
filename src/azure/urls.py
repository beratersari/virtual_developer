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


def parse_tfs_collection_url(raw: str) -> str:
    """Normalize a TFS collection URL or return ``""``.

    Accepted: ``https://host/tfs/<Collection>`` (optional extra project /
    ``_git`` / ``_apis`` suffix is stripped). Host-only and ``/tfs`` with no
    collection name are rejected.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = unquote(parsed.path or "")
    lower = path.lower()
    if "/_apis/" in lower:
        path = path[: lower.index("/_apis/")]
    elif lower.endswith("/_apis"):
        path = path[: -len("/_apis")]
    lower = path.lower()
    if "/_git/" in lower:
        head = path[: lower.index("/_git/")].rstrip("/")
        if "/" in head:
            head = head.rsplit("/", 1)[0]
        path = head
    parts = [item for item in path.split("/") if item]
    if len(parts) < 2 or parts[0].lower() != "tfs":
        return ""
    collection = parts[1].strip()
    if not collection or collection.lower() in {"_apis", "_git", "_workitems"}:
        return ""
    return urlunparse(
        (parsed.scheme, parsed.netloc, f"/tfs/{collection}", "", "", "")
    ).rstrip("/")


def require_tfs_collection_url(raw: str) -> str:
    """Like ``parse_tfs_collection_url`` but raises ``ValueError`` if invalid."""
    url = parse_tfs_collection_url(raw)
    if url:
        return url
    raise ValueError(
        "Azure URL must include a TFS collection, e.g. "
        "https://tfs.example.com/tfs/DefaultCollection"
    )
