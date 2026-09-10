"""GitLab REST v4 client — works on GitLab.com, CE (basic), and EE.

Uses ``PRIVATE-TOKEN`` (all plans) and ``verify=False``
(INTENTIONAL product TLS policy: on-prem / intercept; no custom-CA path yet).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple
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


def project_from_repo_url(url: str) -> Optional[Tuple[str, str]]:
    """Split a clone or project URL into ``(host[:port], group/repo)``.

    Accepts ``https://host/group/repo.git``, ``git@host:group/repo.git``,
    and an MR page URL (path after ``/-/`` is dropped).
    """
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith("git@"):
        rest = raw[4:]
        if ":" not in rest:
            return None
        host, path = rest.split(":", 1)
        path = path.strip().removesuffix(".git").strip("/")
        host = host.strip().lower()
        if host and path:
            return host, path
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    path = (parsed.path or "").strip("/")
    if "/-/" in path:
        path = path.split("/-/", 1)[0]
    elif "/merge_requests/" in path:
        path = path.split("/merge_requests/", 1)[0]
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path:
        return None
    return host, path


def parse_merge_request_url(url: str) -> Optional[Tuple[str, str, int]]:
    """Split ``https://host/group/repo/-/merge_requests/12`` → host, project, iid."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").strip("/")
    if parsed.port and parsed.port not in (80, 443):
        host = f"{host}:{parsed.port}"
    match = re.search(r"^(.*?)/-/merge_requests/(\d+)$", path)
    if not match:
        match = re.search(r"^(.*?)/merge_requests/(\d+)$", path)
    if not host or not match:
        return None
    project = (match.group(1) or "").strip("/")
    if project.endswith(".git"):
        project = project[:-4]
    try:
        iid = int(match.group(2))
    except (TypeError, ValueError):
        return None
    if not project or iid <= 0:
        return None
    return host, project, iid


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

    def resolve_project(self, project: Any) -> Optional[Dict[str, Any]]:
        """GET ``/projects/:id`` or match ``path_with_namespace`` from the list."""
        if not self.api_base:
            return None
        url = self._project_url(project)
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                if isinstance(data, dict) and data.get("id") is not None:
                    return data
        except Exception as e:
            logger.debug(f"GitLab GET project {project!r} error: {e}")
        want = str(project or "").strip().strip("/").lower()
        if not want or want.isdigit():
            return None
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(
                    f"{self.api_base}/projects",
                    headers=self._headers(),
                    params={"simple": True, "membership": True},
                )
            if resp.status_code != 200:
                return None
            rows = resp.json() if resp.content else []
        except Exception as e:
            logger.debug(f"GitLab LIST projects error: {e}")
            return None
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            path = str(row.get("path_with_namespace") or "").strip().lower()
            if path == want:
                return row
        return None

    def _project_ident(self, project: Any) -> Any:
        """Prefer numeric project id so encoded ``group/repo`` paths work."""
        raw = project
        if isinstance(project, int) or (isinstance(project, str) and str(project).isdigit()):
            return project
        found = self.resolve_project(project)
        if found and found.get("id") is not None:
            return found["id"]
        return raw

    def get_merge_request(self, project: Any, mr_iid: int) -> Optional[Dict[str, Any]]:
        """GET ``/projects/:id/merge_requests/:iid`` (state / merged)."""
        if not self.api_base:
            return None
        try:
            iid = int(mr_iid)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        ident = self._project_ident(project)
        url = f"{self._project_url(ident)}/merge_requests/{iid}"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(url, headers=self._headers())
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                return data if isinstance(data, dict) else None
            logger.debug(
                f"GitLab GET MR {project}!{iid} failed ({resp.status_code})"
            )
            return None
        except Exception as e:
            logger.debug(f"GitLab GET MR {project}!{iid} error: {e}")
            return None

    def post_mr_note(
        self,
        *,
        project: Any,
        mr_iid: int,
        body: str,
        discussion_id: str = "",
        allow_new_thread: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """POST ``/projects/:id/merge_requests/:iid/notes`` (CE + EE).

        When *discussion_id* is set, only reply in that discussion. A failed
        reply must not become a new top-level note.
        """
        if not self.api_base:
            logger.error("GitLab API base missing; cannot post MR note")
            return None
        text = (body or "").strip()
        if not text:
            return None
        ident = self._project_ident(project)
        url = f"{self._project_url(ident)}/merge_requests/{int(mr_iid)}/notes"
        payload: Dict[str, Any] = {"body": text}
        did = (discussion_id or "").strip()
        if did:
            payload["in_reply_to_discussion_id"] = did
        elif not allow_new_thread:
            logger.warning(
                f"GitLab MR note skip: no discussion_id (thread-only) "
                f"{project}!{mr_iid}"
            )
            return None
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
                return None
        except Exception as e:
            logger.error(f"GitLab MR note error: {e}")
            return None
