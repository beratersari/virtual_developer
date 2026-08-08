"""Persist OpenCode session ids keyed by git repository + work branch.

A later issue (or re-run) that uses the same remote and branch can resume
``opencode run --session`` / serve ``session_id`` instead of starting cold.
Operators reset a bind from the dashboard when they want a fresh session.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.logger import logger


def _default_binds_dir() -> Path:
    return Path.cwd() / ".jira-agent" / "opencode-binds"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_repo_key(url: str) -> str:
    """Identity key for a git remote (host/path, no scheme/.git/userinfo)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    raw = raw.strip("<>").strip("`").strip().rstrip("/")
    if raw.lower().endswith(".git"):
        raw = raw[:-4]
    if raw.startswith("git@"):
        # git@host:group/repo
        rest = raw[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            return f"{host.lower()}/{path.strip('/').lower()}"
        return rest.lower()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        host = (parsed.hostname or parsed.netloc.split("@")[-1]).lower()
        path = (parsed.path or "").strip("/").lower()
        if path.endswith(".git"):
            path = path[:-4]
        return f"{host}/{path}".rstrip("/")
    return raw.lower().replace("\\", "/")


def normalize_branch(name: str) -> str:
    branch = (name or "").strip().strip("`")
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    return branch


def bind_id_for(repository_url: str, branch: str) -> str:
    repo_key = normalize_repo_key(repository_url)
    br = normalize_branch(branch)
    digest = hashlib.sha256(f"{repo_key}\0{br}".encode("utf-8")).hexdigest()[:16]
    return f"osb_{digest}"


class SessionBindStore:
    """One JSON file per (repo, branch) → OpenCode session id."""

    def __init__(self, binds_dir: Optional[Path] = None) -> None:
        self.binds_dir = binds_dir or _default_binds_dir()
        self.binds_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, bind_id: str) -> Path:
        safe = (bind_id or "").replace("/", "_").replace("\\", "_")
        return self.binds_dir / f"{safe}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        path = self._path(rec["bind_id"])
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        tmp.replace(path)

    def get(
        self, repository_url: str, branch: str
    ) -> Optional[Dict[str, Any]]:
        bid = bind_id_for(repository_url, branch)
        if not normalize_repo_key(repository_url) or not normalize_branch(branch):
            return None
        return self.get_by_id(bid)

    def get_by_id(self, bind_id: str) -> Optional[Dict[str, Any]]:
        path = self._path((bind_id or "").strip())
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                rec = json.load(f)
            return rec if isinstance(rec, dict) else None
        except Exception as e:
            logger.debug(f"Could not read session bind {bind_id}: {e}")
            return None

    def upsert(
        self,
        *,
        repository_url: str,
        branch: str,
        session_id: str,
        issue_key: str = "",
        job_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(repository_url, str) or not isinstance(branch, str):
            return None
        if not isinstance(session_id, str):
            return None
        repo = repository_url.strip()
        br = normalize_branch(branch)
        sid = session_id.strip()
        if not normalize_repo_key(repo) or not br or not sid:
            return None
        bid = bind_id_for(repo, br)
        now = _now_iso()
        with self._lock:
            prev = self.get_by_id(bid) or {}
            rec: Dict[str, Any] = {
                "bind_id": bid,
                "repository_url": repo,
                "repository_key": normalize_repo_key(repo),
                "branch": br,
                "session_id": sid,
                "issue_key": (issue_key or "").strip().upper(),
                "job_id": job_id or prev.get("job_id"),
                "created_at": prev.get("created_at") or now,
                "updated_at": now,
            }
            self._write(rec)
        logger.info(
            f"OpenCode session bind {bid}: {normalize_repo_key(repo)}@{br} → {sid}"
        )
        return rec

    def delete(self, bind_id: str) -> bool:
        bid = (bind_id or "").strip()
        if not bid:
            return False
        path = self._path(bid)
        with self._lock:
            if not path.is_file():
                return False
            try:
                path.unlink()
            except OSError as e:
                logger.warning(f"Could not delete session bind {bid}: {e}")
                return False
        logger.info(f"OpenCode session bind reset: {bid}")
        return True

    def delete_for(self, repository_url: str, branch: str) -> bool:
        return self.delete(bind_id_for(repository_url, branch))

    def list_binds(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not self.binds_dir.is_dir():
            return items
        with self._lock:
            for path in self.binds_dir.glob("osb_*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("session_id"):
                    items.append(rec)
        items.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return items[: max(1, int(limit))]


session_bind_store = SessionBindStore()
