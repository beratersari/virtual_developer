"""Persist OpenCode session ids keyed by repository + work branch + target.

A later issue (or re-run) with the same remote, work/Source branch, **and**
Target can resume the same OpenCode serve session. A different Target is a
different MR base — new clone folder + new session so the model is not mixed
with work aimed at another branch. Dashboard Reset drops the bind.
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


def bind_id_for(
    repository_url: str, branch: str, target_branch: str = ""
) -> str:
    repo_key = normalize_repo_key(repository_url)
    br = normalize_branch(branch)
    tgt = normalize_branch(target_branch)
    digest = hashlib.sha256(
        f"{repo_key}\0{br}\0{tgt}".encode("utf-8")
    ).hexdigest()[:16]
    return f"osb_{digest}"


class SessionBindStore:
    """One JSON file per (repo, work branch, target) → OpenCode session id."""

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
        self,
        repository_url: str,
        branch: str,
        target_branch: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not normalize_repo_key(repository_url) or not normalize_branch(branch):
            return None
        if not normalize_branch(target_branch):
            return None
        bid = bind_id_for(repository_url, branch, target_branch)
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
        working_directory: Optional[str] = None,
        target_branch: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(repository_url, str) or not isinstance(branch, str):
            return None
        if not isinstance(session_id, str):
            return None
        if not isinstance(target_branch, str):
            return None
        repo = repository_url.strip()
        br = normalize_branch(branch)
        tgt = normalize_branch(target_branch)
        sid = session_id.strip()
        if not normalize_repo_key(repo) or not br or not tgt or not sid:
            return None
        bid = bind_id_for(repo, br, tgt)
        now = _now_iso()
        wd = (working_directory or "").strip() or None
        if wd:
            try:
                wd = str(Path(wd).resolve())
            except OSError:
                wd = str(wd)
        with self._lock:
            prev = self.get_by_id(bid) or {}
            forgotten = [
                str(x).strip()
                for x in (prev.get("forgotten_session_ids") or [])
                if str(x).strip()
            ]
            if sid in forgotten:
                logger.info(
                    f"OpenCode session bind {bid}: refusing forgotten session {sid}"
                )
                return prev or None
            rec: Dict[str, Any] = {
                "bind_id": bid,
                "repository_url": repo,
                "repository_key": normalize_repo_key(repo),
                "branch": br,
                "target_branch": tgt,
                "session_id": sid,
                "issue_key": (issue_key or "").strip().upper(),
                "job_id": job_id or prev.get("job_id"),
                "working_directory": wd or prev.get("working_directory"),
                "forgotten_session_ids": forgotten[-50:],
                "created_at": prev.get("created_at") or now,
                "updated_at": now,
            }
            if prev.get("reset_at"):
                rec["reset_at"] = prev.get("reset_at")
            self._write(rec)
        logger.info(
            f"OpenCode session bind {bid}: {normalize_repo_key(repo)}"
            f"@{br}→{tgt} → {sid}"
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

    def forget_session(
        self,
        bind_id: str,
        *,
        session_id: str = "",
        reason: str = "reset",
    ) -> Optional[Dict[str, Any]]:
        """Drop the resume pointer but remember the id so discovery cannot rebind it.

        Dashboard Reset and empty-timeout abandon use this instead of unlink so
        ``find_sessions_for_directory`` cannot restore the same ``ses_*``.
        """
        bid = (bind_id or "").strip()
        if not bid:
            return None
        with self._lock:
            rec = self.get_by_id(bid)
            if not rec:
                return None
            now = _now_iso()
            forgotten = [
                str(x).strip()
                for x in (rec.get("forgotten_session_ids") or [])
                if str(x).strip()
            ]
            sid = (session_id or rec.get("session_id") or "").strip()
            if sid and sid not in forgotten:
                forgotten.append(sid)
            rec["session_id"] = ""
            rec["forgotten_session_ids"] = forgotten[-50:]
            rec["reset_at"] = now
            rec["forget_reason"] = reason
            rec["updated_at"] = now
            self._write(rec)
        logger.info(
            f"OpenCode session bind forgotten {bid}: {sid or '(none)'} ({reason})"
        )
        return rec

    def delete_for(
        self, repository_url: str, branch: str, target_branch: str = ""
    ) -> bool:
        if not normalize_branch(target_branch):
            return False
        return self.delete(bind_id_for(repository_url, branch, target_branch))

    def forget_for(
        self,
        repository_url: str,
        branch: str,
        target_branch: str,
        *,
        session_id: str = "",
        reason: str = "abandoned",
    ) -> Optional[Dict[str, Any]]:
        if not normalize_branch(target_branch):
            return None
        return self.forget_session(
            bind_id_for(repository_url, branch, target_branch),
            session_id=session_id,
            reason=reason,
        )

    def find_by_issue_key(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Newest bind that still points at a session for this Jira issue."""
        key = (issue_key or "").strip().upper()
        if not key:
            return None
        best: Optional[Dict[str, Any]] = None
        for rec in self.list_binds(limit=500):
            if (rec.get("issue_key") or "").strip().upper() != key:
                continue
            if not str(rec.get("session_id") or "").strip():
                continue
            if best is None or (rec.get("updated_at") or "") >= (
                best.get("updated_at") or ""
            ):
                best = rec
        return best

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

    def relocate_working_directory(self, old_dir: Any, new_dir: Any) -> int:
        """Point binds at *new_dir* after a clone folder was renamed in place."""
        try:
            old_r = Path(old_dir).resolve()
            new_s = str(Path(new_dir).resolve())
        except (OSError, TypeError):
            return 0
        if not new_s:
            return 0
        try:
            if old_r == Path(new_s).resolve():
                return 0
        except OSError:
            pass
        updated = 0
        with self._lock:
            if not self.binds_dir.is_dir():
                return 0
            now = _now_iso()
            for path in self.binds_dir.glob("osb_*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                raw = rec.get("working_directory")
                if not raw or not isinstance(raw, str):
                    continue
                try:
                    if Path(raw).resolve() != old_r:
                        continue
                except OSError:
                    continue
                rec["working_directory"] = new_s
                rec["updated_at"] = now
                self._write(rec)
                updated += 1
        if updated:
            logger.info(
                f"Relocated {updated} session bind working_directory "
                f"{old_r} → {new_s}"
            )
        return updated

    def working_directories(self) -> List[Path]:
        """Clone paths still referenced by a session bind (protect from purge)."""
        out: List[Path] = []
        seen: set[str] = set()
        for rec in self.list_binds(limit=500):
            raw = rec.get("working_directory")
            if not raw or not isinstance(raw, str):
                continue
            try:
                resolved = Path(raw).resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out


session_bind_store = SessionBindStore()
