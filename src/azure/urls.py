"""Azure DevOps / TFS URL helpers. Match Creasy 0.9.1."""

from __future__ import annotations

from urllib.parse import unquote, urlparse, urlunparse


def identity_root(url: str) -> str:
    """Server root for ``connectionData``.

    TFS rejects collection-scoped ``/_apis/connectionData`` with 400.
    ``https://host/tfs/Collection`` and ``https://host/tfs`` both resolve
    to ``https://host/tfs``. ``https://host/Collection`` (no virtual
    directory) resolves to the host. Azure DevOps Services keeps the org.
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
    # Collection at site root (no /tfs virtual directory): connectionData
    # is on the host, not /Collection. Azure DevOps Services is handled above.
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


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


_RESERVED_COLLECTION = {"tfs", "_apis", "_git", "_workitems"}


def parse_tfs_collection_url(raw: str) -> str:
    """Normalize a collection URL or return ``""``.

    Accepted (optional project / ``_git`` / ``_apis`` suffix is stripped):

    * ``https://host/tfs/<Collection>`` — classic TFS virtual directory
    * ``https://host/<Collection>`` — Azure DevOps Server without ``/tfs``

    Host-only and ``/tfs`` with no collection name are rejected. ``/tfs`` is
    never inserted when the operator omitted it.
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
    if not parts:
        return ""
    if parts[0].lower() == "tfs":
        if len(parts) < 2:
            return ""
        collection = parts[1].strip()
        path_out = f"/tfs/{collection}"
    else:
        collection = parts[0].strip()
        path_out = f"/{collection}"
    if not collection or collection.lower() in _RESERVED_COLLECTION:
        return ""
    return urlunparse(
        (parsed.scheme, _collection_netloc(parsed), path_out, "", "", "")
    ).rstrip("/")


def _collection_netloc(parsed) -> str:
    """Hostname in lower case, without the scheme's default port.

    ``https://TFS.example.com:443/...`` and ``https://tfs.example.com/...``
    are the same collection. A non-default port stays. The scheme stays, so
    http and https remain different keys.
    """
    host = (parsed.hostname or "").lower()
    if not host:
        return parsed.netloc
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    default = 443 if parsed.scheme == "https" else 80
    if port and port != default:
        return f"{host}:{port}"
    return host


def tfs_collection_host(url: str) -> str:
    """Hostname[:port] from a collection or git URL."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        parsed = urlparse(f"https://{url}")
    name = (parsed.hostname or "").lower()
    if not name:
        return ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and port not in (80, 443):
        return f"{name}:{port}"
    return name


def require_tfs_collection_url(raw: str) -> str:
    """Like ``parse_tfs_collection_url`` but raises ``ValueError`` if invalid."""
    url = parse_tfs_collection_url(raw)
    if url:
        return url
    raise ValueError(
        "Azure URL must include a collection name, e.g. "
        "https://host/tfs/DefaultCollection or https://host/DefaultCollection"
    )
