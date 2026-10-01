"""Job rows in yaver.sqlite (Analytics / Jobs list).

The document column is the full record, including deliveries. Indexed
columns serve list, count, and charts. Leftover job_*.json is imported
once on reconcile, then those files are deleted and not scanned again.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.logger import logger
from src.state.record_db import (
    connect,
    database_path,
    dumps,
    ensure_column,
    import_json_once,
    loads,
)

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
    completed_at TEXT NOT NULL DEFAULT '',
    working_directory TEXT NOT NULL DEFAULT '',
    document TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at);
CREATE INDEX IF NOT EXISTS idx_jobs_issue ON jobs(issue_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

_EXTRA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_jobs_repo ON jobs(repository_url);
CREATE INDEX IF NOT EXISTS idx_jobs_mr ON jobs(merge_request_url);
CREATE INDEX IF NOT EXISTS idx_jobs_workdir ON jobs(working_directory);
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

def _like_contains(text: str) -> str:
    """Literal substring for LIKE ... ESCAPE '\\'."""
    escaped = (
        text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


_STATUS_GROUPS = {
    "completed": ("completed", "plan_ready"),
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
    azure_pr_id, started_at, updated_at, completed_at,
    working_directory, document
) VALUES (
    :job_id, :issue_key, :summary, :description, :status, :workflow_type,
    :source, :agent, :model, :backend, :repository_url, :merge_request_url,
    :merge_request_state, :gitlab_project, :gitlab_mr_iid, :azure_project,
    :azure_pr_id, :started_at, :updated_at, :completed_at,
    :working_directory, :document
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
    completed_at=excluded.completed_at,
    working_directory=excluded.working_directory,
    document=excluded.document
"""


def default_index_path(jobs_dir: Path) -> Path:
    return database_path(jobs_dir)


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
        "working_directory": _text(job, "working_directory"),
        "document": dumps(job),
    }


def row_to_job(row: sqlite3.Row) -> Dict[str, Any]:
    keys = set(row.keys())
    if "document" in keys:
        doc = loads(row["document"])
        if doc is not None:
            out = dict(doc)
            if not str(out.get("job_id") or "").strip():
                out["job_id"] = row["job_id"]
            started_col = str(row["started_at"] or "").strip()
            if started_col and not str(out.get("started_at") or "").strip():
                out["started_at"] = started_col
            return out
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
    """WAL SQLite for job rows. Safe for one daemon per data dir."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    def _open(self) -> None:
        conn = connect(self.path)
        conn.executescript(_SCHEMA)
        ensure_column(conn, "jobs", "working_directory", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "jobs", "document", "TEXT NOT NULL DEFAULT ''")
        conn.executescript(_EXTRA_INDEXES)
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

    def delete(self, job_id: str) -> bool:
        jid = (job_id or "").strip()
        if not jid:
            return False
        with self._lock:
            assert self._conn is not None
            self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (jid,))
            changed = self._conn.execute("SELECT changes()").fetchone()
            self._conn.commit()
        return bool(changed and int(changed[0]) > 0)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        jid = (job_id or "").strip()
        if not jid:
            return None
        with self._lock:
            assert self._conn is not None
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (jid,)
            ).fetchone()
        return row_to_job(row) if row else None

    def list_records(
        self,
        *,
        issue_key: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        needle = (issue_key or "").strip().upper()
        off = max(0, int(offset or 0))
        lim = max(1, int(limit or 1))
        sql = (
            "SELECT * FROM jobs WHERE UPPER(issue_key) = ? "
            "ORDER BY started_at DESC, job_id DESC LIMIT ? OFFSET ?"
            if needle
            else "SELECT * FROM jobs ORDER BY started_at DESC, job_id DESC "
            "LIMIT ? OFFSET ?"
        )
        args: tuple[Any, ...] = (needle, lim, off) if needle else (lim, off)
        with self._lock:
            assert self._conn is not None
            rows = self._conn.execute(sql, args).fetchall()
        return [row_to_job(r) for r in rows]

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
                repo_bits.append(
                    "LOWER(IFNULL(repository_url,'')) LIKE ? ESCAPE '\\'"
                )
                args.append(_like_contains(text))
            where.append("(" + " OR ".join(repo_bits) + ")")

        want_keys = [p.strip().upper() for p in (issue_key or "").split(",") if p.strip()]
        if want_keys:
            where.append(_in_clause("UPPER(IFNULL(issue_key,''))", want_keys, args))

        search = (q or "").strip().lower()
        if search:
            like = _like_contains(search)
            where.append(
                "("
                "LOWER(IFNULL(issue_key,'')) LIKE ? ESCAPE '\\' OR "
                "LOWER(IFNULL(summary,'')) LIKE ? ESCAPE '\\' OR "
                "LOWER(IFNULL(description,'')) LIKE ? ESCAPE '\\' OR "
                "LOWER(IFNULL(repository_url,'')) LIKE ? ESCAPE '\\' OR "
                "LOWER(IFNULL(agent,'')) LIKE ? ESCAPE '\\' OR "
                "LOWER(IFNULL(model,'')) LIKE ? ESCAPE '\\'"
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
        """Import leftover ``job_*.json`` once. Later calls do not scan.

        A file that omits ``job_id`` takes the filename. A crash before the
        flag is set re-imports. Corrupt files stay on disk and do not force
        another scan. Rows already in the database are kept when their file
        is already gone.
        """
        with self._lock:
            assert self._conn is not None
            try:
                return import_json_once(
                    self._conn,
                    jobs_dir,
                    flag="jobs_json_imported",
                    pattern="job_*.json",
                    consume=self._consume_job_file,
                )
            except OSError as exc:
                logger.warning(f"Job import glob failed: {exc}")
                raise

    def _consume_job_file(self, path: Path) -> str:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            logger.warning(f"Job import skip {path.name}: {exc}")
            return "bad"
        if not isinstance(raw, dict):
            logger.warning(f"Job import skip {path.name}: not an object")
            return "bad"
        job = dict(raw)
        jid = str(job.get("job_id") or "").strip() or path.stem
        if not jid.startswith("job_"):
            return "skip"
        job["job_id"] = jid
        if not str(job.get("started_at") or "").strip():
            created = str(job.get("created_at") or "").strip()
            if created:
                job["started_at"] = created
        self.upsert(job)
        return "ok"
