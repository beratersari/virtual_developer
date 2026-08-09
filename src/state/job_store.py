"""Persistent job history: each processing run for a Jira issue is one job."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.logger import logger


def _default_jobs_dir() -> Path:
    return Path.cwd() / ".jira-agent" / "jobs"


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

        allowed = _under(Path.cwd() / ".jira-agent") or _under(_default_jobs_dir())
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
    """File-backed store of agent jobs (one JSON file per job)."""

    def __init__(self, jobs_dir: Optional[Path] = None) -> None:
        self.jobs_dir = jobs_dir or _default_jobs_dir()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

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
    ) -> Dict[str, Any]:
        """Create a job snapshot for one agent run.

        ``summary`` and ``description`` are frozen at start time so later Jira
        edits / reprocess do not rewrite history for this job.
        """
        src = (source or "jira").strip().lower() or "jira"
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")
        job: Dict[str, Any] = {
            "job_id": job_id,
            "issue_key": issue_key,
            "summary": summary or "",
            "description": description or "",
            "workflow_type": workflow_type or "direct",
            "agent": agent or "",
            "status": status,
            "source": src,
            "gitlab_project": gitlab_project or None,
            "gitlab_mr_iid": gitlab_mr_iid,
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
        self._write(job)
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
            self._write(job)
            return job

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading job {job_id}: {e}")
            return None

    def delete_job(self, job_id: str) -> bool:
        """Remove the job JSON file. Returns True if a file was deleted.

        Does not delete session logs or prompt files — callers handle artifacts.
        """
        jid = (job_id or "").strip()
        if not jid or not jid.startswith("job_"):
            return False
        path = self._path(jid)
        with self._lock:
            if not path.is_file():
                return False
            try:
                path.unlink()
                logger.info(f"Job deleted: {jid}")
                return True
            except OSError as e:
                logger.error(f"Error deleting job {jid}: {e}")
                return False

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
        jobs: List[Dict[str, Any]] = []
        if not self.jobs_dir.is_dir():
            return jobs
        needle = (issue_key or "").strip().upper()
        for path in self.jobs_dir.glob("job_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    job = json.load(f)
                if needle and (job.get("issue_key") or "").upper() != needle:
                    continue
                jobs.append(job)
            except Exception as e:
                logger.error(f"Error loading {path}: {e}")
        jobs.sort(key=lambda j: j.get("started_at") or j.get("updated_at") or "", reverse=True)
        off = max(0, int(offset or 0))
        lim = max(1, int(limit or 1))
        return jobs[off : off + lim]

    def count_jobs(self, *, issue_key: Optional[str] = None) -> int:
        """Count stored jobs matching optional issue filter."""
        if not self.jobs_dir.is_dir():
            return 0
        needle = (issue_key or "").strip().upper()
        n = 0
        for path in self.jobs_dir.glob("job_*.json"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    job = json.load(f)
                if needle and (job.get("issue_key") or "").upper() != needle:
                    continue
                n += 1
            except Exception:
                continue
        return n

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

    def _write(self, job: Dict[str, Any]) -> None:
        path = self._path(job["job_id"])
        tmp = path.with_suffix(".tmp")
        with self._lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(job, f, indent=2, ensure_ascii=False)
                tmp.replace(path)
            except Exception as e:
                logger.error(f"Error saving job {job.get('job_id')}: {e}")
                try:
                    if tmp.is_file():
                        tmp.unlink()
                except OSError:
                    pass


# Process-wide default store
job_store = JobStore()
