"""Azure DevOps Server REST client (2020.1+ and 2022).

PAT auth is the same as Creasy: ``Authorization: Basic pat:<PAT>``.
IIS rejects an empty username (``:PAT`` / Bearer). ``verify=False`` is
the product TLS policy (on-prem / intercept; no custom-CA path yet).

REST mapping (Microsoft): 2022.1→7.1, 2022→7.0, 2020 (incl. Update 1.1)→6.0.
Older api-version values work on newer servers, so every call walks
``API_VERSIONS`` on 400/404/415.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from src.azure.auth import (
    TFS_API_HEADERS,
    azure_basic_auth,
    azure_basic_auth_header,
    azure_basic_user,
)
from src.azure.log import azure_error, azure_info, azure_warning, http_detail, yn
from src.config import settings

__all__ = [
    "AzureDevOpsClient",
    "azure_basic_auth",
    "azure_basic_auth_header",
    "azure_basic_user",
]

# Newest first. 2020 Update 1.1 stops at 6.0; 2022 accepts 6.0 and 7.x.
API_VERSIONS: tuple[str, ...] = ("7.1", "7.0", "6.1", "6.0")
_API_VERSION = API_VERSIONS[0]
_API_VERSION_FALLBACK = API_VERSIONS[1]


def _response_api_version(resp: Any) -> str:
    req = getattr(resp, "request", None)
    url = getattr(req, "url", None)
    params = getattr(url, "params", None)
    if params is None:
        return "-"
    try:
        return str(params.get("api-version") or "-")
    except Exception:
        return "-"


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


def _azure_parent_comment_id(raw: Any) -> str:
    """Numeric comment id for parentCommentId (strip pr:thread:comment keys)."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    return text if text.isdigit() else ""


def _identity_names_from_payload(data: Any) -> list:
    rows: list = []
    if isinstance(data, dict):
        if isinstance(data.get("value"), list):
            rows = [x for x in data["value"] if isinstance(x, dict)]
        else:
            rows = [data]
    elif isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
    names: list = []
    seen: set = set()

    def _add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            names.append(text)

    for row in rows:
        for key in (
            "providerDisplayName",
            "customDisplayName",
            "displayName",
            "uniqueName",
            "mailAddress",
        ):
            _add(row.get(key))
        props = row.get("properties")
        if isinstance(props, dict):
            for key in ("Account", "Mail", "DirectoryAlias", "Domain"):
                item = props.get(key)
                if isinstance(item, dict):
                    _add(item.get("$value") or item.get("value"))
                else:
                    _add(item)
    return names


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
        self.collection_url = (collection_url or "").rstrip("/")
        self.host = _normalize_host(host or self.collection_url or "")
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
        if (
            not self.pat
            and self.collection_url
            and hasattr(settings, "azure_pat_for_collection")
        ):
            self.pat = (
                settings.azure_pat_for_collection(self.collection_url) or ""
            ).strip()
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
            **TFS_API_HEADERS,
            "Content-Type": "application/json",
        }
        if self.pat:
            headers["Authorization"] = azure_basic_auth(self.pat)
        return headers

    def _request(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        extra_params: Optional[Dict[str, Any]] = None,
        json: Any = None,
        ok: tuple = (200, 201),
        retry_on: tuple = (400, 404, 415),
    ) -> httpx.Response:
        """Walk ``API_VERSIONS`` so 2020.1.1 (6.0) and 2022 (7.x) both work."""
        extra = dict(extra_params or {})
        last: Optional[httpx.Response] = None
        verb = (method or "GET").upper()
        for ver in API_VERSIONS:
            params = {**extra, "api-version": ver}
            if verb == "GET":
                resp = client.get(url, headers=self._headers(), params=params)
            elif verb == "POST":
                resp = client.post(
                    url, headers=self._headers(), params=params, json=json
                )
            elif verb == "PATCH":
                resp = client.patch(
                    url, headers=self._headers(), params=params, json=json
                )
            else:
                raise ValueError(f"unsupported Azure HTTP method {verb}")
            last = resp
            if resp.status_code in ok or resp.status_code not in retry_on:
                return resp
        assert last is not None
        return last

    def connection_user(self) -> Optional[Dict[str, Any]]:
        """PAT user from ``connectionData`` on collection and TFS identity roots."""
        from src.azure.identity import parse_authenticated_user
        from src.azure.urls import identity_root, identity_roots

        if not self.pat:
            azure_warning("connectionData skip pat empty")
            return None
        bases: List[str] = []
        for raw in (self.collection_url, self.api_base):
            text = str(raw or "").strip()
            if not text:
                continue
            bases.extend(identity_roots(identity_root(text) or text))
            if text not in bases:
                bases.append(text)
        if self.host and not bases:
            local = self.host.startswith("127.") or self.host.startswith("localhost")
            scheme = "http" if local else "https"
            bases.extend(identity_roots(f"{scheme}://{self.host}"))
        headers = self._headers()
        try:
            with httpx.Client(timeout=8.0, verify=False, headers=headers) as client:
                for base in bases:
                    url = f"{str(base).rstrip('/')}/_apis/connectionData"
                    for ver in (*API_VERSIONS, ""):
                        try:
                            resp = client.get(
                                url,
                                params={"api-version": ver} if ver else None,
                            )
                        except httpx.HTTPError as exc:
                            azure_warning(f"connectionData error root={base} err={exc}")
                            break
                        if resp.status_code in (400, 404):
                            continue
                        if resp.status_code != 200:
                            azure_warning(
                                f"connectionData fail status={resp.status_code} "
                                f"root={base} ver={ver}"
                            )
                            break
                        user = parse_authenticated_user(
                            resp.json() if resp.content else {}
                        )
                        if user:
                            azure_info(
                                f"connectionData ok id={user.get('id')} "
                                f"names={user.get('names')} root={base}"
                            )
                            return user
        except Exception as exc:
            azure_warning(f"connectionData lookup failed: {exc}")
        return None

    def identity_aliases(self, identity_id: str) -> list:
        """Display / unique names for a TFS mention GUID."""
        from src.azure.mentions import normalize_guid

        gid = normalize_guid(identity_id)
        if not gid or not self.api_base:
            return []
        cache = getattr(self, "_identity_alias_cache", None)
        if cache is None:
            cache = {}
            self._identity_alias_cache = cache
        if gid in cache:
            return list(cache[gid])
        names = self._fetch_identity_aliases(gid)
        cache[gid] = names
        return list(names)

    def _fetch_identity_aliases(self, gid: str) -> list:
        from src.azure.urls import identity_root, identity_roots

        bases = []
        for raw in (self.collection_url, self.api_base):
            if raw:
                bases.extend(identity_roots(identity_root(raw) or raw))
        if self.api_base and self.api_base not in bases:
            bases.append(self.api_base)
        urls = []
        for base in bases:
            root = str(base).rstrip("/")
            urls.append((f"{root}/_apis/identities/{gid}", {}))
            urls.append((f"{root}/_apis/identities", {"identityIds": gid}))
        for url, extra_params in urls:
            try:
                with httpx.Client(timeout=20.0, verify=False) as client:
                    resp = self._request(
                        client, "GET", url, extra_params=extra_params
                    )
                    ver = _response_api_version(resp)
                if resp.status_code != 200:
                    azure_warning(
                        f"identity lookup fail id={gid} "
                        + http_detail(
                            method="GET",
                            url=url,
                            status=resp.status_code,
                            api_version=ver,
                            body=resp.text,
                        )
                    )
                    continue
                names = _identity_names_from_payload(
                    resp.json() if resp.content else {}
                )
                if names:
                    azure_info(f"identity lookup ok id={gid} names={names}")
                    return names
            except Exception as exc:
                azure_warning(f"identity lookup error id={gid} err={exc}")
        return []

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
                resp = self._request(client, "GET", url)
                ver = _response_api_version(resp)
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
        comment_content: str = "",
    ) -> str:
        """Find the PR thread that contains *comment_id* (TFS omits threadId).

        Comment ids restart at 1 on every thread. If more than one thread
        has that id, require a unique content match. Never return the
        first hit.
        """
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
                resp = self._request(client, "GET", url)
                ver = _response_api_version(resp)
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
            hits: list[tuple[str, str]] = []
            want = (comment_content or "").strip()
            for thread in rows:
                if not isinstance(thread, dict):
                    continue
                tid = thread.get("id")
                if tid is None or tid == "":
                    continue
                for item in thread.get("comments") or []:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("id") or "") != cid:
                        continue
                    hits.append((str(tid), str(item.get("content") or "").strip()))
            if not hits:
                azure_warning(
                    f"find_thread miss {project}/{repository}!{iid} comment={cid}"
                )
                return ""
            if len(hits) == 1:
                azure_info(
                    f"find_thread ok {project}/{repository}!{iid} "
                    f"comment={cid} thread={hits[0][0]}"
                )
                return hits[0][0]
            if want:
                matched = [tid for tid, text in hits if text == want]
                if len(matched) == 1:
                    azure_info(
                        f"find_thread ok {project}/{repository}!{iid} "
                        f"comment={cid} thread={matched[0]} (content)"
                    )
                    return matched[0]
            azure_warning(
                f"find_thread ambiguous {project}/{repository}!{iid} "
                f"comment={cid} threads={[t for t, _ in hits]}"
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
        parent_comment_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """POST a PR overview note, or a reply when thread_id is set.

        A new top-level comment is a Closed conversation note (not an
        Active review thread the operator has to resolve). Replies set
        ``parentCommentId`` when the prompt comment id is known.

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
        parent_id = _azure_parent_comment_id(parent_comment_id)
        azure_info(
            f"post_comment start {project}/{repository}!{iid} "
            f"thread={tid or 'new'} parent={parent_id or '-'} "
            f"chars={len(text)} pat={yn(self.pat)}"
        )
        try:
            with httpx.Client(timeout=30.0, verify=False) as client:
                if tid and tid.isdigit():
                    url = f"{base}/{tid}/comments"
                    payload: Dict[str, Any] = {
                        "content": text,
                        "commentType": 1,
                    }
                    if parent_id:
                        payload["parentCommentId"] = int(parent_id)
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
                    "comments": [
                        {
                            "parentCommentId": 0,
                            "content": text,
                            "commentType": 1,
                        }
                    ],
                    # Closed: overview note, not an Active review thread.
                    "status": 4,
                }
                posted = self._post_json(client, base, payload)
                if posted is not None:
                    azure_info(
                        f"post_comment ok {project}/{repository}!{iid} "
                        f"new_note id={posted.get('id')}"
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

    def list_pr_threads(
        self, *, project: str, repository: Any, pr_id: int
    ) -> list[Dict[str, Any]]:
        if not self.api_base:
            return []
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return []
        if iid <= 0:
            return []
        url = f"{self._repo_url(project, repository)}/pullrequests/{iid}/threads"
        try:
            with httpx.Client(timeout=30.0, verify=False) as client:
                resp = self._request(client, "GET", url)
            if resp.status_code != 200:
                return []
            data = resp.json() if resp.content else {}
        except Exception as e:
            azure_warning(f"list_threads error {project}/{repository}!{iid}: {e}")
            return []
        rows = data.get("value") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def pr_iteration_span(
        self, *, project: str, repository: Any, pr_id: int
    ) -> tuple[int, int]:
        """``(firstComparingIteration, secondComparingIteration)``."""
        if not self.api_base:
            return 1, 1
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return 1, 1
        if iid <= 0:
            return 1, 1
        url = f"{self._repo_url(project, repository)}/pullrequests/{iid}/iterations"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = self._request(client, "GET", url)
            if resp.status_code != 200:
                return 1, 1
            data = resp.json() if resp.content else {}
        except Exception as e:
            azure_warning(f"iterations error {project}/{repository}!{iid}: {e}")
            return 1, 1
        rows = data.get("value") if isinstance(data, dict) else data
        ids: list[int] = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            try:
                ids.append(int(item.get("id")))
            except (TypeError, ValueError):
                continue
        if not ids:
            return 1, 1
        return 1, max(ids)

    def post_pr_file_thread(
        self,
        *,
        project: str,
        repository: Any,
        pr_id: int,
        body: str,
        thread_context: Dict[str, Any],
        first_iteration: int = 1,
        second_iteration: int = 1,
    ) -> Optional[Dict[str, Any]]:
        """Active inline file thread (status 1), not a Closed overview note."""
        if not self.api_base:
            azure_error("post_file_thread fail api_base missing")
            return None
        text = (body or "").strip()
        if not text or not isinstance(thread_context, dict) or not thread_context:
            return None
        try:
            iid = int(pr_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        url = f"{self._repo_url(project, repository)}/pullrequests/{iid}/threads"
        payload: Dict[str, Any] = {
            "comments": [
                {"parentCommentId": 0, "content": text, "commentType": 1}
            ],
            "status": 1,
            "threadContext": thread_context,
            "properties": {
                "Microsoft.TeamFoundation.Discussion.SupportsMarkdown": {
                    "$type": "System.Int32",
                    "$value": 1,
                }
            },
            "pullRequestThreadContext": {
                "iterationContext": {
                    "firstComparingIteration": max(1, int(first_iteration or 1)),
                    "secondComparingIteration": max(1, int(second_iteration or 1)),
                }
            },
        }
        path = thread_context.get("filePath") or "-"
        try:
            with httpx.Client(timeout=30.0, verify=False) as client:
                posted = self._post_json(client, url, payload)
            if posted is not None:
                azure_info(
                    f"post_file_thread ok {project}/{repository}!{iid} "
                    f"path={path} id={posted.get('id')}"
                )
                return posted
            azure_warning(
                f"post_file_thread fail {project}/{repository}!{iid} path={path}"
            )
            return None
        except Exception as e:
            azure_error(f"post_file_thread error {project}/{repository}!{iid}: {e}")
            return None

    def _post_json(
        self, client: httpx.Client, url: str, payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        resp = self._request(client, "POST", url, json=payload)
        ver = _response_api_version(resp)
        if resp.status_code in (200, 201):
            azure_info(
                "http "
                + http_detail(
                    method="POST",
                    url=url,
                    status=resp.status_code,
                    api_version=ver,
                )
            )
            data = resp.json() if resp.content else {}
            return data if isinstance(data, dict) else {"ok": True}
        azure_error(
            "http "
            + http_detail(
                method="POST",
                url=url,
                status=resp.status_code,
                api_version=ver,
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
        extra = {k: v for k, v in params.items() if k != "api-version"}
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                resp = self._request(client, "GET", url, extra_params=extra)
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

    def _wit_item_url(self, project: str, work_item_id: int) -> str:
        ident = int(work_item_id)
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        if proj:
            return f"{self.api_base}/{proj}/_apis/wit/workitems/{ident}"
        return f"{self.api_base}/_apis/wit/workitems/{ident}"

    def _get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        versions: Optional[List[str]] = None,
        accept: str = "application/json",
    ) -> Optional[Any]:
        extra = dict(params or {})
        vers = list(versions or API_VERSIONS)
        headers = dict(self._headers())
        headers["Accept"] = accept
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                last_status = 0
                last_ver = vers[0] if vers else _API_VERSION
                last_body = ""
                for ver in vers:
                    query = {**extra, "api-version": ver}
                    resp = client.get(url, headers=headers, params=query)
                    last_status = resp.status_code
                    last_ver = ver
                    last_body = resp.text
                    if resp.status_code == 200:
                        azure_info(
                            "http "
                            + http_detail(
                                method="GET",
                                url=url,
                                status=200,
                                api_version=ver,
                            )
                        )
                        if not resp.content:
                            return {}
                        try:
                            return resp.json()
                        except Exception:
                            return None
                    if resp.status_code not in (400, 404, 415):
                        break
                azure_warning(
                    "http "
                    + http_detail(
                        method="GET",
                        url=url,
                        status=last_status,
                        api_version=last_ver,
                        body=last_body,
                    )
                )
                return None
        except Exception as exc:
            azure_warning(f"http GET error url={url} err={exc}")
            return None

    def _patch_json(
        self,
        url: str,
        payload: Any,
        *,
        versions: Optional[List[str]] = None,
        content_type: str = "application/json-patch+json",
    ) -> Optional[Dict[str, Any]]:
        vers = list(versions or API_VERSIONS)
        headers = dict(self._headers())
        headers["Content-Type"] = content_type
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                last_status = 0
                last_ver = vers[0] if vers else _API_VERSION
                last_body = ""
                for ver in vers:
                    resp = client.patch(
                        url,
                        headers=headers,
                        params={"api-version": ver},
                        json=payload,
                    )
                    last_status = resp.status_code
                    last_ver = ver
                    last_body = resp.text
                    if resp.status_code in (200, 201):
                        azure_info(
                            "http "
                            + http_detail(
                                method="PATCH",
                                url=url,
                                status=resp.status_code,
                                api_version=ver,
                            )
                        )
                        data = resp.json() if resp.content else {}
                        return data if isinstance(data, dict) else {"ok": True}
                    if resp.status_code not in (400, 404, 415):
                        break
                azure_warning(
                    "http "
                    + http_detail(
                        method="PATCH",
                        url=url,
                        status=last_status,
                        api_version=last_ver,
                        body=last_body,
                    )
                )
                return None
        except Exception as exc:
            azure_warning(f"http PATCH error url={url} err={exc}")
            return None

    def list_projects(self) -> List[str]:
        """All team project names in this collection (paged)."""
        if not self.api_base:
            return []
        url = f"{self.api_base}/_apis/projects"
        names: List[str] = []
        seen: set[str] = set()
        page = 200
        skip = 0
        while skip < 5000:
            data = self._get_json(
                url,
                params={"$top": page, "$skip": skip, "stateFilter": "wellFormed"},
            )
            if data is None and skip == 0:
                data = self._get_json(url, params={"$top": page, "$skip": skip})
            rows: List[Any] = []
            if isinstance(data, dict):
                raw = data.get("value")
                if isinstance(raw, list):
                    rows = raw
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            if len(rows) < page:
                break
            skip += page
        azure_info(f"list_projects count={len(names)} collection={self.api_base}")
        return names

    def create_work_item(
        self,
        project: str,
        work_item_type: str,
        fields: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """POST a work item (JSON Patch). Returns the created item dict."""
        if not self.api_base or not fields:
            return None
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        wtype = quote(unquote(str(work_item_type or "Task").strip()), safe="")
        if not proj or not wtype:
            return None
        ops = []
        for path, value in fields.items():
            name = str(path or "").strip()
            if not name:
                continue
            if not name.startswith("/"):
                name = f"/fields/{name}"
            ops.append({"op": "add", "path": name, "value": value})
        if not ops:
            return None
        url = f"{self.api_base}/{proj}/_apis/wit/workitems/${wtype}"
        vers = list(API_VERSIONS)
        headers = dict(self._headers())
        headers["Content-Type"] = "application/json-patch+json"
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                last_status = 0
                last_ver = vers[0]
                last_body = ""
                for ver in vers:
                    resp = client.post(
                        url,
                        headers=headers,
                        params={"api-version": ver},
                        json=ops,
                    )
                    last_status = resp.status_code
                    last_ver = ver
                    last_body = resp.text
                    if resp.status_code in (200, 201):
                        azure_info(
                            "http "
                            + http_detail(
                                method="POST",
                                url=url,
                                status=resp.status_code,
                                api_version=ver,
                            )
                        )
                        data = resp.json() if resp.content else {}
                        return data if isinstance(data, dict) else None
                    if resp.status_code not in (400, 404, 415):
                        break
                azure_warning(
                    "http "
                    + http_detail(
                        method="POST",
                        url=url,
                        status=last_status,
                        api_version=last_ver,
                        body=last_body,
                    )
                )
                return None
        except Exception as exc:
            azure_warning(f"create_work_item error {project}/{wtype}: {exc}")
            return None

    def get_work_item(
        self, project: str, work_item_id: int
    ) -> Optional[Dict[str, Any]]:
        """GET work item (expand=all). Walks 7.1 → 6.0 for 2020 and 2022."""
        if not self.api_base:
            return None
        try:
            iid = int(work_item_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        url = self._wit_item_url(project, iid)
        data = self._get_json(url, params={"$expand": "all"})
        if isinstance(data, dict) and data.get("id"):
            azure_info(
                f"get_work_item ok {project or '-'}/{iid} "
                f"rev={data.get('rev') or '-'}"
            )
            return data
        azure_warning(f"get_work_item fail {project or '-'}/{iid}")
        return None

    def get_work_item_type_states(
        self, project: str, work_item_type: str
    ) -> List[Dict[str, Any]]:
        """GET work item type states (Proposed / InProgress / Completed)."""
        if not self.api_base or not (work_item_type or "").strip():
            return []
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        wtype = quote(unquote(str(work_item_type).strip()), safe="")
        if proj:
            url = f"{self.api_base}/{proj}/_apis/wit/workitemtypes/{wtype}/states"
        else:
            url = f"{self.api_base}/_apis/wit/workitemtypes/{wtype}/states"
        data = self._get_json(url)
        rows = []
        if isinstance(data, dict):
            raw = data.get("value")
            if isinstance(raw, list):
                rows = [x for x in raw if isinstance(x, dict)]
        azure_info(
            f"wit states {project or '-'}/{work_item_type} count={len(rows)}"
        )
        return rows

    def update_work_item_fields(
        self,
        project: str,
        work_item_id: int,
        fields: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """JSON-Patch work item fields (System.State, System.Tags, …)."""
        if not self.api_base or not fields:
            return None
        try:
            iid = int(work_item_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        ops = []
        for path, value in fields.items():
            name = str(path or "").strip()
            if not name:
                continue
            if not name.startswith("/"):
                name = f"/fields/{name}"
            ops.append({"op": "add", "path": name, "value": value})
        if not ops:
            return None
        url = self._wit_item_url(project, iid)
        posted = self._patch_json(url, ops)
        if posted:
            azure_info(
                f"update_work_item ok {project or '-'}/{iid} fields={list(fields)}"
            )
        else:
            azure_warning(f"update_work_item fail {project or '-'}/{iid}")
        return posted

    def add_work_item_comment(
        self, project: str, work_item_id: int, body: str
    ) -> Optional[Dict[str, Any]]:
        """Post a work-item comment (comments API, then System.History)."""
        from src.azure.comment_html import work_item_comment_html

        text = work_item_comment_html(body or "")
        if not text or not self.api_base:
            return None
        try:
            iid = int(work_item_id)
        except (TypeError, ValueError):
            return None
        if iid <= 0:
            return None
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        if proj:
            url = f"{self.api_base}/{proj}/_apis/wit/workItems/{iid}/comments"
        else:
            url = f"{self.api_base}/_apis/wit/workItems/{iid}/comments"
        versions = [
            "7.1-preview.4",
            "7.0-preview.3",
            "6.1-preview.3",
            "6.0-preview.3",
            *API_VERSIONS,
        ]
        try:
            with httpx.Client(timeout=20.0, verify=False) as client:
                headers = dict(self._headers())
                for ver in versions:
                    resp = client.post(
                        url,
                        headers=headers,
                        params={"api-version": ver},
                        json={"text": text},
                    )
                    if resp.status_code in (200, 201):
                        azure_info(
                            "http "
                            + http_detail(
                                method="POST",
                                url=url,
                                status=resp.status_code,
                                api_version=ver,
                            )
                        )
                        data = resp.json() if resp.content else {}
                        return data if isinstance(data, dict) else {"id": "1"}
                    if resp.status_code not in (400, 404, 415):
                        azure_warning(
                            "http "
                            + http_detail(
                                method="POST",
                                url=url,
                                status=resp.status_code,
                                api_version=ver,
                                body=resp.text,
                            )
                        )
                        break
        except Exception as exc:
            azure_warning(f"workitem comment error {project}/{iid}: {exc}")
        posted = self.update_work_item_fields(
            project, iid, {"System.History": text}
        )
        if posted:
            azure_info(f"workitem comment via System.History {project or '-'}/{iid}")
            return {"id": str(posted.get("rev") or "history"), "rev": posted.get("rev")}
        azure_warning(f"workitem comment fail {project or '-'}/{iid}")
        return None

    def get_work_item_comments(
        self, project: str, work_item_id: int
    ) -> List[Dict[str, Any]]:
        """List work-item comments (oldest first). Empty if the API is missing."""
        try:
            iid = int(work_item_id)
        except (TypeError, ValueError):
            return []
        if iid <= 0 or not self.api_base:
            return []
        proj = quote(unquote(str(project or "").strip().strip("/")), safe="")
        if proj:
            url = f"{self.api_base}/{proj}/_apis/wit/workItems/{iid}/comments"
        else:
            url = f"{self.api_base}/_apis/wit/workItems/{iid}/comments"
        data = self._get_json(
            url,
            versions=[
                "7.1-preview.4",
                "7.0-preview.3",
                "6.1-preview.3",
                "6.0-preview.3",
                *API_VERSIONS,
            ],
        )
        rows: List[Any] = []
        if isinstance(data, dict):
            raw = data.get("comments") or data.get("value") or []
            if isinstance(raw, list):
                rows = raw
        out: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
        return out
