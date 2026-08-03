"""JIRA API client wrapper."""

from typing import Any, Dict, List, Optional, Union

import httpx

from src.config import settings
from src.logger import logger


class JiraClient:
    """Client for JIRA REST API.

    Auth (always Bearer — no email check):
      ``JIRA_HOST`` + ``JIRA_API_TOKEN`` → ``Authorization: Bearer {token}``

    Dashboard settings and runtime clients never switch to HTTP Basic based on
    email. ``email`` is accepted for API compatibility only and is not used
    for authentication.
    """
    
    def __init__(
        self,
        host: Optional[str] = None,
        api_token: Optional[str] = None,
        email: Optional[str] = None,
    ):
        self.host = (host or settings.jira_host).rstrip("/")
        self.api_token = api_token if api_token is not None else settings.jira_api_token
        # Stored for callers/tests that still read it; never used for HTTP auth
        self.email = (email if email is not None else getattr(settings, "jira_email", "")) or ""
        
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
            logger.info("JiraClient auth: Bearer token")
        
        self.client = httpx.Client(
            base_url=f"{self.host}/rest/api/2",
            headers=headers,
            timeout=30.0,
            verify=False,
        )
        self.is_cloud = "atlassian.net" in self.host.lower()
        # Last create_issue failure detail (for callers that soft-map None)
        self.last_error: Optional[str] = None

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
        fields: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get issue details by key.

        ``fields`` limits payload size when only a subset is needed (e.g. description).
        """
        try:
            params: Dict[str, Any] = {}
            if fields:
                params["fields"] = ",".join(fields)
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
        if fields:
            params["fields"] = ",".join(fields)
        
        try:
            response = self.client.get("/search", params=params)
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
                if fields:
                    params["fields"] = ",".join(fields)
                response = self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                batch = data.get("issues") or []
                all_issues.extend(batch)
                total = int(data.get("total") or 0)
                if not batch:
                    break
                page_start += len(batch)
                if total and page_start >= total:
                    break
                if len(batch) < max_results:
                    break
            return all_issues
        except httpx.HTTPError as e:
            logger.error(f"Error getting board issues: {e}")
            return all_issues
    
    def get_active_sprint(self, board_id: str) -> Optional[Dict[str, Any]]:
        """Get the active sprint for a board.

        Returns None for Kanban/simple boards that do not support sprints
        (HTTP 400) or when no active sprint exists.
        """
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
                return None
            response.raise_for_status()
            data = response.json()
            values = data.get("values", [])
            if values:
                return values[0]
            return None
        except httpx.HTTPError as e:
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
            if fields:
                params["fields"] = ",".join(fields)
            
            try:
                # Agile API is at /rest/agile/1.0, not /rest/api/2
                url = f"{self.host}/rest/agile/1.0/sprint/{sprint_id}/issue"
                response = self.client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                issues = data.get("issues", [])
                all_issues.extend(issues)
                
                # Check if there are more issues
                total = data.get("total", 0)
                if start_at + max_results >= total:
                    break
                start_at += max_results
                
            except httpx.HTTPError as e:
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

        * **On-prem (unchanged):** transition whose name contains ``in progress``.
        * **Cloud (atlassian.net):** also match locale names (e.g. Turkish
          ``Devam Ediyor``) and prefer destination statusCategory
          ``indeterminate``, skipping review-like transitions.
        """
        transitions = self.get_transitions(issue_key)
        if not transitions:
            logger.warning(f"No transitions available for {issue_key}")
            return False

        # --- On-prem / default: exact previous behaviour ---
        if not self.is_cloud:
            for t in transitions:
                if "in progress" in t["name"].lower():
                    return self.do_transition(issue_key, t["id"])
            logger.warning(f"No 'In Progress' transition found for {issue_key}")
            return False

        # --- Cloud: locale-safe matching ---
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
                    f"Cloud transition for {issue_key}: '{t.get('name')}' (id={t.get('id')})"
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
                    f"Cloud transition for {issue_key}: '{t.get('name')}' "
                    f"(id={t.get('id')}, category=indeterminate)"
                )
                return self.do_transition(issue_key, t["id"])

        names = [t.get("name") for t in transitions]
        logger.warning(
            f"No In Progress-like transition found for {issue_key} "
            f"(cloud). Available: {names}"
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
                alt_body = {"body": {"version": 1, "type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}]}}
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
            issue = self.get_issue(issue_key, fields="description")
            old = ""
            if issue and isinstance(issue, dict):
                raw = (issue.get("fields") or {}).get("description")
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

        existing: List[str] = []
        issue = self.get_issue(issue_key)
        if issue and isinstance(issue, dict):
            raw = (issue.get("fields") or {}).get("labels") or []
            if isinstance(raw, list):
                existing = [str(x) for x in raw]

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
    
    def assign_issue(self, issue_key: str, username: str) -> bool:
        """Assign issue to a user (on-prem Server/DC uses assignee.name)."""
        return self.update_issue(
            issue_key,
            fields={"assignee": {"name": username}},
        )
    
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
                    headers={"X-Atlassian-Token": "no-check"},
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
