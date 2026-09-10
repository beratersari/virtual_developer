"""Azure DevOps Server REST client (2022.2 / TFS).

PAT auth is the same as Creasy: ``Authorization: Basic pat:<PAT>``.
IIS rejects an empty username (``:PAT`` / Bearer). ``verify=False`` is
the product TLS policy (on-prem / intercept; no custom-CA path yet).
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from src.azure.auth import azure_basic_auth, azure_basic_auth_header, azure_basic_user
from src.azure.log import azure_error, azure_info, azure_warning, http_detail, yn
from src.config import settings

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
        azure_info(
            f"client host={self.host or '-'} collection={self.collection_url or '-'} "
            f"api_base={self.api_base or '-'} pat={yn(self.pat)}"
        )

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.pat:
            headers["Authorization"] = azure_basic_auth(self.pat)
        return headers

    def _repo_url(self, project: str, repository: Any) -> str:
        # Unquote first so "Tank%20Projeleri" is not encoded as Tank%2520…
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        ident = unquote(str(repository or "").strip())
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
                ver = _API_VERSION
                if resp.status_code == 404:
                    azure_info(
                        "http "
                        + http_detail(
                            method="GET",
                            url=url,
                            status=resp.status_code,
                            api_version=_API_VERSION,
                            body=resp.text,
                        )
                        + " — retry 7.0"
                    )
                    resp = client.get(
                        url,
                        headers=self._headers(),
                        params={"api-version": _API_VERSION_FALLBACK},
                    )
                    ver = _API_VERSION_FALLBACK
            if resp.status_code == 200:
                data = resp.json() if resp.content else {}
                status = ""
                if isinstance(data, dict):
                    status = str(data.get("status") or "")
                azure_info(
                    f"get_pr ok {project}/{repository}!{iid} status={status or '-'} "
                    + http_detail(method="GET", url=url, status=200, api_version=ver)
                )
                return data if isinstance(data, dict) else None
            azure_warning(
                f"get_pr fail {project}/{repository}!{iid} "
                + http_detail(
                    method="GET",
                    url=url,
                    status=resp.status_code,
                    api_version=ver,
                    body=resp.text,
                )
            )
            return None
        except Exception as e:
            azure_warning(f"get_pr error {project}/{repository}!{iid}: {e}")
            return None

    def find_thread_id_for_comment(
        self,
        *,
        project: str,
        repository: Any,
        pr_id: int,
        comment_id: str,
    ) -> str:
        """Find the PR thread that contains *comment_id* (TFS omits threadId)."""
        cid = str(comment_id or "").strip()
        if not cid or not self.api_base:
            return ""
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return ""
        if iid <= 0:
            return ""
        url = f"{self._repo_url(project, repository)}/pullrequests/{iid}/threads"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = client.get(
                    url,
                    headers=self._headers(),
                    params={"api-version": _API_VERSION},
                )
                ver = _API_VERSION
                if resp.status_code in (400, 404, 415):
                    resp = client.get(
                        url,
                        headers=self._headers(),
                        params={"api-version": _API_VERSION_FALLBACK},
                    )
                    ver = _API_VERSION_FALLBACK
            if resp.status_code != 200:
                azure_warning(
                    f"find_thread fail {project}/{repository}!{iid} "
                    + http_detail(
                        method="GET",
                        url=url,
                        status=resp.status_code,
                        api_version=ver,
                        body=resp.text,
                    )
                )
                return ""
            data = resp.json() if resp.content else {}
            rows = data.get("value") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                return ""
            for thread in rows:
                if not isinstance(thread, dict):
                    continue
                tid = thread.get("id")
                if tid is None or tid == "":
                    continue
                for item in thread.get("comments") or []:
                    if isinstance(item, dict) and str(item.get("id") or "") == cid:
                        azure_info(
                            f"find_thread ok {project}/{repository}!{iid} "
                            f"comment={cid} thread={tid}"
                        )
                        return str(tid)
            azure_warning(
                f"find_thread miss {project}/{repository}!{iid} comment={cid}"
            )
            return ""
        except Exception as e:
            azure_warning(f"find_thread error {project}/{repository}!{iid}: {e}")
            return ""

    def post_pr_comment(
        self,
        *,
        project: str,
        repository: Any,
        pr_id: int,
        body: str,
        thread_id: str = "",
        allow_new_thread: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """POST a PR thread comment (new thread, or reply when thread_id set).

        When *thread_id* is set, only reply in that thread. A failed reply
        must not become a new top-level post.
        """
        if not self.api_base:
            azure_error("post_comment fail api_base missing")
            return None
        text = (body or "").strip()
        if not text:
            azure_warning(
                f"post_comment skip empty body {project}/{repository}!{pr_id}"
            )
            return None
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            azure_error(f"post_comment fail bad pr_id={pr_id!r}")
            return None
        if iid <= 0:
            azure_error(f"post_comment fail pr_id={iid}")
            return None
        base = f"{self._repo_url(project, repository)}/pullrequests/{iid}/threads"
        tid = (thread_id or "").strip()
        azure_info(
            f"post_comment start {project}/{repository}!{iid} "
            f"thread={tid or 'new'} chars={len(text)} pat={yn(self.pat)}"
        )
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
                        azure_info(
                            f"post_comment ok {project}/{repository}!{iid} "
                            f"thread={tid} id={posted.get('id')}"
                        )
                        return posted
                    azure_warning(
                        f"post_comment thread reply failed; not creating a new post "
                        f"{project}/{repository}!{iid} thread={tid}"
                    )
                    return None
                if not allow_new_thread:
                    azure_warning(
                        f"post_comment skip new thread {project}/{repository}!{iid}"
                    )
                    return None
                payload = {
                    "comments": [{"parentCommentId": 0, "content": text, "commentType": 1}],
                    "status": 1,
                }
                posted = self._post_json(client, base, payload)
                if posted is not None:
                    azure_info(
                        f"post_comment ok {project}/{repository}!{iid} "
                        f"new_thread id={posted.get('id')}"
                    )
                    return posted
                azure_error(
                    f"post_comment fail {project}/{repository}!{iid} "
                    f"thread={tid or 'new'}"
                )
                return None
        except Exception as e:
            azure_error(f"post_comment error {project}/{repository}!{iid}: {e}")
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
            azure_info(
                "http "
                + http_detail(
                    method="POST",
                    url=url,
                    status=resp.status_code,
                    api_version=_API_VERSION,
                )
            )
            data = resp.json() if resp.content else {}
            return data if isinstance(data, dict) else {"ok": True}
        if resp.status_code in (400, 404, 415):
            azure_info(
                "http "
                + http_detail(
                    method="POST",
                    url=url,
                    status=resp.status_code,
                    api_version=_API_VERSION,
                    body=resp.text,
                )
                + " — retry 7.0"
            )
            resp2 = client.post(
                url,
                headers=self._headers(),
                params={"api-version": _API_VERSION_FALLBACK},
                json=payload,
            )
            if resp2.status_code in (200, 201):
                azure_info(
                    "http "
                    + http_detail(
                        method="POST",
                        url=url,
                        status=resp2.status_code,
                        api_version=_API_VERSION_FALLBACK,
                    )
                )
                data = resp2.json() if resp2.content else {}
                return data if isinstance(data, dict) else {"ok": True}
            azure_error(
                "http "
                + http_detail(
                    method="POST",
                    url=url,
                    status=resp2.status_code,
                    api_version=_API_VERSION_FALLBACK,
                    body=resp2.text,
                )
            )
            return None
        azure_error(
            "http "
            + http_detail(
                method="POST",
                url=url,
                status=resp.status_code,
                api_version=_API_VERSION,
                body=resp.text,
            )
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
            azure_warning("find_pr skip api_base missing")
            return None
        source = _ref_name(source_branch)
        if not source:
            azure_warning("find_pr skip empty source branch")
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
                azure_warning(
                    f"find_pr fail {project}/{repository} "
                    + http_detail(
                        method="GET",
                        url=url,
                        status=resp.status_code,
                        body=resp.text,
                    )
                )
                return None
            data = resp.json() if resp.content else {}
            rows = data.get("value") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                azure_warning(
                    f"find_pr unexpected body {project}/{repository} "
                    f"type={type(data).__name__}"
                )
                return None
            azure_info(
                f"find_pr {project}/{repository} source={source} "
                f"target={target or '-'} matches={len(rows)}"
            )
            for item in rows:
                if not isinstance(item, dict):
                    continue
                web = self._pr_web_url(item, project, repository)
                if web:
                    azure_info(f"find_pr hit {web}")
                    return web
            return None
        except Exception as e:
            azure_warning(f"find_pr error {project}/{repository}: {e}")
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
            azure_error("create_pr fail api_base missing")
            return None
        source = _ref_name(source_branch)
        target = _ref_name(target_branch)
        if not source or not target:
            azure_error(
                f"create_pr refuse missing branch source={source!r} target={target!r}"
            )
            return None
        azure_info(
            f"create_pr start {project}/{repository} "
            f"{source} → {target} title={title!r} pat={yn(self.pat)}"
        )
        existing = self.find_pull_request(
            project=project,
            repository=repository,
            source_branch=source,
            target_branch=target,
        )
        if existing:
            azure_info(f"create_pr reuse existing {existing}")
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
                azure_error(
                    f"create_pr fail {project}/{repository} {source} → {target}"
                )
                return None
            web = self._pr_web_url(posted, project, repository)
            if web:
                azure_info(f"create_pr ok {web}")
            else:
                azure_warning(
                    f"create_pr posted but no web URL {project}/{repository} "
                    f"id={posted.get('pullRequestId')}"
                )
            return web
        except Exception as e:
            azure_error(f"create_pr error {project}/{repository}: {e}")
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
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        repo = quote(unquote(str(repository or "").strip()), safe="")
        if proj and repo:
            return f"{self.api_base}/{proj}/_git/{repo}/pullrequest/{pr_id}"
        return None
