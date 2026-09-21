"""Local SQLite index of job JSON files (Analytics / Jobs list).

One file per data dir, created on first open. JSON remains the full
record; this table is columns used for list, count, and charts.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    issue_key TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    workflow_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT '',
    repository_url TEXT NOT NULL DEFAULT '',
    merge_request_url TEXT NOT NULL DEFAULT '',
    merge_request_state TEXT NOT NULL DEFAULT '',
    gitlab_project TEXT NOT NULL DEFAULT '',
    gitlab_mr_iid INTEGER,
    azure_project TEXT NOT NULL DEFAULT '',
    azure_pr_id INTEGER,
    started_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_jobs_issue ON jobs(issue_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_WHEN_SQL = """CASE
    WHEN TRIM(IFNULL(started_at,'')) != '' THEN started_at
    WHEN TRIM(IFNULL(updated_at,'')) != '' THEN updated_at
    WHEN TRIM(IFNULL(completed_at,'')) != '' THEN completed_at
    ELSE ''
END"""

_CATEGORY_SQL = """CASE
    WHEN LOWER(REPLACE(IFNULL(workflow_type,''), '_', '-')) IN ('planning', 'plan')
        THEN 'plan'
    WHEN LOWER(REPLACE(IFNULL(workflow_type,''), '_', '-')) IN ('testing', 'test')
        THEN 'test'
    WHEN LOWER(REPLACE(IFNULL(workflow_type,''), '_', '-'))
        IN ('review', 'gitlab-review', 'azure-review') THEN 'review'
    WHEN LOWER(REPLACE(IFNULL(workflow_type,''), '_', '-'))
        IN ('execution', 'build', 'direct', 'gitlab-mr', 'azure-pr') THEN 'build'
    ELSE 'other'
END"""

_SOURCE_SQL = """CASE
    WHEN LOWER(IFNULL(source, '')) IN ('gitlab_mr', 'gitlab-mr') THEN 'gitlab'
    WHEN LOWER(IFNULL(source, '')) IN ('azure_pr', 'azure-pr', 'azure_workitem')
        THEN 'azure'
    WHEN TRIM(IFNULL(source, '')) = '' THEN 'jira'
    ELSE LOWER(source)
END"""

_STATUS_GROUPS = {
    "completed": ("completed",),
    "error": ("error", "unknown"),
    "cancelled": ("cancelled", "canceled", "superseded"),
    "plan_ready": ("plan_ready",),
    "in_flight": ("pending", "planning", "executing", "running"),
}


def _csv_set(raw: Optional[str]) -> set[str]:
    if not raw:
        return set()
    return {p.strip().lower() for p in str(raw).split(",") if p.strip()}


def _in_clause(column: str, values: List[str], args: List[Any]) -> str:
    placeholders = ",".join("?" for _ in values)
    args.extend(values)
    return f"{column} IN ({placeholders})"


_UPSERT = """
INSERT INTO jobs (
    job_id, issue_key, summary, description, status, workflow_type, source,
    agent, model, backend, repository_url, merge_request_url,
    merge_request_state, gitlab_project, gitlab_mr_iid, azure_project,
    azure_pr_id, started_at, updated_at, completed_at
) VALUES (
    :job_id, :issue_key, :summary, :description, :status, :workflow_type,
    :source, :agent, :model, :backend, :repository_url, :merge_request_url,
    :merge_request_state, :gitlab_project, :gitlab_mr_iid, :azure_project,
    :azure_pr_id, :started_at, :updated_at, :completed_at
)
ON CONFLICT(job_id) DO UPDATE SET
    issue_key=excluded.issue_key,
    summary=excluded.summary,
    description=excluded.description,
    status=excluded.status,
    workflow_type=excluded.workflow_type,
    source=excluded.source,
    agent=excluded.agent,
    model=excluded.model,
    backend=excluded.backend,
    repository_url=excluded.repository_url,
    merge_request_url=excluded.merge_request_url,
    merge_request_state=excluded.merge_request_state,
    gitlab_project=excluded.gitlab_project,
    gitlab_mr_iid=excluded.gitlab_mr_iid,
    azure_project=excluded.azure_project,
    azure_pr_id=excluded.azure_pr_id,
    started_at=excluded.started_at,
    updated_at=excluded.updated_at,
    completed_at=excluded.completed_at
"""


def default_index_path(jobs_dir: Path) -> Path:
    return jobs_dir.parent / "jobs.sqlite"


def _text(job: Dict[str, Any], key: str) -> str:
    return str(job.get(key) or "").strip()


def _int(job: Dict[str, Any], key: str) -> Optional[int]:
    raw = job.get(key)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def row_params(job: Dict[str, Any]) -> Dict[str, Any]:
    jid = _text(job, "job_id")
    started = _text(job, "started_at") or _text(job, "created_at")
    return {
        "job_id": jid,
        "issue_key": _text(job, "issue_key"),
        "summary": _text(job, "summary"),
        "description": _text(job, "description"),
        "status": _text(job, "status"),
        "workflow_type": _text(job, "workflow_type"),
        "source": _text(job, "source") or "jira",
        "agent": _text(job, "agent"),
        "model": _text(job, "model"),
        "backend": _text(job, "backend"),
        "repository_url": _text(job, "repository_url"),
        "merge_request_url": _text(job, "merge_request_url"),
        "merge_request_state": _text(job, "merge_request_state"),
        "gitlab_project": _text(job, "gitlab_project"),
        "gitlab_mr_iid": _int(job, "gitlab_mr_iid"),
        "azure_project": _text(job, "azure_project"),
        "azure_pr_id": _int(job, "azure_pr_id"),
        "started_at": started,
        "updated_at": _text(job, "updated_at") or started,
        "completed_at": _text(job, "completed_at"),
    }


def row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    job: Dict[str, Any] = {
        "job_id": row["job_id"],
        "issue_key": row["issue_key"] or "",
        "summary": row["summary"] or "",
        "description": row["description"] or "",
        "status": row["status"] or "",
        "workflow_type": row["workflow_type"] or "",
        "source": row["source"] or "jira",
        "agent": row["agent"] or "",
        "model": row["model"] or None,
        "backend": row["backend"] or None,
        "repository_url": row["repository_url"] or None,
        "merge_request_url": row["merge_request_url"] or None,
        "merge_request_state": row["merge_request_state"] or None,
        "gitlab_project": row["gitlab_project"] or None,
        "gitlab_mr_iid": row["gitlab_mr_iid"],
        "azure_project": row["azure_project"] or None,
        "azure_pr_id": row["azure_pr_id"],
        "started_at": row["started_at"] or "",
        "updated_at": row["updated_at"] or "",
        "completed_at": row["completed_at"] or None,
    }
    if not job["model"]:
        job["model"] = None
    if not job["backend"]:
        job["backend"] = None
    if not job["repository_url"]:
        job["repository_url"] = None
    if not job["merge_request_url"]:
        job["merge_request_url"] = None
    if not job["completed_at"]:
        job["completed_at"] = None
    return job


class JobIndex:
    """WAL SQLite next to ``jobs/``. Safe for one daemon per data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def upsert(self, job: Dict[str, Any]) -> None:
        params = row_params(job)
        if not params["job_id"]:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute(_UPSERT, params)
            self._conn.commit()

    def delete(self, job_id: str) -> None:
        jid = (job_id or "").strip()
        if not jid:
            return
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (jid,))
            self._conn.commit()

    def list_ids(
        self,
        *,
        issue_key: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[str]:
        needle = (issue_key or "").strip().upper()
        off = max(0, int(offset or 0))
        lim = max(1, int(limit or 1))
        sql = (
            "SELECT job_id FROM jobs WHERE UPPER(issue_key) = ? "
            "ORDER BY started_at DESC, job_id DESC LIMIT ? OFFSET ?"
            if needle
            else "SELECT job_id FROM jobs ORDER BY started_at DESC, job_id DESC "
            "LIMIT ? OFFSET ?"
        )
        args: tuple[Any, ...] = (needle, lim, off) if needle else (lim, off)
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, args).fetchall()
        return [str(r["job_id"]) for r in rows]

    def count(self, *, issue_key: Optional[str] = None) -> int:
        needle = (issue_key or "").strip().upper()
        with self._lock:
            assert self._conn is not None
            if needle:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM jobs WHERE UPPER(issue_key) = ?",
                    (needle,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"] if row else 0)

    def iter_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT * FROM jobs").fetchall()
        return [row_to_job(r) for r in rows]

    def min_when(self) -> Optional[str]:
        sql = f"SELECT MIN({_WHEN_SQL}) AS t FROM jobs WHERE {_WHEN_SQL} != ''"
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(sql).fetchone()
        val = str(row["t"] or "").strip() if row else ""
        return val or None

    def query_jobs(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        status: str = "",
        category: str = "",
        source: str = "",
        model: str = "",
        backend: str = "",
        agent: str = "",
        repository: str = "",
        issue_key: str = "",
        q: str = "",
    ) -> List[Dict[str, Any]]:
        """Rows matching period bounds and Analytics filters (SQL WHERE)."""
        where: List[str] = [f"{_WHEN_SQL} != ''"]
        args: List[Any] = []
        start_s = (start or "").strip()
        end_s = (end or "").strip()
        if start_s:
            where.append(f"{_WHEN_SQL} >= ?")
            args.append(start_s)
        if end_s:
            where.append(f"{_WHEN_SQL} <= ?")
            args.append(end_s)

        want_status = _csv_set(status)
        if want_status:
            expanded: set[str] = set()
            for item in want_status:
                expanded.add(item)
                expanded.update(_STATUS_GROUPS.get(item, ()))
            where.append(_in_clause("LOWER(IFNULL(status,''))", sorted(expanded), args))

        want_cat = _csv_set(category)
        if want_cat:
            where.append(_in_clause(_CATEGORY_SQL, sorted(want_cat), args))

        want_src = _csv_set(source)
        if want_src:
            where.append(_in_clause(_SOURCE_SQL, sorted(want_src), args))

        want_model = _csv_set(model)
        if want_model:
            where.append(
                _in_clause(
                    "LOWER(CASE WHEN TRIM(IFNULL(model,'')) = '' THEN '(unset)' "
                    "ELSE model END)",
                    sorted(want_model),
                    args,
                )
            )

        want_backend = _csv_set(backend)
        if want_backend:
            where.append(
                _in_clause(
                    "LOWER(CASE WHEN TRIM(IFNULL(backend,'')) = '' THEN '(unset)' "
                    "ELSE backend END)",
                    sorted(want_backend),
                    args,
                )
            )

        want_agent = {p.strip().lower() for p in (agent or "").split(",") if p.strip()}
        if want_agent:
            where.append(
                _in_clause(
                    "LOWER(CASE WHEN TRIM(IFNULL(agent,'')) = '' THEN '(unset)' "
                    "ELSE agent END)",
                    sorted(want_agent),
                    args,
                )
            )

        want_repo = [p.strip() for p in str(repository or "").split(",") if p.strip()]
        if want_repo:
            repo_bits = []
            for needle in want_repo:
                text = needle.strip().rstrip("/").lower()
                if text.endswith(".git"):
                    text = text[:-4].rstrip("/")
                repo_bits.append("LOWER(IFNULL(repository_url,'')) LIKE ?")
                args.append(f"%{text}%")
            where.append("(" + " OR ".join(repo_bits) + ")")

        want_keys = [p.strip().upper() for p in (issue_key or "").split(",") if p.strip()]
        if want_keys:
            where.append(_in_clause("UPPER(IFNULL(issue_key,''))", want_keys, args))

        search = (q or "").strip().lower()
        if search:
            like = f"%{search}%"
            where.append(
                "("
                "LOWER(IFNULL(issue_key,'')) LIKE ? OR "
                "LOWER(IFNULL(summary,'')) LIKE ? OR "
                "LOWER(IFNULL(description,'')) LIKE ? OR "
                "LOWER(IFNULL(repository_url,'')) LIKE ? OR "
                "LOWER(IFNULL(agent,'')) LIKE ? OR "
                "LOWER(IFNULL(model,'')) LIKE ?"
                ")"
            )
            args.extend([like] * 6)

        sql = f"SELECT * FROM jobs WHERE {' AND '.join(where)}"
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, args).fetchall()
        return [row_to_job(r) for r in rows]

    def all_ids(self) -> set[str]:
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute("SELECT job_id FROM jobs").fetchall()
        return {str(r["job_id"]) for r in rows}

    def reconcile(self, jobs_dir: Path) -> int:
        """Reload every job JSON into the index and drop rows whose file is gone.

        Teams upgrading already have months of ``job_*.json`` and no
        ``jobs.sqlite``. A later start must also refresh rows: a crash can
        leave the file newer than the index, and some old files omit
        ``job_id`` (the name is the id).
        """
        if not jobs_dir.is_dir():
            return 0
        try:
            paths = list(jobs_dir.glob("job_*.json"))
        except OSError as e:
            logger.warning(f"Job index glob failed: {e}")
            return 0
        kept: set[str] = set()
        unread: set[str] = set()
        upserted = 0
        for path in paths:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError) as e:
                logger.warning(f"Job index skip {path.name}: {e}")
                unread.add(path.stem)
                continue
            if not isinstance(raw, dict):
                unread.add(path.stem)
                continue
            job = dict(raw)
            jid = str(job.get("job_id") or "").strip() or path.stem
            if not jid.startswith("job_"):
                continue
            if not str(job.get("job_id") or "").strip():
                job["job_id"] = jid
            self.upsert(job)
            kept.add(jid)
            upserted += 1
        for jid in self.all_ids() - kept - unread:
            self.delete(jid)
        return upserted
