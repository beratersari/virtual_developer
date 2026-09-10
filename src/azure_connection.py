"""Probe Azure DevOps Server connectivity with a host-specific PAT."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import httpx

from src.azure.auth import azure_basic_auth
from src.azure.log import azure_info, azure_warning, yn
from src.azure.urls import identity_root, identity_roots
from src.config import settings

# TFS redirects anonymous/Negotiate probes to a login page unless suppressed.
# Same PAT then 401s at the host root even though /tfs/<Collection> is fine.
_PROBE_HEADERS = {
    "Accept": "application/json",
    "X-TFS-FedAuthRedirect": "Suppress",
}


def _normalize_host(raw: str) -> str:
    host = (raw or "").strip().lower()
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    try:
        parsed = urlparse(host)
        name = (parsed.hostname or "").lower()
        if not name:
            return ""
        if parsed.port:
            return f"{name}:{parsed.port}"
        return name
    except Exception:
        return (raw or "").strip().lower().split("/")[0]


def _netloc(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text.split('/', 1)[0]}"
    try:
        parsed = urlparse(text)
    except Exception:
        return ""
    return (parsed.netloc or "").strip()


def _origins(host: str) -> List[str]:
    """Scheme+host candidates. Hostname-only tries https then http."""
    raw = (host or "").strip()
    if not raw:
        return []
    if "://" in raw:
        parsed = urlparse(raw)
        if not parsed.netloc:
            return []
        return [f"{parsed.scheme}://{parsed.netloc}"]
    first = raw.split("/", 1)[0]
    local = first.startswith("127.") or first.startswith("localhost")
    if local:
        return [f"http://{first}"]
    return [f"https://{first}", f"http://{first}"]


def _dedupe_bases(bases: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for b in bases:
        key = (b or "").rstrip("/").lower()
        if key and key not in seen:
            seen.add(key)
            out.append((b or "").rstrip("/"))
    return out


def _configured_urls(host: str) -> List[str]:
    """Turn a Settings host into full URLs. Hostname-only tries https then http."""
    raw = (host or "").strip()
    if not raw:
        return []
    if "://" in raw:
        return [raw.rstrip("/")]
    first = raw.split("/", 1)[0]
    rest = raw[len(first) :].rstrip("/")
    local = first.startswith("127.") or first.startswith("localhost")
    schemes = ["http"] if local else ["https", "http"]
    return [f"{scheme}://{first}{rest}" for scheme in schemes]


def _candidate_bases(host: str, extra: Optional[Iterable[str]] = None) -> List[str]:
    """Creasy 0.9.1 identity roots: ``/tfs``, not ``/tfs/<Collection>``.

    Collection-scoped ``connectionData`` returns 400 on TFS.
    """
    roots: List[str] = []
    for item in extra or []:
        text = str(item or "").strip()
        if not text:
            continue
        if "://" not in text:
            text = f"https://{text}"
        roots.extend(identity_roots(identity_root(text) or text))
    for configured in _configured_urls(host):
        roots.extend(identity_roots(configured))
    return _dedupe_bases(roots)


def _collection_names(payload: Any) -> List[str]:
    raw = payload.get("value") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    names: List[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _discover_collection_bases(client: httpx.Client, origins: List[str]) -> List[str]:
    """List /tfs/<Collection> from the server so hostname-only Test works.

    Clone already has the collection on the git URL. Settings Test only has
    the host, so DefaultCollection is often the wrong guess.
    """
    found: List[str] = []
    for origin in origins:
        for prefix in ("/tfs", ""):
            listed = False
            for ver in ("7.1", "7.0", "6.0", "4.1"):
                url = f"{origin}{prefix}/_apis/projectCollections"
                try:
                    resp = client.get(url, params={"api-version": ver})
                except httpx.HTTPError:
                    break
                azure_info(
                    f"probe collections {url} api={ver} status={resp.status_code}"
                )
                if resp.status_code == 200:
                    try:
                        payload = resp.json() if resp.content else {}
                    except Exception:
                        payload = {}
                    for name in _collection_names(payload):
                        found.append(f"{origin}{prefix}/{name}")
                        if prefix:
                            found.append(f"{origin}/{name}")
                    listed = True
                    break
                if resp.status_code not in (400, 404):
                    break
            if listed:
                break
    return _dedupe_bases(found)


def remembered_azure_collection(host: str) -> str:
    """Last collection URL learned from a webhook or a successful Test."""
    h = _normalize_host(host)
    if not h:
        return ""
    try:
        from src.config import load_runtime_settings

        raw = load_runtime_settings().get("azure_collection_urls")
    except Exception:
        return ""
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get(h) or "").strip()


def remember_azure_collection(host: str, collection_url: str) -> None:
    """Keep /tfs/<Collection> so the next hostname-only Test does not 401."""
    h = _normalize_host(host)
    url = (collection_url or "").rstrip("/")
    if not h or not url or "/_apis/" in url.lower():
        return
    url = identity_root(url) or url
    try:
        from src.config import load_runtime_settings, save_runtime_settings

        raw = load_runtime_settings().get("azure_collection_urls")
        data: Any = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if str(data.get(h) or "").rstrip("/") == url:
            return
        data[h] = url
        save_runtime_settings({"azure_collection_urls": json.dumps(data, sort_keys=True)})
        azure_info(f"probe remember collection host={h} collection={url}")
    except Exception as e:
        azure_warning(f"probe remember collection failed host={h}: {e}")


def probe_azure_connection(
    host: str,
    *,
    pat: Optional[str] = None,
    max_projects: int = 25,
) -> Dict[str, Any]:
    """Verify an Azure DevOps PAT can authenticate and list projects.

    If ``pat`` is empty, uses the stored PAT for ``host`` from settings.
    Never returns the PAT value.

    Named ``probe_*`` (not ``test_*``) so pytest does not collect this module.
    """
    raw_host = (host or "").strip()
    h = _normalize_host(raw_host)
    if not h:
        azure_warning("probe fail host is required")
        return {"ok": False, "error": "host is required", "host": ""}

    token = (pat or "").strip()
    provided_pat = bool(token)
    azure_info(
        f"probe start host={h} raw={raw_host!r} pat_in_request={yn(provided_pat)}"
    )
    if not token and hasattr(settings, "azure_pat_for_host"):
        token = (settings.azure_pat_for_host(h) or "").strip()
    if not token:
        allowed = []
        if hasattr(settings, "azure_allowed_hosts_list"):
            allowed = [x.lower() for x in settings.azure_allowed_hosts_list]
        mapped = {}
        if hasattr(settings, "azure_host_pat_map"):
            try:
                mapped = settings.azure_host_pat_map() or {}
            except Exception:
                mapped = {}
        if h in mapped or h in allowed:
            token = (getattr(settings, "azure_pat", "") or "").strip()
        elif not provided_pat:
            azure_warning(f"probe fail no stored PAT host={h}")
            return {
                "ok": False,
                "host": h,
                "error": (
                    "No PAT stored for this host. Paste a PAT or add the host "
                    "in Settings before testing."
                ),
            }
    if not token:
        azure_warning(f"probe fail no PAT host={h}")
        return {
            "ok": False,
            "host": h,
            "error": (
                "No PAT provided and none stored for this host. "
                "Paste a PAT or save credentials first."
            ),
        }

    headers = {
        **_PROBE_HEADERS,
        "Authorization": azure_basic_auth(token),
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    last_error = ""
    last_status: Optional[int] = None
    saw_401 = False
    saw_403 = False
    tried: List[str] = []

    try:
        # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
        with httpx.Client(timeout=timeout, verify=False, headers=headers) as client:
            extra: List[str] = []
            remembered = remembered_azure_collection(h)
            if remembered:
                extra.append(identity_root(remembered) or remembered)
            for base in _candidate_bases(raw_host or h, extra):
                conn_url = f"{base}/_apis/connectionData"
                resp = None
                for api_ver in ("7.1", "7.0"):
                    try:
                        resp = client.get(
                            conn_url, params={"api-version": api_ver}
                        )
                    except httpx.HTTPError as e:
                        last_error = str(e)
                        resp = None
                        break
                    last_status = resp.status_code
                    if resp.status_code == 200:
                        break
                    if resp.status_code not in (400, 404):
                        break
                if resp is None:
                    azure_info(f"probe try {conn_url} no response last_error={last_error!r}")
                    continue
                last_status = resp.status_code
                tried.append(f"{conn_url} → {resp.status_code}")
                azure_info(
                    f"probe try {conn_url} status={resp.status_code}"
                )
                # Creasy 0.9.1: collection-scoped connectionData is 400.
                # Host root may 401 (Negotiate). /tfs is the identity root.
                if resp.status_code == 401:
                    saw_401 = True
                    last_error = (
                        f"{conn_url} returned HTTP 401"
                    )
                    continue
                if resp.status_code == 403:
                    saw_403 = True
                    last_error = (
                        f"{conn_url} returned HTTP 403"
                    )
                    continue
                if resp.status_code != 200:
                    last_error = (
                        f"{conn_url} returned HTTP {resp.status_code}: "
                        f"{(resp.text or '')[:200]}"
                    )
                    continue

                data = resp.json() if resp.content else {}
                authenticated = (
                    data.get("authenticatedUser")
                    if isinstance(data, dict)
                    else {}
                )
                if not isinstance(authenticated, dict):
                    authenticated = {}
                username = (
                    authenticated.get("uniqueName")
                    or authenticated.get("providerDisplayName")
                    or authenticated.get("displayName")
                    or ""
                )
                user_id = authenticated.get("id")

                projects: List[Dict[str, Any]] = []
                projects_error: Optional[str] = None
                proj_resp = client.get(
                    f"{base}/_apis/projects",
                    params={
                        "api-version": "7.1",
                        "$top": max(1, min(int(max_projects), 50)),
                    },
                )
                if proj_resp.status_code == 200:
                    raw = proj_resp.json() if proj_resp.content else {}
                    items = raw.get("value") if isinstance(raw, dict) else raw
                    if isinstance(items, list):
                        for p in items:
                            if not isinstance(p, dict):
                                continue
                            projects.append(
                                {
                                    "id": p.get("id"),
                                    "name": p.get("name") or "",
                                    "path_with_namespace": p.get("name") or "",
                                    "web_url": "",
                                    "visibility": p.get("visibility") or "",
                                }
                            )
                else:
                    projects_error = (
                        f"Could not list projects (HTTP {proj_resp.status_code})"
                    )
                    azure_warning(
                        f"probe projects list failed host={h} "
                        f"status={proj_resp.status_code}"
                    )

                azure_info(
                    f"probe ok host={h} collection={base} user={username or '-'} "
                    f"projects={len(projects)}"
                )
                remember_azure_collection(h, identity_root(base) or base)
                return {
                    "ok": True,
                    "host": h,
                    "collection_url": base,
                    "user": {
                        "id": user_id,
                        "username": username,
                        "name": authenticated.get("providerDisplayName")
                        or authenticated.get("displayName"),
                    },
                    "projects": projects,
                    "project_count": len(projects),
                    "projects_error": projects_error,
                    "message": (
                        f"Connected as {username or 'unknown'} on {h}; "
                        f"{len(projects)} project(s) listed"
                        + (f" ({projects_error})" if projects_error else "")
                    ),
                }

            if saw_401 and not saw_403:
                azure_warning(f"probe fail 401 host={h} tried={tried}")
                hint = (
                    "Unauthorized (401) at the TFS identity root. Creasy 0.9.1 "
                    "authenticates at https://<server>/tfs/_apis/connectionData "
                    "(not /tfs/<Collection>). Check the PAT and try Host "
                    "tfs.example.com/tfs."
                )
                if tried:
                    hint += " Tried: " + "; ".join(tried[:8])
                return {
                    "ok": False,
                    "host": h,
                    "error": hint,
                    "http_status": 401,
                }
            if saw_403:
                azure_warning(f"probe fail 403 host={h}")
                return {
                    "ok": False,
                    "host": h,
                    "error": (
                        "Forbidden (403) — PAT lacks required scopes "
                        "(Code: Read & Write)"
                    ),
                    "http_status": 403,
                }
            azure_warning(
                f"probe fail host={h} last_status={last_status} "
                f"error={last_error!r}"
            )
            return {
                "ok": False,
                "host": h,
                "error": last_error
                or (
                    f"Could not reach Azure DevOps Server on {h} "
                    f"(last HTTP {last_status})"
                ),
                "http_status": last_status,
            }
    except httpx.TimeoutException:
        azure_warning(f"probe timeout host={h}")
        return {
            "ok": False,
            "host": h,
            "error": f"Timed out reaching {h} (network or host unreachable)",
        }
    except httpx.HTTPError as e:
        azure_warning(f"probe HTTP error host={h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": f"HTTP error contacting Azure DevOps Server: {e}",
        }
    except Exception as e:
        azure_warning(f"probe failed host={h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": str(e)[:500],
        }
