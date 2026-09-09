"""Azure DevOps Server REST client (2022.2 / TFS).

PAT auth is the same as Creasy: ``Authorization: Basic pat:<PAT>``.
IIS rejects an empty username (``:PAT`` / Bearer). ``verify=False`` is
the product TLS policy (on-prem / intercept; no custom-CA path yet).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import httpx

from src.azure.auth import azure_basic_auth, azure_basic_auth_header, azure_basic_user
from src.config import settings
from src.logger import logger

__all__ = [
    "AzureDevOpsClient",
    "azure_basic_auth",
    "azure_basic_auth_header",
    "azure_basic_user",
]

# Azure DevOps Server 2022.2 ships REST 7.1; 7.0 is accepted as fallback.
_API_VERSION = "7.1"
_API_VERSION_FALLBACK = "7.0"


def _normalize_host(raw: str) -> str:
    host = (raw or "").strip().lower()
    if not host:
        return ""
    if host.startswith("git@"):
        return host[4:].split(":", 1)[0]
    if "://" not in host:
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


def _ref_name(branch: str) -> str:
    name = (branch or "").strip()
    if not name:
        return ""
    if name.startswith("refs/"):
        return name
    return f"refs/heads/{name}"


class AzureDevOpsClient:
    """Minimal Azure DevOps Server API used by PR comment intake."""

    def __init__(
        self,
        host: Optional[str] = None,
        pat: Optional[str] = None,
        *,
        collection_url: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> None:
        self.host = _normalize_host(host or "")
        self.collection_url = (collection_url or "").rstrip("/")
        if api_base:
            self.api_base = api_base.rstrip("/")
        elif self.collection_url:
            self.api_base = self.collection_url
        elif self.host:
            local = self.host.startswith("127.") or self.host.startswith("localhost")
            scheme = "http" if local else "https"
            self.api_base = f"{scheme}://{self.host}"
        else:
            self.api_base = ""
        self.pat = (pat or "").strip()
        if not self.pat and self.host and hasattr(settings, "azure_pat_for_host"):
            self.pat = (settings.azure_pat_for_host(self.host) or "").strip()
        if not self.pat and self.host:
            leftover = (getattr(settings, "azure_pat", "") or "").strip()
            mapping = {}
            if hasattr(settings, "azure_host_pat_map"):
                try:
                    mapping = settings.azure_host_pat_map() or {}
                except Exception:
                    mapping = {}
            if leftover and not mapping:
                self.pat = leftover

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.pat:
            headers["Authorization"] = azure_basic_auth(self.pat)
        return headers

    def _repo_url(self, project: str, repository: Any) -> str:
        proj = quote(str(project or "").strip().strip("/"), safe="")
        ident = str(repository or "").strip()
        if not ident.isdigit() and "-" not in ident:
            ident = quote(ident, safe="")
        if proj:
            return (
                f"{self.api_base}/{proj}/_apis/git/repositories/{ident}"
            )
        return f"{self.api_base}/_apis/git/repositories/{ident}"

    def get_pull_request(
        self, project: str, repository: Any, pr_id: int
    ) -> Optional[Dict[str, Any]]:
        """GET pull request (status / completed)."""
        if not self.api_base:
            return None
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        url = f"{self._repo_url(project, repository)}/pullrequests/{iid}"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(
                    url,
                    headers=self._headers(),
                    params={"api-version": _API_VERSION},
                )
                if resp.status_code == 404:
                    resp = client.get(
                        url,
                        headers=self._headers(),
                        params={"api-version": _API_VERSION_FALLBACK},
                    )
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                return data if isinstance(data, dict) else None
            logger.debug(
                f"Azure GET PR {project}/{repository}!{iid} failed "
                f"({resp.status_code})"
            )
            return None
        except Exception as e:
            logger.debug(f"Azure GET PR {project}/{repository}!{iid} error: {e}")
            return None

    def post_pr_comment(
        self,
        *,
        project: str,
        repository: Any,
        pr_id: int,
        body: str,
        thread_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """POST a PR thread comment (new thread, or reply when thread_id set)."""
        if not self.api_base:
            logger.error("Azure API base missing; cannot post PR comment")
            return None
        text = (body or "").strip()
        if not text:
            return None
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        base = f"{self._repo_url(project, repository)}/pullrequests/{iid}/threads"
        tid = (thread_id or "").strip()
        try:
            with httpx.Client(timeout=30.0, verify=False) as client:
                if tid and tid.isdigit():
                    url = f"{base}/{tid}/comments"
                    payload: Dict[str, Any] = {
                        "content": text,
                        "commentType": 1,
                    }
                    posted = self._post_json(client, url, payload)
                    if posted is not None:
                        logger.info(
                            f"Posted Azure PR comment on {project}/{repository}!{iid} "
                            f"thread={tid} id={posted.get('id')}"
                        )
                        return posted
                payload = {
                    "comments": [{"parentCommentId": 0, "content": text, "commentType": 1}],
                    "status": 1,
                }
                posted = self._post_json(client, base, payload)
                if posted is not None:
                    logger.info(
                        f"Posted Azure PR thread on {project}/{repository}!{iid} "
                        f"id={posted.get('id')}"
                    )
                    return posted
                return None
        except Exception as e:
            logger.error(f"Azure PR comment error: {e}")
            return None

    def _post_json(
        self, client: httpx.Client, url: str, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        resp = client.post(
            url,
            headers=self._headers(),
            params={"api-version": _API_VERSION},
            json=payload,
        )
        if resp.status_code in (200, 201):
            data = resp.json() if resp.content else {}
            return data if isinstance(data, dict) else {"ok": True}
        if resp.status_code in (400, 404, 415):
            resp2 = client.post(
                url,
                headers=self._headers(),
                params={"api-version": _API_VERSION_FALLBACK},
                json=payload,
            )
            if resp2.status_code in (200, 201):
                data = resp2.json() if resp2.content else {}
                return data if isinstance(data, dict) else {"ok": True}
            logger.error(
                f"Azure PR comment retry failed ({resp2.status_code}): "
                f"{(resp2.text or '')[:400]}"
            )
            return None
        logger.error(
            f"Azure PR comment failed ({resp.status_code}): "
            f"{(resp.text or '')[:400]}"
        )
        return None

    def find_pull_request(
        self,
        *,
        project: str,
        repository: Any,
        source_branch: str,
        target_branch: str = "",
    ) -> Optional[str]:
        """Return the web URL of an active PR for source→target, if any."""
        if not self.api_base:
            return None
        source = _ref_name(source_branch)
        if not source:
            return None
        params: Dict[str, Any] = {
            "api-version": _API_VERSION,
            "searchCriteria.sourceRefName": source,
            "searchCriteria.status": "active",
        }
        target = _ref_name(target_branch)
        if target:
            params["searchCriteria.targetRefName"] = target
        url = f"{self._repo_url(project, repository)}/pullrequests"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(url, headers=self._headers(), params=params)
                if resp.status_code == 404:
                    params["api-version"] = _API_VERSION_FALLBACK
                    resp = client.get(url, headers=self._headers(), params=params)
            if resp.status_code != 200:
                logger.debug(
                    f"Azure list PR {project}/{repository} failed "
                    f"({resp.status_code})"
                )
                return None
            data = resp.json() if resp.content else {}
            rows = data.get("value") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                return None
            for item in rows:
                if not isinstance(item, dict):
                    continue
                web = self._pr_web_url(item, project, repository)
                if web:
                    return web
            return None
        except Exception as e:
            logger.debug(f"Azure list PR {project}/{repository} error: {e}")
            return None

    def create_pull_request(
        self,
        *,
        project: str,
        repository: Any,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> Optional[str]:
        """Create a PR (or reuse an active one) using the same PAT as clone/push."""
        if not self.api_base:
            logger.error("Azure API base missing; cannot create pull request")
            return None
        source = _ref_name(source_branch)
        target = _ref_name(target_branch)
        if not source or not target:
            logger.error("Azure PR create refused: source/target branch missing")
            return None
        existing = self.find_pull_request(
            project=project,
            repository=repository,
            source_branch=source,
            target_branch=target,
        )
        if existing:
            logger.info(f"Azure PR already exists: {existing}")
            return existing
        payload = {
            "sourceRefName": source,
            "targetRefName": target,
            "title": title or source_branch,
            "description": description or title or "",
        }
        url = f"{self._repo_url(project, repository)}/pullrequests"
        try:
            with httpx.Client(timeout=30.0, verify=False) as client:
                posted = self._post_json(client, url, payload)
            if not posted:
                return None
            web = self._pr_web_url(posted, project, repository)
            if web:
                logger.info(f"Azure pull request created: {web}")
            return web
        except Exception as e:
            logger.error(f"Azure PR create error: {e}")
            return None

    def _pr_web_url(
        self, data: Dict[str, Any], project: str, repository: Any
    ) -> Optional[str]:
        links = data.get("_links") if isinstance(data.get("_links"), dict) else {}
        web = links.get("web") if isinstance(links.get("web"), dict) else {}
        href = str(web.get("href") or "").strip()
        if href:
            return href
        iid = data.get("pullRequestId")
        try:
            pr_id = int(iid)
        except (TypeError, ValueError):
            pr_id = 0
        if pr_id <= 0 or not self.api_base:
            return None
        proj = quote(str(project or "").strip().strip("/"), safe="")
        repo = quote(str(repository or "").strip(), safe="")
        if proj and repo:
            return f"{self.api_base}/{proj}/_git/{repo}/pullrequest/{pr_id}"
        return None
