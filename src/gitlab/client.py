"""GitLab REST v4 client — works on GitLab.com, CE (basic), and EE.

Uses ``PRIVATE-TOKEN`` (all plans) and ``verify=False``
(INTENTIONAL product TLS policy: on-prem / intercept; no custom-CA path yet).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import httpx

from src.config import settings
from src.logger import logger


def _normalize_host(raw: str) -> str:
    """Hostname, including non-default port (simulator / on-prem :8091)."""
    host = (raw or "").strip().lower()
    if not host:
        return ""
    if host.startswith("git@"):
        return host[4:].split(":", 1)[0]
    if "://" not in host:
        # already host[:port]
        if "/" not in host:
            return host
        host = f"https://{host}"
    try:
        parsed = urlparse(host)
        name = (parsed.hostname or "").lower()
        if not name:
            return ""
        if parsed.port and parsed.port not in (80, 443):
            return f"{name}:{parsed.port}"
        return name
    except Exception:
        return (raw or "").strip().lower().split("/")[0]


class GitlabClient:
    """Minimal GitLab API used by MR comment intake (notes only)."""

    def __init__(
        self,
        host: Optional[str] = None,
        pat: Optional[str] = None,
        *,
        api_base: Optional[str] = None,
    ) -> None:
        self.host = _normalize_host(host or "")
        if api_base:
            self.api_base = api_base.rstrip("/")
        elif self.host:
            local = self.host.startswith("127.") or self.host.startswith("localhost")
            scheme = "http" if local else "https"
            self.api_base = f"{scheme}://{self.host}/api/v4"
        else:
            self.api_base = ""
        self.pat = (pat or "").strip()
        if not self.pat and self.host and hasattr(settings, "gitlab_pat_for_host"):
            self.pat = (settings.gitlab_pat_for_host(self.host) or "").strip()

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.pat:
            headers["PRIVATE-TOKEN"] = self.pat
        return headers

    def _project_url(self, project: Any) -> str:
        if isinstance(project, int) or (isinstance(project, str) and str(project).isdigit()):
            ident = str(project)
        else:
            ident = quote(str(project or "").strip().strip("/"), safe="")
        return f"{self.api_base}/projects/{ident}"

    def post_mr_note(
        self,
        *,
        project: Any,
        mr_iid: int,
        body: str,
        discussion_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """POST ``/projects/:id/merge_requests/:iid/notes`` (CE + EE)."""
        if not self.api_base:
            logger.error("GitLab API base missing; cannot post MR note")
            return None
        text = (body or "").strip()
        if not text:
            return None
        url = f"{self._project_url(project)}/merge_requests/{int(mr_iid)}/notes"
        payload: Dict[str, Any] = {"body": text}
        # Thread reply — supported on CE and EE when discussion_id is present
        if (discussion_id or "").strip():
            payload["in_reply_to_discussion_id"] = discussion_id.strip()
        try:
            # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
            with httpx.Client(timeout=30.0, verify=False) as client:
                resp = client.post(url, headers=self._headers(), json=payload)
                if resp.status_code in (200, 201):
                    data = resp.json() if resp.content else {}
                    logger.info(
                        f"Posted GitLab MR note on {project}!{mr_iid} "
                        f"note_id={data.get('id')}"
                    )
                    return data if isinstance(data, dict) else {"ok": True}
                logger.error(
                    f"GitLab MR note failed ({resp.status_code}): "
                    f"{(resp.text or '')[:400]}"
                )
                # Retry without discussion id (older CE / malformed id)
                if discussion_id and resp.status_code in (400, 404, 422):
                    payload.pop("in_reply_to_discussion_id", None)
                    resp2 = client.post(url, headers=self._headers(), json=payload)
                    if resp2.status_code in (200, 201):
                        data = resp2.json() if resp2.content else {}
                        return data if isinstance(data, dict) else {"ok": True}
                    logger.error(
                        f"GitLab MR note retry failed ({resp2.status_code}): "
                        f"{(resp2.text or '')[:400]}"
                    )
                return None
        except Exception as e:
            logger.error(f"GitLab MR note error: {e}")
            return None


def gitlab_client_for_host(host: str) -> GitlabClient:
    return GitlabClient(host=host)
