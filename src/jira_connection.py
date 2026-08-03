"""Probe Jira connectivity (settings Test connection)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from src.config import settings
from src.logger import logger


def probe_jira_connection(
    *,
    host: Optional[str] = None,
    email: Optional[str] = None,
    api_token: Optional[str] = None,
    max_projects: int = 25,
) -> Dict[str, Any]:
    """Verify Jira credentials via ``/myself`` and list projects.

    Auth:
      * email + token → HTTP Basic (Cloud API token / dev)
      * token only → Bearer (PAT / prod)

    Omitted fields fall back to runtime ``settings``. Never returns the token.
    """
    h = (host if host is not None else settings.jira_host or "").strip().rstrip("/")
    em = (
        email if email is not None else getattr(settings, "jira_email", "") or ""
    ).strip()
    tok = (api_token if api_token is not None else "").strip()
    if not tok:
        tok = (settings.jira_api_token or "").strip()

    if not h:
        return {"ok": False, "error": "Jira host is required", "host": ""}
    if not tok:
        return {
            "ok": False,
            "host": h,
            "error": (
                "No API token provided and none stored. "
                "Paste a token or save settings first."
            ),
        }

    is_cloud = "atlassian.net" in h.lower()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = None
    if em:
        auth = (em, tok)
        auth_mode = "basic"
    else:
        headers["Authorization"] = f"Bearer {tok}"
        auth_mode = "bearer"

    base = f"{h}/rest/api/2"
    timeout = httpx.Timeout(25.0, connect=10.0)

    try:
        with httpx.Client(
            base_url=base,
            auth=auth,
            headers=headers,
            timeout=timeout,
            verify=False,
        ) as client:
            me = client.get("/myself")
            if me.status_code in (401, 403):
                hint = ""
                if is_cloud and not em:
                    hint = (
                        " For Jira Cloud API tokens, set Jira email "
                        "(HTTP Basic email+token). Leave email empty only for "
                        "Bearer PAT (on-prem / prod)."
                    )
                return {
                    "ok": False,
                    "host": h,
                    "auth_mode": auth_mode,
                    "http_status": me.status_code,
                    "error": (
                        f"Auth failed (HTTP {me.status_code}). "
                        f"Token invalid, revoked, or missing scopes.{hint}"
                    ),
                }
            if me.status_code != 200:
                return {
                    "ok": False,
                    "host": h,
                    "auth_mode": auth_mode,
                    "http_status": me.status_code,
                    "error": (
                        f"/myself returned HTTP {me.status_code}: "
                        f"{(me.text or '')[:300]}"
                    ),
                }
            user = me.json() if me.content else {}
            display = ""
            account = ""
            if isinstance(user, dict):
                display = (
                    user.get("displayName")
                    or user.get("name")
                    or user.get("emailAddress")
                    or ""
                )
                account = (
                    user.get("accountId")
                    or user.get("name")
                    or user.get("key")
                    or ""
                )

            projects: List[Dict[str, Any]] = []
            projects_error: Optional[str] = None
            # Server/DC + Cloud: GET /project returns projects the user can browse
            limit = max(1, min(int(max_projects), 50))
            proj = client.get("/project", params={"expand": "description"})
            if proj.status_code == 200:
                raw = proj.json()
                if isinstance(raw, list):
                    for p in raw[:limit]:
                        if not isinstance(p, dict):
                            continue
                        projects.append(
                            {
                                "id": p.get("id"),
                                "key": p.get("key") or "",
                                "name": p.get("name") or "",
                                "project_type": (
                                    (p.get("projectTypeKey") or "")
                                    if isinstance(p.get("projectTypeKey"), str)
                                    else ""
                                ),
                                "style": p.get("style") or "",
                            }
                        )
            else:
                projects_error = f"Could not list projects (HTTP {proj.status_code})"
                logger.warning(
                    f"Jira test connection projects failed for {h}: {proj.status_code}"
                )

            return {
                "ok": True,
                "host": h,
                "auth_mode": auth_mode,
                "is_cloud": is_cloud,
                "user": {
                    "display_name": display,
                    "account": str(account) if account else "",
                    "email": (user.get("emailAddress") if isinstance(user, dict) else None),
                },
                "projects": projects,
                "project_count": len(projects),
                "projects_error": projects_error,
                "message": (
                    f"Connected as {display or account or 'user'} on {h}; "
                    f"{len(projects)} project(s) listed"
                    + (f" ({projects_error})" if projects_error else "")
                ),
            }
    except httpx.TimeoutException:
        return {
            "ok": False,
            "host": h,
            "error": f"Timed out reaching {h} (network or host unreachable)",
        }
    except httpx.HTTPError as e:
        logger.warning(f"Jira test connection HTTP error for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": f"HTTP error contacting Jira: {e}",
        }
    except Exception as e:
        logger.warning(f"Jira test connection failed for {h}: {e}")
        return {
            "ok": False,
            "host": h,
            "error": str(e)[:500],
        }
