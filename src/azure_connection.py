"""Probe Azure DevOps Server connectivity with a host-specific PAT."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from src.azure.client import azure_basic_auth_header
from src.config import settings
from src.logger import logger


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


def _candidate_bases(host: str) -> List[str]:
    """On-prem TFS often lives at /tfs/DefaultCollection, not the host root."""
    h = (host or "").strip()
    if not h:
        return []
    if "://" in h:
        parsed = urlparse(h)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = (parsed.path or "").rstrip("/")
        bases = []
        if path:
            bases.append(f"{origin}{path}")
        bases.extend(
            [
                origin,
                f"{origin}/tfs",
                f"{origin}/tfs/DefaultCollection",
            ]
        )
        out: List[str] = []
        seen: set[str] = set()
        for b in bases:
            key = b.rstrip("/").lower()
            if key not in seen:
                seen.add(key)
                out.append(b.rstrip("/"))
        return out
    local = h.startswith("127.") or h.startswith("localhost")
    scheme = "http" if local else "https"
    origin = f"{scheme}://{h}"
    return [
        origin,
        f"{origin}/tfs",
        f"{origin}/tfs/DefaultCollection",
    ]


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
        return {"ok": False, "error": "host is required", "host": ""}

    token = (pat or "").strip()
    provided_pat = bool(token)
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
            return {
                "ok": False,
                "host": h,
                "error": (
                    "No PAT stored for this host. Paste a PAT or add the host "
                    "in Settings before testing."
                ),
            }
    if not token:
        return {
            "ok": False,
            "host": h,
            "error": (
                "No PAT provided and none stored for this host. "
                "Paste a PAT or save credentials first."
            ),
        }

    headers = {
        "Accept": "application/json",
        "Authorization": azure_basic_auth_header(token),
    }
    timeout = httpx.Timeout(20.0, connect=10.0)
    last_error = ""
    last_status: Optional[int] = None

    try:
        # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
        with httpx.Client(timeout=timeout, verify=False, headers=headers) as client:
            for base in _candidate_bases(raw_host or h):
                conn_url = f"{base}/_apis/connectionData"
                try:
                    resp = client.get(
                        conn_url, params={"api-version": "7.1"}
                    )
                except httpx.HTTPError as e:
                    last_error = str(e)
                    continue
                last_status = resp.status_code
                if resp.status_code == 401:
                    return {
                        "ok": False,
                        "host": h,
                        "error": "Unauthorized (401) — PAT is invalid or revoked",
                        "http_status": 401,
                    }
                if resp.status_code == 403:
                    return {
                        "ok": False,
                        "host": h,
                        "error": (
                            "Forbidden (403) — PAT lacks required scopes "
                            "(Code: Read & Write)"
                        ),
                        "http_status": 403,
                    }
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
                    logger.warning(
                        f"Azure test connection projects list failed for {h}: "
                        f"{proj_resp.status_code}"
                    )

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
        return {
            "ok": False,
            "host": h,
            "error": f"Timed out reaching {h} (network or host unreachable)",
        }
    except httpx.HTTPError as e:
        logger.warning(f"Azure test connection HTTP error for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": f"HTTP error contacting Azure DevOps Server: {e}",
        }
    except Exception as e:
        logger.warning(f"Azure test connection failed for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": str(e)[:500],
        }
