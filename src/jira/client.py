"""JIRA API client wrapper."""

from typing import Any, Dict, Iterable, List, Optional, Union

import httpx

from src.config import settings
from src.logger import logger


def _comment_body_to_adf(body: str) -> Dict[str, Any]:
    """ADF doc for Cloud 400 fallback. Text nodes cannot contain raw newlines."""
    text = body or ""
    paragraphs: List[Dict[str, Any]] = []
    for line in text.split("\n"):
        content: List[Dict[str, Any]] = []
        if line:
            content.append({"type": "text", "text": line})
        else:
            content.append({"type": "hardBreak"})
        paragraphs.append({"type": "paragraph", "content": content})
    if not paragraphs:
        paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]
    return {"version": 1, "type": "doc", "content": paragraphs}


def _jira_fields_query(fields: Union[str, Iterable[str], None]) -> Optional[str]:
    """Normalize ``fields`` to a Jira ``fields=a,b,c`` query value.

    Callers sometimes pass a single string (``\"description\"``). ``\",\"``.join
    on a str iterates characters and requests ``d,e,s,c,...`` — which makes
    Jira omit the real description and can wipe the issue body on PUT.
    """
    if fields is None:
        return None
    if isinstance(fields, str):
        value = fields.strip()
        return value or None
    parts = [str(f).strip() for f in fields if str(f).strip()]
    return ",".join(parts) if parts else None


class JiraClient:
    """Client for JIRA REST API.

    Auth:
      * **Bearer (default / prod):** ``JIRA_HOST`` + ``JIRA_API_TOKEN``
        → ``Authorization: Bearer {token}``
      * **Basic (Cloud / dev):** also set ``JIRA_EMAIL``
        → HTTP Basic (email as username, API token as password)

    Empty email never forces Basic. Prod on-prem should leave email empty.

    TLS: ``verify=False`` is intentional (on-prem / intercept). Do not turn
    verification on without a supported custom-CA path.
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        api_token: Optional[str] = None,
        email: Optional[str] = None,
    ):
        self.host = (host or settings.jira_host).rstrip("/")
        self.api_token = api_token if api_token is not None else settings.jira_api_token
        self.email = (
            email if email is not None else getattr(settings, "jira_email", "")
        ) or ""
        
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        auth = None
        em = self.email.strip()
        # Cloud personal API tokens need Basic email:token; PATs use Bearer.
        if self.api_token and em:
            auth = (em, self.api_token)
            logger.info("JiraClient auth: HTTP Basic (email + API token)")
        elif self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
            logger.info("JiraClient auth: Bearer token")
        
        # INTENTIONAL: verify=False. On-prem / enterprise TLS intercept and
        # self-signed certs are expected. Do not enable verification until a
        # custom-CA path exists (product requirement).
        self.client = httpx.Client(
            base_url=f"{self.host}/rest/api/2",
            auth=auth,
            headers=headers,
            timeout=30.0,
            verify=False,
        )
        self.is_cloud = "atlassian.net" in self.host.lower()
        # Last create_issue / Agile lookup failure detail (callers soft-map None)
        self.last_error: Optional[str] = None
        # Last get_active_sprint outcome: ok | kanban | empty | error
        self.sprint_lookup: Optional[str] = None

    # Locale-friendly aliases: English preferred name → match tokens (lower)
    _ISSUE_TYPE_ALIASES: Dict[str, List[str]] = {
        "task": ["task", "görev", "gorev"],
        "story": ["story", "hikaye"],
        "epic": ["epic", "epik"],
        "bug": ["bug", "hata", "defect"],
        "sub-task": ["sub-task", "subtask", "alt görev", "alt gorev", "altgörev"],
        "subtask": ["sub-task", "subtask", "alt görev", "alt gorev", "altgörev"],
    }

    @staticmethod
    def _format_jira_error_body(response: httpx.Response) -> str:
        """Extract human-readable errors from a Jira REST error payload."""
        try:
            data = response.json()
        except Exception:
            return (response.text or f"HTTP {response.status_code}")[:500]
        parts: List[str] = []
        for m in data.get("errorMessages") or []:
            if m:
                parts.append(str(m))
        errors = data.get("errors") or {}
        if isinstance(errors, dict):
            for k, v in errors.items():
                parts.append(f"{k}: {v}")
        if parts:
            return "; ".join(parts)
        return (response.text or f"HTTP {response.status_code}")[:500]

    def get_project_issue_types(self, project: str) -> List[Dict[str, Any]]:
        """Return issue types available for a project (create-meta / project API)."""
        key = (project or "").strip()
        if not key:
            return []
        # Prefer create-meta (types actually creatable for the project)
        candidates = [
            f"/issue/createmeta?projectKeys={key}&expand=projects.issuetypes",
            f"/issue/createmeta/{key}/issuetypes",
            f"/project/{key}",
        ]
        for path in candidates:
            try:
                response = self.client.get(path)
                if response.status_code != 200:
                    continue
                data = response.json()
                if "projects" in data:
                    for p in data.get("projects") or []:
                        types = p.get("issuetypes") or p.get("issueTypes") or []
                        if types:
                            return list(types)
                if "issueTypes" in data:
                    return list(data.get("issueTypes") or [])
                if isinstance(data.get("values"), list) and data["values"]:
                    # Some Cloud paginated shapes
                    return list(data["values"])
                if "issueTypes" in data or data.get("issueTypes"):
                    return list(data.get("issueTypes") or [])
            except Exception as e:
                logger.debug(f"get_project_issue_types via {path}: {e}")
        return []

    def resolve_issuetype_ref(
        self,
        project: str,
        preferred: str = "Task",
    ) -> Dict[str, str]:
        """Pick a creatable issue type for the project.

        Team-managed / localized Jira Cloud often uses names like ``Görev``
        instead of English ``Task``. Prefer matching by preferred name and
        known locale aliases, then first non-subtask. Returns ``{"id": ...}``
        when possible (most reliable), else ``{"name": preferred}``.
        """
        preferred = (preferred or "Task").strip() or "Task"
        types = self.get_project_issue_types(project)
        if not types:
            return {"name": preferred}

        pref_l = preferred.lower()
        alias_tokens = set(self._ISSUE_TYPE_ALIASES.get(pref_l, [pref_l]))
        alias_tokens.add(pref_l)

        def _names(it: Dict[str, Any]) -> List[str]:
            out = []
            for k in ("name", "untranslatedName"):
                v = (it.get(k) or "").strip()
                if v:
                    out.append(v)
            return out

        def _is_subtask(it: Dict[str, Any]) -> bool:
            return bool(it.get("subtask"))

        # 1) Exact preferred name / untranslatedName
        for it in types:
            for n in _names(it):
                if n.lower() == pref_l and not _is_subtask(it):
                    tid = it.get("id")
                    if tid:
                        logger.info(
                            f"Issue type for {project}: id={tid} name={n!r} "
                            f"(exact match for {preferred!r})"
                        )
                        return {"id": str(tid)}
                    return {"name": n}

        # 2) Locale aliases (Task ↔ Görev, Story ↔ Hikaye, …)
        for it in types:
            if _is_subtask(it):
                continue
            for n in _names(it):
                if n.lower() in alias_tokens:
                    tid = it.get("id")
                    if tid:
                        logger.info(
                            f"Issue type for {project}: id={tid} name={n!r} "
                            f"(alias of {preferred!r})"
                        )
                        return {"id": str(tid)}
                    return {"name": n}

        # 3) First non-subtask, non-epic-ish hierarchy if possible
        for it in types:
            if _is_subtask(it):
                continue
            names = [n.lower() for n in _names(it)]
            if any(n in ("epic", "epik") for n in names):
                continue
            tid = it.get("id")
            name = (_names(it) or [preferred])[0]
            if tid:
                logger.warning(
                    f"Issue type for {project}: falling back to id={tid} "
                    f"name={name!r} (preferred {preferred!r} not found)"
                )
                return {"id": str(tid)}
            return {"name": name}

        # 4) Last resort: first type with an id
        for it in types:
            tid = it.get("id")
            if tid:
                return {"id": str(tid)}
        return {"name": preferred}

    def create_issue(
        self,
        project: str,
        summary: str,
        description: str,
        issue_type: str = "Task",
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a new issue in JIRA (REST API v2 fields wrapper).

        Resolves issue type against the project's creatable types so localized
        Cloud sites (e.g. ``Görev`` instead of ``Task``) still work.
        On failure sets ``self.last_error`` with Jira's message.
        """
        self.last_error = None
        itype_ref = self.resolve_issuetype_ref(project, issue_type)
        fields: Dict[str, Any] = {
            "project": {"key": project},
            "summary": summary,
            "description": description,
            "issuetype": itype_ref,
        }

        if assignee:
            # On-prem Server/DC uses name (not Cloud accountId)
            fields["assignee"] = {"name": assignee}

        if labels:
            fields["labels"] = labels

        payload = {"fields": fields}

        try:
            response = self.client.post("/issue", json=payload)
            logger.info(f"Create issue status: {response.status_code}")
            if response.status_code != 201:
                detail = self._format_jira_error_body(response)
                self.last_error = detail
                logger.warning(f"Create issue response: {detail}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            if not self.last_error:
                resp = getattr(e, "response", None)
                if resp is not None:
                    self.last_error = self._format_jira_error_body(resp)
                else:
                    self.last_error = str(e)
            logger.error(f"Error creating issue: {e}")
            return None
    
    def get_issue(
        self,
        issue_key: str,
        fields: Optional[Union[str, List[str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get issue details by key.

        ``fields`` limits payload size when only a subset is needed (e.g. description).
        Accepts a list or a single field name string.
        """
        try:
            params: Dict[str, Any] = {}
            fields_q = _jira_fields_query(fields)
            if fields_q:
                params["fields"] = fields_q
            response = self.client.get(f"/issue/{issue_key}", params=params or None)
            if response.status_code != 200:
                logger.warning(f"Get issue {issue_key}: {response.status_code}")
                logger.debug(f"Get issue response: {response.text}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching issue {issue_key}: {e}")
            return None
    
    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search issues using JQL."""
        params = {
            "jql": jql,
            "maxResults": max_results,
        }
        fields_q = _jira_fields_query(fields)
        if fields_q:
            params["fields"] = fields_q
        
        try:
            response = self.client.get("/search", params=params)
            # Cloud removed GET /rest/api/2/search (HTTP 410). On-prem still uses it.
            if response.status_code == 410:
                response = self.client.get(
                    f"{self.host}/rest/api/3/search/jql",
                    params=params,
                )
            response.raise_for_status()
            data = response.json()
            return data.get("issues", [])
        except httpx.HTTPError as e:
            logger.error(f"Error searching issues: {e}")
            return []
    
    def get_board_issues(
        self,
        board_id: str,
        fields: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> List[Dict[str, Any]]:
        """Get all issues from a Jira board (paginated)."""
        all_issues: List[Dict[str, Any]] = []
        page_start = start_at
        try:
            url = f"{self.host}/rest/agile/1.0/board/{board_id}/issue"
            while True:
                params: Dict[str, Any] = {
                    "maxResults": max_results,
                    "startAt": page_start,
                }
                fields_q = _jira_fields_query(fields)
                if fields_q:
                    params["fields"] = fields_q
                response = self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                batch = data.get("issues") or []
                all_issues.extend(batch)
                total = int(data.get("total") or 0)
                if not batch:
                    break
                # Agile often returns fewer than maxResults (e.g. 50 of 100).
                # Advance by page length, not the requested size.
                page_start += len(batch)
                if total and page_start >= total:
                    break
                # Short page is final only when the server did not advertise more.
                if len(batch) < max_results and (not total or page_start >= total):
                    break
            return all_issues
        except Exception as e:
            self.last_error = str(e)
            logger.error(f"Error getting board issues: {e}")
            return all_issues
    
    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        """Get the active sprint for a board.

        Sets ``sprint_lookup`` so the poller can tell these None cases apart:
          * ``kanban`` — HTTP 400, board does not support sprints (use board issues)
          * ``empty`` — Scrum board with no active sprint (do not widen intake)
          * ``error`` — 401/5xx/network (do not widen intake)
          * ``ok`` — returned a sprint dict
        """
        self.sprint_lookup = "error"
        try:
            # Agile API is at /rest/agile/1.0, not /rest/api/2
            url = f"{self.host}/rest/agile/1.0/board/{board_id}/sprint"
            response = self.client.get(url, params={"state": "active"})
            if response.status_code == 400:
                # e.g. "Board does not support sprints" (Kanban / team-managed simple)
                logger.info(
                    f"Board {board_id} does not support sprints "
                    f"({response.text[:200]}); use board issues instead"
                )
                self.sprint_lookup = "kanban"
                return None
            response.raise_for_status()
            data = response.json()
            values = data.get("values", [])
            if values:
                self.sprint_lookup = "ok"
                return values[0]
            self.sprint_lookup = "empty"
            return None
        except Exception as e:
            self.last_error = str(e)
            self.sprint_lookup = "error"
            logger.error(f"Error getting active sprint: {e}")
            return None

    def get_board(self, board_id: str) -> Optional[Dict[str, Any]]:
        """Get board metadata by id."""
        try:
            url = f"{self.host}/rest/agile/1.0/board/{board_id}"
            response = self.client.get(url)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error getting board {board_id}: {e}")
            return None
    
    def get_sprint_issues(
        self,
        sprint_id: int,
        fields: Optional[List[str]] = None,
        max_results: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get all issues in a sprint with pagination."""
        all_issues = []
        start_at = 0
        
        while True:
            params = {
                "startAt": start_at,
                "maxResults": max_results,
            }
            fields_q = _jira_fields_query(fields)
            if fields_q:
                params["fields"] = fields_q
            
            try:
                # Agile API is at /rest/agile/1.0, not /rest/api/2
                url = f"{self.host}/rest/agile/1.0/sprint/{sprint_id}/issue"
                response = self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                batch = data.get("issues") or []
                all_issues.extend(batch)
                if not batch:
                    break
                # Agile often returns fewer than maxResults (e.g. 50 of 100).
                # Advance by page length, not the requested size.
                start_at += len(batch)
                total = int(data.get("total") or 0)
                if total and start_at >= total:
                    break
                # Short page is final only when the server did not advertise more.
                if len(batch) < max_results and (not total or start_at >= total):
                    break
                
            except Exception as e:
                self.last_error = str(e)
                logger.error(f"Error getting sprint issues: {e}")
                break
        
        return all_issues
    
    def get_transitions(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get available transitions for an issue."""
        try:
            response = self.client.get(f"/issue/{issue_key}/transitions")
            response.raise_for_status()
            data = response.json()
            return data.get("transitions", [])
        except httpx.HTTPError as e:
            logger.error(f"Error getting transitions for {issue_key}: {e}")
            return []
    
    def do_transition(self, issue_key: str, transition_id: str) -> bool:
        """Transition an issue to a new status."""
        try:
            response = self.client.post(
                f"/issue/{issue_key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error transitioning {issue_key}: {e}")
            return False
    
    def transition_to_in_progress(self, issue_key: str) -> bool:
        """Transition an issue toward an In Progress-like status.

        * Match transition **names** (``In Progress``, ``Start Progress``,
          locale equivalents) then destination ``statusCategory=indeterminate``.
        * Same matcher for Cloud and on-prem — classic Jira Software names
          the transition ``Start Progress``, not ``In Progress``.
        """
        transitions = self.get_transitions(issue_key)
        if not transitions:
            logger.warning(f"No transitions available for {issue_key}")
            return False

        name_hints = (
            "in progress",
            "devam ediyor",
            "start progress",
            "start work",
            "başlat",
            "baslat",
            "işlemde",
            "islemde",
            "doing",
            "wip",
        )
        review_hints = (
            "review",
            "inceleme",
            "peer",
            "code review",
            "qa",
        )

        def _is_review(name: str) -> bool:
            return any(h in name for h in review_hints)

        # 1) Prefer transition name hints (exclude review)
        for t in transitions:
            name = (t.get("name") or "").lower()
            if _is_review(name):
                continue
            if any(h in name for h in name_hints):
                logger.info(
                    f"In Progress transition for {issue_key}: "
                    f"'{t.get('name')}' (id={t.get('id')})"
                )
                return self.do_transition(issue_key, t["id"])

        # 2) Prefer first non-review transition into statusCategory=indeterminate
        for t in transitions:
            name = (t.get("name") or "").lower()
            if _is_review(name):
                continue
            to = t.get("to") or {}
            cat = ((to.get("statusCategory") or {}).get("key") or "").lower()
            if cat == "indeterminate":
                logger.info(
                    f"In Progress transition for {issue_key}: '{t.get('name')}' "
                    f"(id={t.get('id')}, category=indeterminate)"
                )
                return self.do_transition(issue_key, t["id"])

        names = [t.get("name") for t in transitions]
        logger.warning(
            f"No In Progress-like transition found for {issue_key}. "
            f"Available: {names}"
        )
        return False
    
    def add_comment(self, issue_key: str, body: str) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue."""
        try:
            logger.info(f"Adding comment to {issue_key}")
            response = self.client.post(
                f"/issue/{issue_key}/comment",
                json={"body": body},
            )
            logger.debug(f"Comment status: {response.status_code}")
            if response.status_code == 400:
                alt_body = {"body": _comment_body_to_adf(body)}
                logger.warning(f"Trying alternate comment format for {issue_key}")
                response = self.client.post(
                    f"/issue/{issue_key}/comment",
                    json=alt_body,
                )
                logger.debug(f"Alternate comment status: {response.status_code}")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error adding comment to {issue_key}: {e}")
            return None
    
    def update_issue(
        self,
        issue_key: str,
        fields: Optional[Dict[str, Any]] = None,
        labels: Optional[List[str]] = None,
    ) -> bool:
        """Update issue fields.

        Note: When ``labels`` is provided it replaces the full label set.
        Prefer :meth:`add_labels` to merge with existing labels on Server/DC.
        """
        payload: Dict[str, Any] = {}
        
        if fields:
            payload["fields"] = fields
        if labels is not None:
            payload["fields"] = payload.get("fields", {})
            payload["fields"]["labels"] = labels
        
        if not payload:
            return True
        
        try:
            response = self.client.put(f"/issue/{issue_key}", json=payload)
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error updating issue {issue_key}: {e}")
            return False

    def append_to_description(self, issue_key: str, suffix: str) -> bool:
        """Append ``suffix`` to the issue description without dropping existing text.

        Used by plan mode to attach the generated plan at the end of the description.
        """
        text = (suffix or "").strip()
        if not text:
            return True
        try:
            issue = self.get_issue(issue_key, fields=["description"])
            # Fail closed: never PUT a plan-only body when we could not read
            # the current description (would wipe {params} / operator text).
            if not issue or not isinstance(issue, dict):
                logger.error(
                    f"Cannot append description on {issue_key}: get_issue failed"
                )
                return False
            fields = issue.get("fields") or {}
            if "description" not in fields:
                logger.error(
                    f"Cannot append description on {issue_key}: "
                    "description field missing from get_issue"
                )
                return False
            old = ""
            raw = fields.get("description")
            if isinstance(raw, str):
                old = raw
            elif raw is not None:
                old = str(raw)
            sep = "\n\n" if old and not old.endswith("\n") else "\n" if old else ""
            new_desc = f"{old}{sep}{text}"
            return self.update_issue(issue_key, fields={"description": new_desc})
        except Exception as e:
            logger.error(f"Error appending description on {issue_key}: {e}")
            return False

    def add_labels(self, issue_key: str, labels: List[str]) -> bool:
        """Merge labels into an issue without dropping existing ones (on-prem safe)."""
        if not labels:
            return True

        issue = self.get_issue(issue_key, fields=["labels"])
        # Fail closed: never PUT only the new labels when we could not read
        # the current set (would drop bot / ai-assist).
        if not issue or not isinstance(issue, dict):
            logger.error(f"Cannot add labels on {issue_key}: get_issue failed")
            return False
        fields = issue.get("fields") or {}
        if "labels" not in fields:
            logger.error(
                f"Cannot add labels on {issue_key}: labels field missing from get_issue"
            )
            return False
        raw = fields.get("labels") or []
        existing = [str(x) for x in raw] if isinstance(raw, list) else []
        merged = list(dict.fromkeys([*existing, *labels]))
        return self.update_issue(issue_key, labels=merged)
    
    def transition_issue(
        self,
        issue_key: str,
        transition_name: str,
    ) -> bool:
        """Transition issue to a new status."""
        try:
            # First get available transitions
            response = self.client.get(f"/issue/{issue_key}/transitions")
            response.raise_for_status()
            transitions = response.json().get("transitions", [])
            
            # Find transition by name
            transition_id = None
            for t in transitions:
                if t["name"].lower() == transition_name.lower():
                    transition_id = t["id"]
                    break
            
            if not transition_id:
                logger.warning(f"Transition '{transition_name}' not found for {issue_key}")
                return False
            
            # Perform transition
            response = self.client.post(
                f"/issue/{issue_key}/transitions",
                json={"transition": {"id": transition_id}},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.error(f"Error transitioning issue {issue_key}: {e}")
            return False
    
    def get_comments(self, issue_key: str) -> List[Dict[str, Any]]:
        """Get all comments for an issue."""
        try:
            response = self.client.get(f"/issue/{issue_key}/comment")
            response.raise_for_status()
            return response.json().get("comments", [])
        except httpx.HTTPError as e:
            logger.error(f"Error fetching comments for {issue_key}: {e}")
            return []
    
    def get_myself(self) -> Optional[Dict[str, Any]]:
        """Current user (Server ``name`` / Cloud ``accountId``)."""
        try:
            response = self.client.get("/myself")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        except httpx.HTTPError as e:
            logger.error(f"Error fetching Jira myself: {e}")
            return None

    def list_webhooks(self) -> List[Dict[str, Any]]:
        """Admin webhook list (Server/DC 9.4 + Cloud ``/rest/webhooks/1.0``)."""
        try:
            response = self.client.get(f"{self.host}/rest/webhooks/1.0/webhook")
            if response.status_code == 404:
                return []
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                values = data.get("values") or data.get("webhooks") or []
                if isinstance(values, list):
                    return [x for x in values if isinstance(x, dict)]
            return []
        except httpx.HTTPError as e:
            logger.error(f"Error listing Jira webhooks: {e}")
            return []

    def assign_issue(self, issue_key: str, username: str) -> bool:
        """Assign issue to a user.

        Server/DC 9.4 uses ``assignee.name``. Cloud uses ``accountId`` (a
        display name is resolved via ``/user/search`` when needed).
        """
        ident = (username or "").strip()
        if not ident:
            return False
        if self.is_cloud:
            account_id = ident
            looks_like_id = ":" in ident or (len(ident) >= 16 and "-" in ident)
            if not looks_like_id:
                resolved = self._lookup_cloud_account_id(ident)
                if resolved:
                    account_id = resolved
            return self.update_issue(
                issue_key,
                fields={"assignee": {"accountId": account_id}},
            )
        return self.update_issue(
            issue_key,
            fields={"assignee": {"name": ident}},
        )

    def _lookup_cloud_account_id(self, query: str) -> str:
        """Best-effort Cloud user search → accountId."""
        q = (query or "").strip()
        if not q:
            return ""
        for path in (f"/user/search?query={q}", f"/user/search?username={q}"):
            try:
                response = self.client.get(path)
                if response.status_code != 200:
                    continue
                data = response.json()
                rows = data if isinstance(data, list) else data.get("values") or []
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    aid = str(row.get("accountId") or "").strip()
                    if aid:
                        return aid
            except Exception:
                continue
        return ""
    
    def add_attachment(
        self,
        issue_key: str,
        file_path: str,
        filename: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Attach a file to an issue."""
        from pathlib import Path
        
        path = Path(file_path)
        if not path.exists():
            logger.error(f"File not found: {file_path}")
            return None
        
        try:
            with open(path, "rb") as f:
                files = {"file": (filename or path.name, f)}
                response = self.client.post(
                    f"/issue/{issue_key}/attachments",
                    files=files,
                    headers={
                        "X-Atlassian-Token": "no-check",
                        # Override client default application/json so multipart works
                        "Content-Type": None,
                    },
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Error attaching file to {issue_key}: {e}")
            return None
    
    def close(self):
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


def create_jira_client(simulated: bool = False):
    """Factory function to create either real or simulated JIRA client.
    
    Args:
        simulated: If True, returns SimulatedJiraClient for local testing
        
    Returns:
        Either JiraClient (real) or SimulatedJiraClient (simulated)
    """
    if simulated or not settings.is_configured():
        from src.jira.simulated_client import SimulatedJiraClient
        return SimulatedJiraClient()
    return JiraClient()
