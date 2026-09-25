"""Persist OpenCode session ids keyed by repository + work branch + target.

A later issue (or re-run) with the same remote, work/Source branch, **and**
Target can resume the same OpenCode serve session *of that kind* (plan,
build, test, or review). A different Target is a different MR base — new clone
folder + new session so the model is not mixed with work aimed at another
branch. Dashboard Reset drops the bind.
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
    from src.paths import agent_subdir, ensure_agent_data_dir

    ensure_agent_data_dir()
    return agent_subdir("opencode-binds")


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


SESSION_KIND_PLAN = "plan"
SESSION_KIND_BUILD = "build"
SESSION_KIND_TEST = "test"
SESSION_KIND_REVIEW = "review"
_SESSION_KINDS = frozenset(
    {
        SESSION_KIND_PLAN,
        SESSION_KIND_BUILD,
        SESSION_KIND_TEST,
        SESSION_KIND_REVIEW,
    }
)


def normalize_session_kind(kind: str = "") -> str:
    """``plan`` / ``build`` / ``test`` / ``review`` map, or empty for the legacy bind."""
    raw = (kind or "").strip().lower()
    if raw in {"planning", "derman-plan"}:
        return SESSION_KIND_PLAN
    if raw in {
        "execution",
        "executing",
        "derman-build",
        "gitlab_mr",
        "azure_pr",
        "gitlab",
        "azure",
    }:
        return SESSION_KIND_BUILD
    if raw in {"testing", "derman-test", "tester"}:
        return SESSION_KIND_TEST
    if raw in {
        "review",
        "reviewing",
        "code-reviewer",
        "derman-reviewer",
        "code_reviewer",
        "gitlab_review",
        "azure_review",
        "gitlab-review",
        "azure-review",
    }:
        return SESSION_KIND_REVIEW
    return raw if raw in _SESSION_KINDS else ""


def other_session_kinds(kind: str = "") -> tuple[str, ...]:
    """Every session map except ``kind`` (empty kind → all kinds)."""
    kind_n = normalize_session_kind(kind)
    return tuple(sorted(k for k in _SESSION_KINDS if k != kind_n))


def bind_compatible_with_kind(rec: Optional[Dict[str, Any]], kind: str) -> bool:
    """Whether ``rec`` may be resumed for this workflow kind.

    Plan and test never adopt another map. Build may adopt a legacy
    empty-kind bind from before the three maps existed.
    """
    if not rec:
        return False
    want = normalize_session_kind(kind)
    rec_kind = normalize_session_kind(str(rec.get("kind") or ""))
    if not want:
        return True
    if want == SESSION_KIND_BUILD:
        return rec_kind in {"", SESSION_KIND_BUILD}
    return rec_kind == want


def bind_id_for(
    repository_url: str,
    branch: str,
    target_branch: str = "",
    issue_key: str = "",
    kind: str = "",
    backend: str = "",
) -> str:
    repo_key = normalize_repo_key(repository_url)
    br = normalize_branch(branch)
    tgt = normalize_branch(target_branch)
    issue = (issue_key or "").strip().upper()
    kind_n = normalize_session_kind(kind)
    backend_n = _normalize_bind_backend(backend)
    # Kind-specific maps are (repo, source/work, target, kind) — no issue
    # in the key so a later same-kind job resumes that chat. Plan, build,
    # and test stay on three different sessions until Dashboard Reset.
    # Backend is part of the key so OpenCode, Codex, and Claude do not
    # replace each other's row. Empty backend keeps the pre-Claude id.
    if kind_n:
        material = f"{repo_key}\0{br}\0{tgt}\0{kind_n}"
    else:
        material = f"{repo_key}\0{br}\0{tgt}\0{issue}"
    if backend_n:
        material = f"{material}\0{backend_n}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"osb_{digest}"


def _normalize_bind_backend(backend: str) -> str:
    from src.backends.base import normalize_backend_name

    return normalize_backend_name(backend) or ""


def inferred_bind_backend(rec: Optional[Dict[str, Any]]) -> str:
    """Backend that owns this row. Untagged UUIDs stay Codex (pre-Claude)."""
    if not rec:
        return ""
    explicit = _normalize_bind_backend(str(rec.get("backend") or ""))
    if explicit:
        return explicit
    from src.backends.base import (
        BACKEND_CODEX,
        BACKEND_OPENCODE,
        is_codex_thread_id,
        is_opencode_session_id,
    )

    sid = str(rec.get("session_id") or "").strip()
    if is_opencode_session_id(sid):
        return BACKEND_OPENCODE
    if sid.startswith("thread_") or is_codex_thread_id(sid):
        return BACKEND_CODEX
    return ""


def workspace_id_for(
    repository_url: str,
    branch: str,
    target_branch: str = "",
) -> str:
    """Stable id for one repo + work/source + target (all kinds share it)."""
    repo_key = normalize_repo_key(repository_url)
    br = normalize_branch(branch)
    tgt = normalize_branch(target_branch)
    if not repo_key or not br or not tgt:
        return ""
    material = f"{repo_key}\0{br}\0{tgt}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"osw_{digest}"


_KIND_ORDER = {
    SESSION_KIND_PLAN: 0,
    SESSION_KIND_BUILD: 1,
    SESSION_KIND_TEST: 2,
    SESSION_KIND_REVIEW: 3,
    "": 4,
}


class SessionBindStore:
    """One JSON file per (repo, work branch, target) → OpenCode session id.

    ``opencode-binds.sqlite`` next to the folder is the Sessions-page index.
    JSON remains the full record; ``get_by_id`` always reads it.
    """

    def __init__(self, binds_dir: Optional[Path] = None) -> None:
        self.binds_dir = binds_dir or _default_binds_dir()
        self.binds_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._index = None
        self._index_ready = False
        self._index_stale = False
        try:
            from src.state.session_index import SessionBindIndex, default_index_path

            self._index = SessionBindIndex(default_index_path(self.binds_dir))
        except Exception as e:
            logger.warning(f"Session SQLite index unavailable: {e}")
            self._index = None

    def ensure_index(self) -> int:
        """Create/open opencode-binds.sqlite and insert JSON files not yet indexed."""
        if self._index is None:
            return 0
        with self._lock:
            if self._index_ready:
                return 0
            try:
                n = self._index.reconcile(self.binds_dir)
            except Exception as e:
                logger.warning(f"Session index backfill failed: {e}")
                self._index_stale = True
                return 0
            self._index_stale = False
            self._index_ready = True
            if n:
                logger.info(f"Session index backfilled {n} bind(s) from JSON")
            return n

    def _path(self, bind_id: str) -> Path:
        safe = (bind_id or "").replace("/", "_").replace("\\", "_")
        return self.binds_dir / f"{safe}.json"

    def _write(self, rec: Dict[str, Any]) -> None:
        path = self._path(rec["bind_id"])
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2, ensure_ascii=False)
        tmp.replace(path)
        if self._index is not None:
            try:
                self._index.upsert(rec)
            except Exception as e:
                self._index_stale = True
                logger.warning(
                    f"Session index upsert failed for {rec.get('bind_id')}: {e}"
                )

    def _index_ok(self) -> bool:
        return self._index is not None and not self._index_stale

    def get(
        self,
        repository_url: str,
        branch: str,
        target_branch: str = "",
        issue_key: str = "",
        kind: str = "",
        backend: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not normalize_repo_key(repository_url) or not normalize_branch(branch):
            return None
        if not normalize_branch(target_branch):
            return None
        kind_n = normalize_session_kind(kind)
        backend_n = _normalize_bind_backend(backend)
        if kind_n:
            # Plan, build, and test maps are separate. A miss must not
            # fall back to another kind (derman-plan cannot implement).
            hit = self.get_by_id(
                bind_id_for(
                    repository_url,
                    branch,
                    target_branch,
                    issue_key="",
                    kind=kind_n,
                    backend=backend_n,
                )
            )
            if hit or not backend_n:
                return hit
            # Rows saved before backend was part of the id.
            legacy = self.get_by_id(
                bind_id_for(
                    repository_url,
                    branch,
                    target_branch,
                    issue_key="",
                    kind=kind_n,
                )
            )
            if legacy and inferred_bind_backend(legacy) == backend_n:
                return legacy
            return None
        bid = bind_id_for(
            repository_url, branch, target_branch, issue_key=issue_key
        )
        hit = self.get_by_id(bid)
        if hit:
            # Exact key, including a forgotten row (empty session_id) so
            # attach can read forgotten_session_ids and refuse that ses_*.
            return hit
        if (issue_key or "").strip():
            legacy = self.get_by_id(
                bind_id_for(repository_url, branch, target_branch, issue_key="")
            )
            if legacy and str(legacy.get("session_id") or "").strip():
                return legacy
        # Newest live bind for this repo+work+target (any issue). Forgotten
        # rows have an empty session_id and are skipped by list_binds.
        return self._find_live_for(repository_url, branch, target_branch)

    def _find_live_for(
        self, repository_url: str, branch: str, target_branch: str
    ) -> Optional[Dict[str, Any]]:
        repo = normalize_repo_key(repository_url)
        br = normalize_branch(branch)
        tgt = normalize_branch(target_branch)
        if not repo or not br or not tgt:
            return None
        self.ensure_index()
        if self._index_ok():
            try:
                return self._index.newest_live(repo, br, tgt)
            except Exception as e:
                logger.warning(f"Session index live lookup failed: {e}")
        best: Optional[Dict[str, Any]] = None
        for rec in self._list_binds_from_files(limit=None):
            rec_repo = rec.get("repository_key") or normalize_repo_key(
                str(rec.get("repository_url") or "")
            )
            if rec_repo != repo:
                continue
            if normalize_branch(str(rec.get("branch") or "")) != br:
                continue
            if normalize_branch(str(rec.get("target_branch") or "")) != tgt:
                continue
            if best is None or (rec.get("updated_at") or "") >= (
                best.get("updated_at") or ""
            ):
                best = rec
        return best

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
        kind: str = "",
        backend: str = "",
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
        kind_n = normalize_session_kind(kind)
        backend_n = _normalize_bind_backend(backend)
        if not normalize_repo_key(repo) or not br or not tgt or not sid:
            return None
        bid = bind_id_for(
            repo,
            br,
            tgt,
            issue_key="" if kind_n else issue_key,
            kind=kind_n,
            backend=backend_n,
        )
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
                "kind": kind_n or prev.get("kind") or "",
                "backend": backend_n or inferred_bind_backend(prev) or "",
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
        kind_note = f" kind={kind_n}" if kind_n else ""
        logger.info(
            f"Session bind {bid}: {normalize_repo_key(repo)}"
            f"@{br}→{tgt}{kind_note} → {sid}"
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
            if self._index is not None:
                try:
                    self._index.delete(bid)
                except Exception as e:
                    self._index_stale = True
                    logger.warning(f"Session index delete failed for {bid}: {e}")
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
        self,
        repository_url: str,
        branch: str,
        target_branch: str = "",
        issue_key: str = "",
        kind: str = "",
    ) -> bool:
        if not normalize_branch(target_branch):
            return False
        kind_n = normalize_session_kind(kind)
        ok = self.delete(
            bind_id_for(
                repository_url,
                branch,
                target_branch,
                issue_key="" if kind_n else issue_key,
                kind=kind_n,
            )
        )
        if kind_n:
            return ok
        # Leftover pre-issue-key file must not keep a live pointer.
        if (issue_key or "").strip():
            ok = (
                self.delete(bind_id_for(repository_url, branch, target_branch))
                or ok
            )
        return ok

    def forget_for(
        self,
        repository_url: str,
        branch: str,
        target_branch: str,
        *,
        session_id: str = "",
        reason: str = "abandoned",
        issue_key: str = "",
        kind: str = "",
        backend: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not normalize_branch(target_branch):
            return None
        kind_n = normalize_session_kind(kind)
        backend_n = _normalize_bind_backend(backend)
        rec = self.forget_session(
            bind_id_for(
                repository_url,
                branch,
                target_branch,
                issue_key="" if kind_n else issue_key,
                kind=kind_n,
                backend=backend_n,
            ),
            session_id=session_id,
            reason=reason,
        )
        if kind_n:
            return rec
        # Production upserts include issue_key. Also tombstone the legacy
        # "" bind so get() fallback cannot restore the abandoned ses_*.
        if (issue_key or "").strip():
            leftover = self.forget_session(
                bind_id_for(repository_url, branch, target_branch),
                session_id=session_id,
                reason=reason,
            )
            rec = rec or leftover
        return rec

    def forgotten_ids_for(
        self,
        repository_url: str,
        branch: str,
        target_branch: str,
        issue_key: str = "",
        kind: str = "",
    ) -> List[str]:
        """Forgotten ses_* for this repo+work+target (any issue, including empty)."""
        out: List[str] = []
        seen: set[str] = set()

        def _add(rec: Optional[Dict[str, Any]]) -> None:
            if not rec:
                return
            for x in rec.get("forgotten_session_ids") or []:
                fx = str(x or "").strip()
                if fx and fx not in seen:
                    seen.add(fx)
                    out.append(fx)

        repo = normalize_repo_key(repository_url)
        br = normalize_branch(branch)
        tgt = normalize_branch(target_branch)
        if not repo or not br or not tgt:
            return out
        kind_n = normalize_session_kind(kind)
        if kind_n:
            _add(
                self.get_by_id(
                    bind_id_for(
                        repository_url,
                        branch,
                        target_branch,
                        issue_key="",
                        kind=kind_n,
                    )
                )
            )
        _add(
            self.get_by_id(
                bind_id_for(
                    repository_url, branch, target_branch, issue_key=issue_key
                )
            )
        )
        _add(self.get_by_id(bind_id_for(repository_url, branch, target_branch)))
        self.ensure_index()
        if self._index_ok():
            try:
                for rec in self._index.rows_for_checkout(repo, br, tgt):
                    _add(rec)
                return out
            except Exception as e:
                logger.warning(f"Session index forgotten lookup failed: {e}")
        if not self.binds_dir.is_dir():
            return out
        with self._lock:
            for path in self.binds_dir.glob("osb_*.json"):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        rec = json.load(f)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                rec_repo = rec.get("repository_key") or normalize_repo_key(
                    str(rec.get("repository_url") or "")
                )
                if rec_repo != repo:
                    continue
                if normalize_branch(str(rec.get("branch") or "")) != br:
                    continue
                if normalize_branch(str(rec.get("target_branch") or "")) != tgt:
                    continue
                _add(rec)
        return out

    def find_by_issue_key(self, issue_key: str) -> Optional[Dict[str, Any]]:
        """Newest bind that still points at a session for this Jira issue."""
        key = (issue_key or "").strip().upper()
        if not key:
            return None
        self.ensure_index()
        if self._index_ok():
            try:
                return self._index.newest_live_for_issue(key)
            except Exception as e:
                logger.warning(f"Session index issue lookup failed: {e}")
        best: Optional[Dict[str, Any]] = None
        for rec in self._list_binds_from_files(limit=None):
            if (rec.get("issue_key") or "").strip().upper() != key:
                continue
            if not str(rec.get("session_id") or "").strip():
                continue
            if best is None or (rec.get("updated_at") or "") >= (
                best.get("updated_at") or ""
            ):
                best = rec
        return best

    def list_binds(self, *, limit: Optional[int] = 200) -> List[Dict[str, Any]]:
        self.ensure_index()
        if self._index_ok():
            try:
                return self._index.list_live(limit=limit)
            except Exception as e:
                logger.warning(f"Session index list failed: {e}")
        return self._list_binds_from_files(limit=limit)

    def _list_binds_from_files(
        self, *, limit: Optional[int] = 200
    ) -> List[Dict[str, Any]]:
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
        if limit is None:
            return items
        return items[: max(1, int(limit))]

    def list_workspaces(
        self, *, limit: Optional[int] = 200
    ) -> List[Dict[str, Any]]:
        """One row per repo + work/source + target, with kind binds rolled up."""
        buckets: Dict[str, Dict[str, Any]] = {}
        for rec in self.list_binds(limit=None):
            wid = workspace_id_for(
                str(rec.get("repository_url") or rec.get("repository_key") or ""),
                str(rec.get("branch") or ""),
                str(rec.get("target_branch") or ""),
            )
            if not wid:
                continue
            kind = str(rec.get("kind") or "").strip() or "legacy"
            bucket = buckets.get(wid)
            if bucket is None:
                buckets[wid] = {
                    "workspace_id": wid,
                    "repository_url": rec.get("repository_url") or "",
                    "repository_key": rec.get("repository_key")
                    or normalize_repo_key(str(rec.get("repository_url") or "")),
                    "branch": rec.get("branch") or "",
                    "target_branch": rec.get("target_branch") or "",
                    "working_directory": rec.get("working_directory"),
                    "issue_key": rec.get("issue_key") or "",
                    "updated_at": rec.get("updated_at") or "",
                    "kinds": [kind],
                    "session_count": 1,
                }
                continue
            bucket["session_count"] = int(bucket.get("session_count") or 0) + 1
            kinds = list(bucket.get("kinds") or [])
            if kind not in kinds:
                kinds.append(kind)
                kinds.sort(key=lambda k: _KIND_ORDER.get(k, 9))
                bucket["kinds"] = kinds
            if (rec.get("updated_at") or "") >= (bucket.get("updated_at") or ""):
                bucket["updated_at"] = rec.get("updated_at") or ""
                if rec.get("issue_key"):
                    bucket["issue_key"] = rec.get("issue_key")
                if rec.get("working_directory"):
                    bucket["working_directory"] = rec.get("working_directory")
                if rec.get("repository_url"):
                    bucket["repository_url"] = rec.get("repository_url")
        rows = list(buckets.values())
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        if limit is None:
            return rows
        return rows[: max(1, int(limit))]

    def binds_for_workspace(self, workspace_id: str) -> List[Dict[str, Any]]:
        """Kind binds (plan/build/test/review) for one workspace id."""
        want = (workspace_id or "").strip()
        if not want:
            return []
        out: List[Dict[str, Any]] = []
        for rec in self.list_binds(limit=None):
            wid = workspace_id_for(
                str(rec.get("repository_url") or rec.get("repository_key") or ""),
                str(rec.get("branch") or ""),
                str(rec.get("target_branch") or ""),
            )
            if wid == want:
                out.append(rec)
        out.sort(
            key=lambda r: (
                _KIND_ORDER.get(str(r.get("kind") or ""), 9),
                r.get("updated_at") or "",
            )
        )
        return out

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
        self.ensure_index()
        rows: Optional[List[Dict[str, Any]]] = None
        if self._index_ok():
            try:
                rows = self._index.rows_with_directory()
            except Exception as e:
                logger.warning(f"Session index directory scan failed: {e}")
                rows = None
        with self._lock:
            if rows is None:
                if not self.binds_dir.is_dir():
                    return 0
                rows = []
                for path in self.binds_dir.glob("osb_*.json"):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            loaded = json.load(f)
                    except Exception:
                        continue
                    if isinstance(loaded, dict):
                        rows.append(loaded)
            now = _now_iso()
            for rec in rows:
                raw = rec.get("working_directory")
                if not raw or not isinstance(raw, str):
                    continue
                try:
                    if Path(raw).resolve() != old_r:
                        continue
                except OSError:
                    continue
                disk = self.get_by_id(str(rec.get("bind_id") or "")) or rec
                disk["working_directory"] = new_s
                disk["updated_at"] = now
                self._write(disk)
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
        for rec in self.list_binds(limit=None):
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
