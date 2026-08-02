"""Probe GitLab connectivity with a host-specific PAT (settings test connection)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

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
        return (parsed.hostname or "").lower()
    except Exception:
        return (raw or "").strip().lower().split("/")[0]


def probe_gitlab_connection(
    host: str,
    *,
    pat: Optional[str] = None,
    max_projects: int = 25,
) -> Dict[str, Any]:
    """Verify a GitLab PAT can authenticate and list reachable projects.

    If ``pat`` is empty, uses the stored PAT for ``host`` from settings.
    Never returns the PAT value.

    Named ``probe_*`` (not ``test_*``) so pytest does not collect this module.
    """
    h = _normalize_host(host)
    if not h:
        return {"ok": False, "error": "host is required", "host": ""}

    token = (pat or "").strip()
    if not token and hasattr(settings, "gitlab_pat_for_host"):
        token = (settings.gitlab_pat_for_host(h) or "").strip()
    if not token:
        token = (settings.gitlab_pat or "").strip()
    if not token:
        return {
            "ok": False,
            "host": h,
            "error": (
                "No PAT provided and none stored for this host. "
                "Paste a PAT or save credentials first."
            ),
        }

    base = f"https://{h}/api/v4"
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    # On-prem often uses custom CAs; match product's pragmatic TLS stance for GitLab
    timeout = httpx.Timeout(20.0, connect=10.0)

    try:
        with httpx.Client(timeout=timeout, verify=False, headers=headers) as client:
            user_resp = client.get(f"{base}/user")
            if user_resp.status_code == 401:
                return {
                    "ok": False,
                    "host": h,
                    "error": "Unauthorized (401) — PAT is invalid or revoked",
                    "http_status": 401,
                }
            if user_resp.status_code == 403:
                return {
                    "ok": False,
                    "host": h,
                    "error": "Forbidden (403) — PAT lacks required scopes (need api or read_api)",
                    "http_status": 403,
                }
            if user_resp.status_code != 200:
                detail = (user_resp.text or "")[:300]
                return {
                    "ok": False,
                    "host": h,
                    "error": f"GitLab /user returned HTTP {user_resp.status_code}: {detail}",
                    "http_status": user_resp.status_code,
                }
            user = user_resp.json() if user_resp.content else {}
            username = (
                (user.get("username") or user.get("name") or "")
                if isinstance(user, dict)
                else ""
            )
            user_id = user.get("id") if isinstance(user, dict) else None

            # Projects the token can see (member of preferred for relevance)
            proj_resp = client.get(
                f"{base}/projects",
                params={
                    "membership": "true",
                    "simple": "true",
                    "per_page": max(1, min(int(max_projects), 50)),
                    "order_by": "last_activity_at",
                    "sort": "desc",
                },
            )
            projects: List[Dict[str, Any]] = []
            projects_error: Optional[str] = None
            if proj_resp.status_code == 200:
                raw = proj_resp.json()
                if isinstance(raw, list):
                    for p in raw:
                        if not isinstance(p, dict):
                            continue
                        projects.append(
                            {
                                "id": p.get("id"),
                                "name": p.get("name") or "",
                                "path_with_namespace": p.get("path_with_namespace")
                                or p.get("path")
                                or "",
                                "web_url": p.get("web_url") or "",
                                "visibility": p.get("visibility") or "",
                            }
                        )
            else:
                projects_error = (
                    f"Could not list projects (HTTP {proj_resp.status_code})"
                )
                logger.warning(
                    f"GitLab test connection projects list failed for {h}: "
                    f"{proj_resp.status_code}"
                )

            return {
                "ok": True,
                "host": h,
                "user": {
                    "id": user_id,
                    "username": username,
                    "name": (user.get("name") if isinstance(user, dict) else None),
                },
                "projects": projects,
                "project_count": len(projects),
                "projects_error": projects_error,
                "message": (
                    f"Connected as @{username or 'unknown'} on {h}; "
                    f"{len(projects)} project(s) listed"
                    + (f" ({projects_error})" if projects_error else "")
                ),
            }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "host": h,
            "error": f"Timed out reaching https://{h}/api/v4 (network or host unreachable)",
        }
    except httpx.HTTPError as e:
        logger.warning(f"GitLab test connection HTTP error for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": f"HTTP error contacting GitLab: {e}",
        }
    except Exception as e:
        logger.warning(f"GitLab test connection failed for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": str(e)[:500],
        }
