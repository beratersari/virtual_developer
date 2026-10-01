"""Persistent job history: each processing run for a Jira issue is one job."""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.state.job_index import JobIndex, default_index_path


def _default_jobs_dir() -> Path:
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("jobs")


def extract_task_description_from_prompt(text: str) -> str:
    """Pull the Jira task body frozen into an agent prompt file.

    PromptBuilder embeds the issue description under ``## Task`` (direct) or
    similar sections. Used to recover per-job description for older jobs that
    never stored ``description`` on the job record.
    """
    if not text or not text.strip():
        return ""
    # Direct / common: "## Task\n<body>\n\n# ..."
    m = re.search(
        r"(?im)^##\s+Task\s*\n(.*?)(?=\n##\s|\n#\s+[A-Z]|\Z)",
        text,
        re.DOTALL,
    )
    if m:
        body = m.group(1).strip()
        if body:
            return body
    # Planning-style: "## Issue Description" / "## Description"
    for heading in (
        r"##\s+Issue\s+Description",
        r"##\s+Description",
        r"##\s+JIRA\s+Description",
    ):
        m = re.search(
            rf"(?im)^{heading}\s*\n(.*?)(?=\n##\s|\n#\s+[A-Z]|\Z)",
            text,
            re.DOTALL,
        )
        if m:
            body = m.group(1).strip()
            if body:
                return body
    return ""


def description_from_prompt_path(prompt_path: Optional[str]) -> str:
    """Read a prompt file and extract the task description, or ''.

    Only reads agent session/job artifacts — not arbitrary filesystem paths
    that might appear in a compromised job JSON record.
    """
    if not prompt_path:
        return ""
    try:
        path = Path(prompt_path).resolve()
        if not path.is_file():
            return ""

        def _under(root: Path) -> bool:
            try:
                path.relative_to(root.resolve())
                return True
            except ValueError:
                return False

        from src.paths import under_agent_data

        allowed = under_agent_data(path) or _under(_default_jobs_dir())
        if not allowed:
            logger.debug(f"Refusing prompt path outside agent dirs: {path}")
            return ""
        # Block obvious system locations even if named sessions/
        blocked = {"etc", "proc", "sys", "windows", "system32"}
        if blocked & {p.lower() for p in path.parts}:
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return extract_task_description_from_prompt(text)
    except OSError as e:
        logger.debug(f"Could not read prompt for description: {prompt_path}: {e}")
        return ""


class JobStore:
    """Agent jobs stored in yaver.sqlite.

    ``get_job`` reads the document column. Leftover ``job_*.json`` is
    imported once by ``ensure_index``, then deleted. A failed write does
    not fall back to a JSON file.
    """

    def __init__(self, jobs_dir: Optional[Path] = None) -> None:
        self.jobs_dir = jobs_dir or _default_jobs_dir()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index: Optional[JobIndex] = None
        self._index_ready = False
        try:
            self._index = JobIndex(default_index_path(self.jobs_dir))
        except Exception as e:
            logger.warning(f"Job database unavailable: {e}")
            self._index = None

    def ensure_index(self) -> int:
        """Import leftover job JSON once. Later calls do not scan the folder.

        Called on daemon start and on the first list/count/Analytics walk.
        A failed import leaves the flag unset so the next call can retry.
        Rows already written stay readable.
        """
        if self._index is None:
            return 0
        with self._lock:
            if self._index_ready:
                return 0
            try:
                n = self._index.reconcile(self.jobs_dir)
            except Exception as e:
                logger.warning(f"Job import failed: {e}")
                return 0
            self._index_ready = True
            if n:
                logger.info(f"Imported {n} job(s) into yaver.sqlite")
            return n

    def _path(self, job_id: str) -> Path:
        safe = job_id.replace("/", "_").replace("\\", "_")
        return self.jobs_dir / f"{safe}.json"

    def create_job(
        self,
        *,
        issue_key: str,
        summary: str = "",
        description: str = "",
        workflow_type: str = "direct",
        agent: str = "",
        task_id: Optional[str] = None,
        status: str = "running",
        source: str = "jira",
        merge_request_url: Optional[str] = None,
        gitlab_project: Optional[str] = None,
        gitlab_mr_iid: Optional[int] = None,
        azure_project: Optional[str] = None,
        azure_pr_id: Optional[int] = None,
        repository_url: Optional[str] = None,
        model: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a job snapshot for one agent run.

        ``summary`` and ``description`` are frozen at start time so later Jira
        edits / reprocess do not rewrite history for this job.
        ``model`` is the worker model id used for this run.
        ``backend`` is ``opencode`` or ``codex`` (empty = infer later).
        """
        src = (source or "jira").strip().lower() or "jira"
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")
        model_id = (model or "").strip() or None
        backend_id = (backend or "").strip() or None
        job: Dict[str, Any] = {
            "job_id": job_id,
            "issue_key": issue_key,
            "summary": summary or "",
            "description": description or "",
            "workflow_type": workflow_type or "direct",
            "agent": agent or "",
            "model": model_id,
            "backend": backend_id,
            "status": status,
            "source": src,
            "gitlab_project": gitlab_project or None,
            "gitlab_mr_iid": gitlab_mr_iid,
            "azure_project": azure_project or None,
            "azure_pr_id": azure_pr_id,
            "repository_url": (repository_url or "").strip() or None,
            "task_id": task_id,
            "task_ids": [task_id] if task_id else [],
            "opencode_session_id": None,
            "opencode_session_ids": [],
            "session_log_path": None,
            # All OpenCode session logs for this job (initial + _retryN), ordered
            "session_log_paths": [],
            "prompt_path": None,
            "prompt_paths": [],
            # Failed-attempt bookkeeping nested under this job (not separate jobs)
            "retry_attempts": [],
            "progress_percentage": 0,
            "error_message": None,
            "started_at": now,
            "completed_at": None,
            "updated_at": now,
        }
        if merge_request_url:
            job["merge_request_url"] = merge_request_url
        if not self._write(job):
            return None
        logger.info(
            f"Job created: {job_id} issue={issue_key} workflow={workflow_type} "
            f"source={src}"
        )
        return job

    def update_job(self, job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self.get_job(job_id)
            if not job:
                return None
            for key, value in fields.items():
                if key == "job_id":
                    continue
                if key == "opencode_session_id" and value:
                    ids = list(job.get("opencode_session_ids") or [])
                    if value not in ids:
                        ids.append(value)
                    job["opencode_session_ids"] = ids
                    job["opencode_session_id"] = value
                elif key == "task_id" and value:
                    # Append history; keep latest as task_id for this job
                    tids = list(job.get("task_ids") or [])
                    if value not in tids:
                        tids.append(value)
                    job["task_ids"] = tids
                    job["task_id"] = value
                elif key == "session_log_path" and value:
                    paths = list(job.get("session_log_paths") or [])
                    if value not in paths:
                        paths.append(value)
                    job["session_log_paths"] = paths
                    job["session_log_path"] = value  # latest
                elif key == "prompt_path" and value:
                    paths = list(job.get("prompt_paths") or [])
                    if value not in paths:
                        paths.append(value)
                    job["prompt_paths"] = paths
                    job["prompt_path"] = value  # latest
                elif key == "deliveries" and isinstance(value, list):
                    job["deliveries"] = value
                elif key == "retry_attempt" and isinstance(value, dict):
                    # Append one failed-attempt record under this job
                    history = list(job.get("retry_attempts") or [])
                    history.append(value)
                    job["retry_attempts"] = history
                elif key == "session_log_paths" and isinstance(value, list):
                    # Merge unique paths preserving order
                    existing = list(job.get("session_log_paths") or [])
                    for p in value:
                        if p and p not in existing:
                            existing.append(p)
                    job["session_log_paths"] = existing
                    if existing:
                        job["session_log_path"] = existing[-1]
                else:
                    job[key] = value
            job["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if not self._write(job):
                return None
            return job

    def apply_review_state(
        self,
        job_id: str,
        *,
        state: str,
        mr_url: str = "",
        project_path: str = "",
        mr_iid: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Set one review's state without dropping the other deliveries.

        ``list_jobs`` can return a snapshot taken before another repository's
        delivery was saved. Re-read this job under the store lock and mark
        only the matching row.
        """
        jid = (job_id or "").strip()
        if not jid:
            return None
        url = (mr_url or "").strip().rstrip("/")
        with self._lock:
            current = self.get_job(jid)
            if not current:
                return None
            deliveries: List[Any] = []
            for row in current.get("deliveries") or []:
                if not isinstance(row, dict):
                    deliveries.append(row)
                    continue
                copied = dict(row)
                row_url = str(copied.get("merge_request_url") or "").rstrip("/")
                if url and row_url == url:
                    copied["merge_request_state"] = state
                deliveries.append(copied)
            fields: Dict[str, Any] = {"merge_request_state": state}
            if deliveries:
                fields["deliveries"] = deliveries
            if url:
                fields["merge_request_url"] = url
            if project_path:
                fields["gitlab_project"] = project_path
            if mr_iid:
                fields["gitlab_mr_iid"] = int(mr_iid)
            return self.update_job(jid, **fields)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        jid = (job_id or "").strip()
        if not jid or self._index is None:
            return None
        self.ensure_index()
        try:
            return self._index.get(jid)
        except Exception as e:
            logger.error(f"Error loading job {job_id}: {e}")
            return None

    def delete_job(self, job_id: str) -> bool:
        """Remove the job row. Returns True when a row was deleted.

        Does not delete session logs or prompt files — callers handle artifacts.
        Import runs first so a not-yet-imported JSON file cannot recreate the row.
        """
        jid = (job_id or "").strip()
        if not jid or not jid.startswith("job_"):
            return False
        self.ensure_index()
        if self._index is None:
            return False
        with self._lock:
            try:
                removed = self._index.delete(jid)
            except Exception as e:
                logger.error(f"Error deleting job {jid}: {e}")
                return False
        if removed:
            logger.info(f"Job deleted: {jid}")
        return removed

    def iter_jobs(self) -> List[Dict[str, Any]]:
        """Every job (unsorted). Analytics walks this set."""
        self.ensure_index()
        if self._index is None:
            return []
        try:
            return self._index.iter_jobs()
        except Exception as e:
            logger.debug(f"Job iter failed: {e}")
            return []

    def min_job_when(self) -> Optional[str]:
        self.ensure_index()
        if self._index is None:
            return None
        try:
            return self._index.min_when()
        except Exception as e:
            logger.debug(f"Job index min_when failed: {e}")
            return None

    def query_jobs(self, **filters: Any) -> List[Dict[str, Any]]:
        """Analytics rows with period/filters applied in SQLite."""
        self.ensure_index()
        if self._index is None:
            return []
        try:
            return self._index.query_jobs(**filters)
        except Exception as e:
            logger.debug(f"Job query failed: {e}")
            return []

    def list_jobs(
        self,
        *,
        issue_key: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Return jobs newest-first, optional filter by issue key (case-insensitive).

        ``offset`` skips that many rows after sorting (for pagination).
        """
        self.ensure_index()
        if self._index is None:
            return []
        try:
            return self._index.list_records(
                issue_key=issue_key, limit=limit, offset=offset
            )
        except Exception as e:
            logger.debug(f"Job list failed: {e}")
            return []

    def count_jobs(self, *, issue_key: Optional[str] = None) -> int:
        """Count stored jobs matching optional issue filter."""
        self.ensure_index()
        if self._index is None:
            return 0
        try:
            return self._index.count(issue_key=issue_key)
        except Exception as e:
            logger.debug(f"Job count failed: {e}")
            return 0

    def active_job_for_issue(self, issue_key: str) -> Optional[Dict[str, Any]]:
        for job in self.list_jobs(issue_key=issue_key, limit=50):
            if job.get("status") in ("running", "planning", "executing", "pending"):
                return job
        return None

    def ensure_description(
        self,
        job: Dict[str, Any],
        *,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Fill empty description from prompt_path (and optionally save)."""
        if (job.get("description") or "").strip():
            return job
        prompt_path = job.get("prompt_path")
        if not prompt_path and job.get("session_log_path"):
            # Sibling of session log: foo.log → foo.prompt.txt
            try:
                log = Path(str(job["session_log_path"]))
                candidate = log.parent / f"{log.stem}.prompt.txt"
                if candidate.is_file():
                    prompt_path = str(candidate)
            except Exception:
                prompt_path = None
        desc = description_from_prompt_path(prompt_path)
        if not desc:
            return job
        job = {**job, "description": desc}
        if prompt_path and not job.get("prompt_path"):
            job["prompt_path"] = prompt_path
        jid = job.get("job_id") or ""
        # Only persist real job records (not synthetic legacy_* rows)
        if persist and jid.startswith("job_"):
            self.update_job(
                jid,
                description=desc,
                **({"prompt_path": prompt_path} if prompt_path else {}),
            )
        return job

    def _write(self, job: Dict[str, Any]) -> bool:
        if self._index is None:
            logger.error(
                f"Error saving job {job.get('job_id')}: database unavailable"
            )
            return False
        with self._lock:
            try:
                self._index.upsert(job)
            except Exception as e:
                logger.error(f"Error saving job {job.get('job_id')}: {e}")
                return False
        return True


# Process-wide default store
job_store = JobStore()
