"""Git manager for JIRA Virtual Developer.

Handles branch creation, commits, and git operations within temp working directories.
Clones are keyed by repository + work branch + target so later jobs with the
same Source **and** Target reuse the folder and continue the OpenCode session
(see ``src.issue_git_spec``). A different Target gets a new clone.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote, urlparse, urlunparse

from src.config import settings, set_current_temp_dir
from src.logger import logger
from src.process_kill import kill_process_tree


class GitCloneError(RuntimeError):
    """Clone failed — ``user_message`` is safe for a Jira comment."""

    def __init__(self, user_message: str, *, technical: str = ""):
        self.user_message = user_message
        self.technical = technical
        super().__init__(user_message)


class GitSourceBranchError(RuntimeError):
    """Work/source branch cannot be prepared — safe for Jira."""

    def __init__(self, user_message: str, *, technical: str = ""):
        self.user_message = user_message
        self.technical = technical
        super().__init__(user_message)


class GitTargetBranchError(RuntimeError):
    """Target (MR destination / base) missing on remote — safe for Jira."""

    def __init__(self, user_message: str, *, technical: str = ""):
        self.user_message = user_message
        self.technical = technical
        super().__init__(user_message)


class GitCancelledError(RuntimeError):
    """Job was cancelled; git/glab subprocesses were force-killed."""


def summarize_git_error(exc: object, *, limit: int = 800) -> str:
    """Operator-facing git stderr: prefer fatal/error/remote lines."""
    text = str(exc or "").replace("\r", "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ""
    preferred = [
        ln
        for ln in lines
        if ln.lower().startswith(("fatal:", "error:", "remote:", "hint:"))
    ]
    if preferred:
        body = "\n".join(preferred)
    elif lines[0].startswith("Git command failed:") and len(lines) > 1:
        body = "\n".join(lines[1:])
    else:
        body = "\n".join(lines[-4:])
    return body[:limit].strip()


# Primary integration bases — never used as the agent work branch
_PRIMARY_BASES = frozenset({"main", "master", "develop", "trunk", "dev"})


class GitManager:
    """Manages git operations in isolated temp directories per JIRA issue.

    Flow (GitLab MR: source → target):
    1. Repository URL + source/target from the Jira ``{params}`` block
    2. Temp folder ``{remote12}_{digest12}`` (short for Windows MAX_PATH;
       digest is sha256(repo+work+target))
    3. Clone remote repo
    4. **Require** ``origin/{target}`` exists
    5. Resolve work branch: params **Source** (unless Source is a primary base
       / equals target → then ``feature/{KEY}``)
    6. If ``origin/{source}`` exists → checkout that tip; else create work
       branch **from** ``origin/{target}``
    7. Agent works on that branch; push + MR **source → target**
       (commit subjects always use the Jira issue key, not the branch name)
    """

    # Live instances keyed by issue — cancel can find clone-in-flight
    # GitManagers before the processor registers ``_contexts``.
    _live_lock = threading.Lock()
    _live_by_issue: Dict[str, "GitManager"] = {}

    def __init__(
        self,
        issue_key: Optional[str] = None,
        *,
        remote_url: Optional[str] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        keep_source_work_branch: bool = False,
    ):
        self.issue_key = issue_key
        self.temp_dir: Optional[Path] = None
        self.remote_enabled: bool = False
        self.remote_url: Optional[str] = (remote_url or "").strip() or None
        self.remote_name: str = "unknown"
        # Params "source" = intended MR source / work branch name
        self.source_branch: str = (source_branch or "").strip()
        # Params "target" = MR destination; work branches are created from it
        self.target_branch: str = (target_branch or source_branch or "").strip()
        # GitLab MR comments must stay on the MR source even when it is develop/main
        self.keep_source_work_branch: bool = bool(keep_source_work_branch)
        # Actual branch checked out for agent work (set by ensure_feature_branch)
        self.work_branch: Optional[str] = None
        # Last failed push reason (operator-facing; cleared on success)
        self.last_push_error: Optional[str] = None
        self._init_proc_state()

        logger.info(f"Initializing GitManager for issue: {issue_key}")
        logger.debug(
            f"remote_url={'set' if self.remote_url else 'missing'} "
            f"source_branch={self.source_branch or '(missing)'} "
            f"target_branch={self.target_branch or '(missing)'}"
        )

        if issue_key:
            self._setup_temp_working_dir()

        if self.temp_dir:
            set_current_temp_dir(self.temp_dir)
            logger.info(f"Temp directory set: {self.temp_dir}")

    def _setup_temp_working_dir(self) -> None:
        """Setup isolated temp clone for this JIRA issue (always required)."""
        self._register_live()
        try:
            self._setup_temp_working_dir_inner()
        except GitCancelledError:
            # Clone thread also drops an incomplete tree so a race with
            # cancel_job's live_for() cannot leave a half-downloaded repo.
            if self.should_discard_on_cancel():
                self.discard_workspace()
            else:
                self._unregister_live()
            raise
        except Exception:
            self._unregister_live()
            raise

    def _setup_temp_working_dir_inner(self) -> None:
        logger.info(f"Setting up temp working directory for issue: {self.issue_key}")

        gitlab_url = (self.remote_url or "").strip()
        if not gitlab_url:
            raise GitCloneError(
                "*Yaver* could not clone: no repository URL was provided on the issue.\n\n"
                "Add `Repository: https://gitlab.example.com/group/repo.git` to the description."
            )
        if not self.target_branch:
            raise GitTargetBranchError(
                "*Yaver* could not prepare the workspace: no target branch on the issue.\n\n"
                "Add `Target branch: develop` (the branch that must exist and receive the MR) "
                "to the `{params}` block."
            )
        if not self.source_branch:
            # Default work-branch name intent to feature/KEY later
            self.source_branch = self.target_branch

        self.remote_url = gitlab_url
        self.remote_name = self._extract_remote_name(gitlab_url)
        self.remote_enabled = True
        logger.info(
            f"Remote configured - name: {self.remote_name}, "
            f"source_branch: {self.source_branch}, "
            f"target_branch: {self.target_branch} "
            f"(MR will be source → target; work branch created from target)"
        )

        # Resolve work branch before naming the folder so the same
        # repo + Source (+ isolated feature/KEY) always land in one clone.
        self.work_branch = self._resolve_work_branch_name(self.issue_key)
        self.temp_dir = self._create_temp_directory()
        logger.info(f"Temp working directory: {self.temp_dir}")

        if self._existing_clone_usable():
            logger.info(
                f"Reusing existing clone for {self.issue_key} "
                f"({self.remote_name} @ {self.work_branch})"
            )
            self._refresh_existing_clone()
        else:
            self._reset_temp_dir_for_clone()
            logger.info("Starting repository clone...")
            self._clone_into_temp()
        logger.info(f"Setup complete for issue: {self.issue_key}")
    def _extract_remote_name(self, url: str) -> str:
        """Extract a short name from remote URL."""
        url = url.rstrip('/').replace('.git', '')
        parts = url.split('/')
        remote_name = parts[-1] if parts else "unknown"
        logger.debug(f"Extracted remote name '{remote_name}' from URL: {url}")
        return remote_name

    @staticmethod
    def _safe_fs_token(token: str, *, max_len: int = 80) -> str:
        cleaned = "".join(
            c if c.isalnum() or c in "._-" else "_" for c in (token or "unknown")
        )
        cleaned = cleaned.strip("._-") or "unknown"
        return cleaned[:max_len]

    def _workspace_identity(self) -> Dict[str, str]:
        """Stable repo + work + target identity used for folder naming."""
        from src.state.session_bind_store import normalize_branch, normalize_repo_key

        work = (
            (self.work_branch or "").strip()
            or self._resolve_work_branch_name(self.issue_key)
        )
        repo_key = normalize_repo_key(self.remote_url or "")
        branch = normalize_branch(work)
        target = normalize_branch(self.target_branch or "")
        issue = (self.issue_key or "").strip().upper()
        digest = hashlib.sha256(
            f"{repo_key}\0{branch}\0{target}\0{issue}".encode("utf-8")
        ).hexdigest()[:12]
        return {
            "repo_key": repo_key,
            "work": branch,
            "target": target,
            "digest": digest,
        }

    def _workspace_folder_name(self) -> str:
        """Short stable folder: ``{remote12}_{digest12}``.

        Branch names are *not* in the path. Uniqueness is the hash of
        repo+work+target. Keeping the folder short leaves budget for nested
        Windows build trees (``build/proj/src/Debug/...``) under MAX_PATH.
        """
        ident = self._workspace_identity()
        remote = self._safe_fs_token(self.remote_name or "repo", max_len=12)
        return f"{remote}_{ident['digest']}"

    def _legacy_workspace_folder_name(self) -> str:
        """Pre-shortening folder name (repo_work_target_digest).

        Kept so an upgrade can rename the leftover long folder onto the
        short path and relocate bind + OpenCode ``session.directory``.
        """
        ident = self._workspace_identity()
        remote = self._safe_fs_token(self.remote_name or "repo", max_len=32)
        branch_tok = self._safe_fs_token(
            ident["work"].replace("/", "-"), max_len=40
        )
        target_tok = self._safe_fs_token(
            ident["target"].replace("/", "-"), max_len=24
        )
        return f"{remote}_{branch_tok}_{target_tok}_{ident['digest']}"

    def _safe_under_temp_base(self, base_temp: Path, folder_name: str) -> Path:
        temp_path = base_temp / folder_name
        try:
            temp_path.resolve().relative_to(base_temp)
        except ValueError as e:
            raise RuntimeError(f"Unsafe temp path rejected: {temp_path}") from e
        return temp_path

    def _create_temp_directory(self) -> Path:
        """Return the stable temp clone dir for this repo + work + target.

        Reuses the folder when it already exists so OpenCode serve can
        continue the same session. Isolated ``feature/{KEY}`` work branches still
        get their own directory. Same Source with a **different Target** gets a
        new folder (different MR base / branch point).

        Always uses the short ``{remote12}_{digest12}`` name. A leftover
        long legacy folder is renamed onto that short path (never kept as
        the working dir — Windows MAX_PATH). Bind ``working_directory`` and
        OpenCode ``session.directory`` are rewritten so serve resume still
        matches the live clone.
        """
        base_temp = (Path.cwd() / settings.temp_dir_base).resolve()
        base_temp.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Base temp directory: {base_temp}")

        short_path = self._safe_under_temp_base(
            base_temp, self._workspace_folder_name()
        )
        if short_path.exists():
            logger.info(f"Reusing temp directory: {short_path}")
            return short_path

        legacy_path = self._safe_under_temp_base(
            base_temp, self._legacy_workspace_folder_name()
        )
        if legacy_path.exists():
            try:
                old_resolved = legacy_path.resolve()
                os.replace(str(legacy_path), str(short_path))
                logger.info(
                    f"Renamed legacy temp dir {legacy_path.name} → {short_path.name}"
                )
                self._relocate_workspace_after_rename(
                    old_resolved, short_path.resolve()
                )
                return short_path
            except OSError as e:
                logger.warning(
                    f"Could not rename legacy temp dir {legacy_path.name} "
                    f"to {short_path.name}: {e}; creating short path"
                )

        short_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created temp directory: {short_path}")
        return short_path

    def _relocate_workspace_after_rename(self, old_dir: Path, new_dir: Path) -> None:
        """Keep session binds + OpenCode DB in sync after an in-place rename."""
        try:
            from src.state.session_bind_store import session_bind_store

            session_bind_store.relocate_working_directory(old_dir, new_dir)
        except Exception as e:
            logger.warning(
                f"Could not relocate session binds after rename "
                f"{old_dir} → {new_dir}: {e}"
            )
        try:
            from src.opencode_sessions import relocate_session_directories

            relocate_session_directories(old_dir, new_dir)
        except Exception as e:
            logger.warning(
                f"Could not relocate OpenCode session dirs after rename "
                f"{old_dir} → {new_dir}: {e}"
            )

    def _enable_git_longpaths(self) -> None:
        """Persist ``core.longpaths`` so Windows git/MSBuild trees can exceed 260.

        Harmless on Linux/macOS. Best-effort — never fail clone/setup.
        """
        if not self.temp_dir or not (self.temp_dir / ".git").exists():
            return
        try:
            self._run_git(["config", "core.longpaths", "true"], check=False)
        except Exception as e:
            logger.debug(f"Could not set core.longpaths in {self.temp_dir}: {e}")

    def _existing_clone_usable(self) -> bool:
        """True when temp_dir is a git checkout of this remote."""
        if not self.temp_dir or not self.temp_dir.is_dir():
            return False
        git_dir = self.temp_dir / ".git"
        if not git_dir.exists():
            return False
        try:
            result = self._run_tracked(
                ["git", "remote", "get-url", "origin"],
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._git_command_timeout(),
            )
        except GitCancelledError:
            raise
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        from src.state.session_bind_store import normalize_repo_key

        origin = (result.stdout or "").strip()
        return bool(origin) and normalize_repo_key(origin) == normalize_repo_key(
            self.remote_url or ""
        )

    def _reset_temp_dir_for_clone(self) -> None:
        """Make temp_dir an empty folder ready for ``git clone``."""
        if not self.temp_dir:
            return
        if self.temp_dir.exists():
            try:
                nonempty = any(self.temp_dir.iterdir())
            except OSError:
                nonempty = True
            if nonempty:
                shutil.rmtree(self.temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    def _refresh_existing_clone(self) -> None:
        """Fetch remotes in a reused clone (work branch checkout happens later)."""
        if not self.temp_dir:
            raise RuntimeError("Temp directory not initialized")
        self._assert_remote_host_allowed(self.remote_url or "")
        issue_tag = self.issue_key or "(unknown)"
        logger.info(f"Refreshing reused clone for {issue_tag}: {self.temp_dir}")
        self._with_auth_remote()
        try:
            self._run_git(["fetch", "origin", "--prune"], check=False, auth=True)
        finally:
            self._scrub_remote_credentials()
        self._enable_git_longpaths()
        self._update_submodules(reason="after reuse fetch")
        self._materialize_job_remote_refs()

    @staticmethod
    def _base_git_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Process env for all git child processes.

        ``GIT_LFS_SKIP_SMUDGE=1`` keeps LFS pointer files as pointers. Unattended
        agent clones must not fail when a broken/old git-lfs filter prints
        ``git version >= 1.8.2 is required… your version:`` (empty) after a
        successful ``checkout`` — that was aborting otherwise-valid GitLab MR jobs.
        """
        env = dict(os.environ)
        env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        if extra:
            env.update(extra)
        return env

    @staticmethod
    def _looks_like_lfs_filter_noise(text: str) -> bool:
        low = (text or "").lower()
        if "git lfs" not in low and "git-lfs" not in low and "lfs" not in low:
            return False
        return (
            "git version" in low
            or "required for git lfs" in low
            or "error: failed to filter" in low
            or "smudge filter" in low
            or "filter-process" in low
        )

    def _apply_pat_to_git_env(
        self, env: Optional[Dict[str, str]] = None, *, url: str = ""
    ) -> Dict[str, str]:
        """Unattended git env: settings/``.env`` PAT, no credential-manager prompt.

        Rewrites ``https://host/`` → ``https://oauth2:PAT@host/`` via
        ``GIT_CONFIG_*`` (PAT is not on the git argv). Disables
        ``credential.helper`` for this child so a missing/disabled Windows
        GCM cannot pop a username prompt. Askpass is a backup.
        """
        out = dict(env) if env is not None else self._base_git_env()
        out["GIT_TERMINAL_PROMPT"] = "0"
        target = url or self.remote_url or ""
        pat = self._pat_for_remote(target)
        host = self._host_from_url(target)
        if not pat or not host:
            return out
        askpass = self._ensure_askpass_script()
        out["GIT_ASKPASS"] = str(askpass)
        out["VD_GIT_PASSWORD"] = pat
        pairs = [
            (f"url.https://oauth2:{pat}@{host}/.insteadOf", f"https://{host}/"),
            (f"url.http://oauth2:{pat}@{host}/.insteadOf", f"http://{host}/"),
            ("credential.helper", ""),
        ]
        try:
            base_count = int(out.get("GIT_CONFIG_COUNT") or "0")
        except ValueError:
            base_count = 0
        for i, (key, value) in enumerate(pairs):
            idx = base_count + i
            out[f"GIT_CONFIG_KEY_{idx}"] = key
            out[f"GIT_CONFIG_VALUE_{idx}"] = value
        out["GIT_CONFIG_COUNT"] = str(base_count + len(pairs))
        return out

    def _submodule_auth_env(self) -> Dict[str, str]:
        """Env that rewrites https://host/ → oauth2:PAT@host for nested submodule clones.

        Parent ``origin`` auth alone does not cover absolute submodule URLs on
        the same GitLab host. ``url.*.insteadOf`` via ``GIT_CONFIG_*`` avoids
        writing the PAT into the repo config file.
        """
        return self._apply_pat_to_git_env(self._base_git_env())

    def _update_submodules(self, *, reason: str = "") -> None:
        """Init and update submodules recursively after clone / branch checkout.

        No-op when ``settings.git_update_submodules`` is false or when the repo
        has no ``.gitmodules``. Hard-fails with ``GitCloneError`` on timeout or
        non-zero exit so agents never run on an incomplete tree.

        Logs only start/end (no live git progress stream).
        """
        if not getattr(settings, "git_update_submodules", True):
            logger.info(
                "Submodule update skipped "
                f"(git_update_submodules=false){f'; {reason}' if reason else ''}"
            )
            return
        if not self.temp_dir or not self.temp_dir.is_dir():
            raise RuntimeError("Temp directory not initialized for submodule update")

        gitmodules = self.temp_dir / ".gitmodules"
        if not gitmodules.is_file():
            logger.info(
                "Submodule update: no .gitmodules found "
                f"(nothing to init){f'; {reason}' if reason else ''}"
            )
            return

        reason_suffix = f" ({reason})" if reason else ""
        timeout = max(
            60,
            int(getattr(settings, "git_submodule_timeout_seconds", 1800) or 1800),
        )
        issue_tag = self.issue_key or "(unknown)"
        logger.info(
            f"Submodule update started for {issue_tag}{reason_suffix} "
            f"(timeout={timeout}s)"
        )
        started = time.monotonic()

        applied = self._apply_settings_pat_to_origin()
        env = self._submodule_auth_env()
        try:
            result = self._run_tracked(
                [
                    "git",
                    "submodule",
                    "update",
                    "--init",
                    "--recursive",
                ],
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except GitCancelledError:
            raise
        except subprocess.TimeoutExpired as e:
            logger.error(
                f"Submodule update timed out for {issue_tag} after {timeout}s"
            )
            raise GitCloneError(
                (
                    f"*Yaver* could not **update git submodules** "
                    f"(timed out after {timeout}s).\n\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`\n"
                    f"*When:* {reason or 'workspace setup'}\n\n"
                    "Raise `GIT_SUBMODULE_TIMEOUT_SECONDS` if the tree is very "
                    "large, check network access to nested repos, then move the "
                    "issue back to *To Do*."
                ),
                technical=str(e),
            ) from e
        finally:
            if applied:
                try:
                    self._scrub_remote_credentials()
                except Exception:
                    pass

        elapsed = time.monotonic() - started
        if result.returncode != 0:
            safe_err = self._redact_secret_text(
                (result.stderr or result.stdout or "")[-2000:]
                or "git submodule update failed"
            )
            logger.error(
                f"Submodule update failed for {issue_tag} after {elapsed:.1f}s: "
                f"{safe_err.strip()[:500]}"
            )
            raise GitCloneError(
                (
                    f"*Yaver* could not **update git submodules**.\n\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`\n"
                    f"*When:* {reason or 'workspace setup'}\n"
                    f"*Detail:* {safe_err.strip()[:800]}\n\n"
                    "Check nested repository URLs, that the PAT can access "
                    "submodule hosts, and network reachability. Then move the "
                    "issue back to *To Do*."
                ),
                technical=safe_err,
            )

        logger.info(
            f"Submodule update finished for {issue_tag}{reason_suffix} "
            f"in {elapsed:.1f}s"
        )

    def _clone_into_temp(self) -> None:
        """Clone the remote repository into the temp directory."""
        if not self.temp_dir:
            raise RuntimeError("Temp directory not initialized")

        # Fail closed: never inject PAT for untrusted/unknown hosts
        self._assert_remote_host_allowed(self.remote_url or "")

        raw_pat = self._pat_for_remote(self.remote_url or "")
        # Coerce so mock/non-str never break redact/replace
        gitlab_pat = raw_pat if isinstance(raw_pat, str) and raw_pat.strip() else ""
        clean_url = (self.remote_url or "").strip()
        # Clean URL on argv (no PAT in ps). Auth is insteadOf + askpass in env
        # from Settings / .env so a disabled credential manager cannot prompt.
        clone_url = clean_url
        clone_env = self._apply_pat_to_git_env(self._base_git_env(), url=clean_url)

        issue_tag = self.issue_key or "(unknown)"
        clone_timeout = max(
            60,
            int(getattr(settings, "git_clone_timeout_seconds", 1800) or 1800),
        )
        logger.info(
            f"Clone started for {issue_tag}: {self.remote_url} "
            f"→ {self.temp_dir} (timeout={clone_timeout}s)"
        )
        logger.debug(f"Clone will use settings PAT in URL: {bool(gitlab_pat)}")
        started = time.monotonic()

        self._clone_in_progress = True
        try:
            result = self._run_tracked(
                [
                    "git",
                    "-c",
                    "core.longpaths=true",
                    "clone",
                    "--no-single-branch",
                    clone_url,
                    str(self.temp_dir),
                ],
                capture_output=True,
                text=True,
                timeout=clone_timeout,
                env=clone_env,
            )
        except GitCancelledError:
            raise
        except subprocess.TimeoutExpired as e:
            logger.error(
                f"Clone timed out for {issue_tag} after {clone_timeout}s"
            )
            raise GitCloneError(
                (
                    f"*Yaver* could not **clone** the repository "
                    f"(timed out after {clone_timeout}s).\n\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`\n\n"
                    "Raise `GIT_CLONE_TIMEOUT_SECONDS` for large repos, check "
                    "network access to GitLab, then move the issue back to *To Do*."
                ),
                technical=str(e),
            ) from e

        elapsed = time.monotonic() - started
        if result.returncode != 0:
            # Never surface PAT-bearing URLs from git stderr to callers/logs
            safe_err = (result.stderr or result.stdout or "")
            if gitlab_pat:
                safe_err = safe_err.replace(gitlab_pat, "***")
            safe_err = self._redact_secret_text(safe_err)
            logger.error(
                f"Clone failed for {issue_tag} after {elapsed:.1f}s: "
                f"{safe_err.strip()[:500]}"
            )
            repo_display = self.remote_url or "(unknown)"
            raise GitCloneError(
                (
                    f"*Yaver* could not **clone** the repository.\n\n"
                    f"*Repository:* `{repo_display}`\n"
                    f"*Detail:* {safe_err.strip()[:800] or 'git clone failed'}\n\n"
                    "Check that the URL is correct, the project is reachable, "
                    "and a GitLab PAT is configured for this host in dashboard "
                    "Settings (or `GITLAB_HOST_PATS`). Then move the issue back to *To Do*."
                ),
                technical=safe_err,
            )

        logger.info(f"Clone finished for {issue_tag} in {elapsed:.1f}s")
        # Ensure origin has no embedded credentials
        self._scrub_remote_credentials()
        self._enable_git_longpaths()

        # Init nested modules on the default tip from clone.
        # ensure_feature_branch re-runs after work-branch checkout so pins match.
        self._update_submodules(reason="after clone")

        # Do NOT create local tracking branches for every remote feature/*.
        # Clone already has origin/* refs (--no-single-branch); ensure_feature_branch
        # fetches only target/source when preparing the work branch.
        self._materialize_job_remote_refs()
        self._clone_in_progress = False

    @staticmethod
    def _host_from_url(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if not raw.startswith("http"):
            raw = "https://" + raw
        parsed = urlparse(raw)
        name = (parsed.hostname or "").lower()
        if parsed.port:
            return f"{name}:{parsed.port}"
        return name

    def _pat_for_remote(self, url: str = "") -> str:
        """Resolve GitLab PAT for this remote URL (per-host map).

        A lone ``GITLAB_PAT`` / Settings PAT with no host map still
        authenticates this job remote (clone/push). Host maps stay exact-match
        so a stored PAT is never sent to an unlisted host.
        """
        host = self._host_from_url(url or self.remote_url or "")
        if not host:
            return ""
        if hasattr(settings, "gitlab_pat_for_host"):
            mapped = (settings.gitlab_pat_for_host(host) or "").strip()
            if mapped:
                return mapped
        mapping = {}
        if hasattr(settings, "gitlab_host_pat_map"):
            try:
                mapping = settings.gitlab_host_pat_map() or {}
            except Exception:
                mapping = {}
        if mapping:
            return ""
        return (getattr(settings, "gitlab_pat", "") or "").strip()

    def _assert_remote_host_allowed(self, url: str) -> None:
        """Refuse to send a mapped PAT to an unknown host.

        When no PATs are configured at all, any host is allowed (public clone).
        A lone ``GITLAB_PAT`` (no host map) authenticates the job remote.
        When a host→PAT map exists, the repository host must be in that map.
        """
        mapping = (
            settings.gitlab_host_pat_map()
            if hasattr(settings, "gitlab_host_pat_map")
            else {}
        )
        if not mapping and not (settings.gitlab_pat or "").strip():
            return
        host = self._host_from_url(url)
        if not host:
            raise GitCloneError(
                "*Yaver* could not clone: repository URL has no host.\n\n"
                "Set `Repository: https://gitlab.example.com/group/repo.git` in `{params}`."
            )
        pat = self._pat_for_remote(url)
        if pat:
            return
        allowed = sorted(mapping.keys()) if mapping else list(
            settings.gitlab_allowed_hosts_list
        )
        if not allowed and (settings.gitlab_pat or "").strip():
            raise GitCloneError(
                "*Yaver* refused to authenticate: "
                "no GitLab host→PAT mapping is configured while a PAT is set.\n\n"
                "Add hosts in dashboard Settings (GitLab credentials), set "
                "`GITLAB_HOST_PATS={\"gitlab.example.com\":\"glpat-…\"}`, "
                "or set `GITLAB_ALLOWED_HOSTS` with legacy `GITLAB_PAT`."
            )
        raise GitCloneError(
            (
                f"*Yaver* refused to send credentials to host "
                f"`{host}`.\n\n"
                f"Configured hosts: `{', '.join(allowed) or '(none)'}`.\n"
                "Add this host with a PAT in dashboard Settings, or update the "
                "issue Repository URL."
            )
        )

    def _https_url_with_settings_pat(self, url: str = "") -> Optional[str]:
        """Build ``https://oauth2:PAT@host/...`` from settings, or None.

        Used only for VD child-process clone/push/fetch so the **settings** PAT
        is preferred without clearing the user's Windows credential helpers.
        When credentials are already in the URL, git does not call helpers.
        Origin is scrubbed back to a clean URL after the operation.
        """
        base = (url or self.remote_url or "").strip()
        if not base:
            return None
        pat = self._pat_for_remote(base)
        if not pat:
            return None
        if not base.startswith("http"):
            base = "https://" + base
        parsed = urlparse(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        # GitLab convention: username oauth2, password = PAT
        userinfo = f"oauth2:{quote(pat, safe='')}"
        host = parsed.hostname
        netloc = f"{userinfo}@{host}:{parsed.port}" if parsed.port else f"{userinfo}@{host}"
        return urlunparse(
            (
                parsed.scheme,
                netloc,
                parsed.path or "",
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )

    def _apply_settings_pat_to_origin(self) -> bool:
        """Temporarily set origin to a settings-PAT URL. Returns True if applied."""
        auth_url = self._https_url_with_settings_pat()
        if not auth_url or not self.temp_dir or not self.temp_dir.is_dir():
            return False
        try:
            self._run_tracked(
                ["git", "remote", "set-url", "origin", auth_url],
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                timeout=self._git_command_timeout(),
            )
            return True
        except GitCancelledError:
            raise
        except Exception as e:
            logger.warning(f"Could not apply settings PAT to origin: {e}")
            return False

    def _git_auth_env(self, *, url: str = "") -> Dict[str, str]:
        """Optional askpass env (legacy/tests). Does **not** clear credential helpers.

        Preferred auth for clone/push is ``_https_url_with_settings_pat`` so the
        Windows credential helper remains available for the user and for ops
        without a settings PAT. Askpass alone loses to Windows GCM when both run.
        """
        env = dict(os.environ)
        pat = self._pat_for_remote(url or self.remote_url or "")
        if not pat:
            return env
        askpass = self._ensure_askpass_script()
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["VD_GIT_PASSWORD"] = pat
        return env

    @staticmethod
    def _ensure_askpass_script() -> Path:
        """Create (once) a small cross-platform askpass helper under temp."""
        # Prefer a stable path under the agent runtime so we do not rewrite each call
        base = Path.cwd() / ".jira-agent" / "bin"
        base.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            path = base / "vd-git-askpass.cmd"
            content = (
                "@echo off\r\n"
                "set \"PROMPT=%~1\"\r\n"
                "echo %PROMPT% | findstr /I \"Username username\" >nul\r\n"
                "if not errorlevel 1 (\r\n"
                "  echo oauth2\r\n"
                "  exit /b 0\r\n"
                ")\r\n"
                "echo %VD_GIT_PASSWORD%\r\n"
            )
        else:
            path = base / "vd-git-askpass.sh"
            content = (
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *[Uu]sername*) echo oauth2 ;;\n"
                "  *) printf '%s\\n' \"$VD_GIT_PASSWORD\" ;;\n"
                "esac\n"
            )
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            if os.name != "nt":
                try:
                    path.chmod(0o700)
                except OSError:
                    pass
        return path

    def _build_clone_url(self, base_url: str, pat: str) -> str:
        """Return a clean remote URL.

        Historical helper used to embed ``oauth2:PAT@`` in the URL. That leaked
        the token via process argv. Auth is now via ``_git_auth_env`` / askpass;
        ``pat`` is ignored and never embedded.
        """
        _ = pat  # intentionally unused — keep signature for callers/tests
        return base_url

    def _scrub_remote_credentials(self) -> None:
        """Point origin at the clean remote URL (no embedded PAT)."""
        if not self.remote_url or not self.temp_dir:
            return
        try:
            self._run_git(["remote", "set-url", "origin", self.remote_url], check=False)
            logger.debug("Scrubbed credentials from origin remote URL")
        except Exception as e:
            logger.warning(f"Could not scrub origin credentials: {e}")

    def _with_auth_remote(self) -> None:
        """Ensure origin uses the clean URL; auth is supplied via env/askpass.

        Kept for call-site compatibility. Never embeds PAT into the remote URL.
        """
        if not self.remote_url:
            return
        self._assert_remote_host_allowed(self.remote_url)
        self._run_git(["remote", "set-url", "origin", self.remote_url], check=False)

    def _job_branch_names(self) -> List[str]:
        """Source/target names for this issue (deduped, non-empty)."""
        names: List[str] = []
        for raw in (self.target_branch, self.source_branch):
            n = (raw or "").strip()
            if n and n not in names:
                names.append(n)
        return names

    def _materialize_job_remote_refs(self) -> None:
        """Fetch only this job's source/target tips — not every remote branch.

        Previously we listed all ``origin/*`` and created a local tracking
        branch for each (causing noisy ``rev-parse`` failures on a fresh clone
        and pointless work for old feature/* branches). Job setup only needs
        target (MR base) and optionally source (existing work tip).
        """
        branches = self._job_branch_names()
        if not branches:
            logger.debug(
                "No source/target set at clone time; "
                "ensure_feature_branch will fetch when preparing work"
            )
            return
        if not self.remote_url:
            return
        logger.info(f"Fetching job branches only (not full remote mirror): {branches}")
        try:
            self._with_auth_remote()
            try:
                for branch in branches:
                    self._run_git(
                        ["fetch", "origin", branch],
                        check=False,
                        auth=True,
                    )
            finally:
                self._scrub_remote_credentials()
        except Exception as e:
            logger.warning(f"Could not fetch job branches {branches}: {e}")

    def _sync_remote_branches(self) -> None:
        """Backward-compatible alias: only materialize source/target, not all remotes."""
        self._materialize_job_remote_refs()

    def _git_command_timeout(self) -> int:
        """Wall-clock cap for non-clone git/glab (push, fetch, MR, etc.)."""
        return max(
            30,
            int(getattr(settings, "git_command_timeout_seconds", 300) or 300),
        )

    def _init_proc_state(self) -> None:
        """Idempotent init so ``__new__`` test instances still work."""
        if not hasattr(self, "_cancelled"):
            self._cancelled = False
        if not hasattr(self, "_clone_in_progress"):
            self._clone_in_progress = False
        if not hasattr(self, "_live_procs"):
            self._live_procs: List[subprocess.Popen] = []
        if not hasattr(self, "_proc_lock"):
            self._proc_lock = threading.Lock()

    def _register_live(self) -> None:
        self._init_proc_state()
        key = (self.issue_key or "").strip()
        if not key:
            return
        with GitManager._live_lock:
            GitManager._live_by_issue[key] = self

    def _unregister_live(self) -> None:
        key = (self.issue_key or "").strip()
        if not key:
            return
        with GitManager._live_lock:
            if GitManager._live_by_issue.get(key) is self:
                GitManager._live_by_issue.pop(key, None)

    @classmethod
    def live_for(cls, issue_key: str) -> Optional["GitManager"]:
        """Return the in-flight GitManager for ``issue_key``, if any."""
        key = (issue_key or "").strip()
        if not key:
            return None
        with cls._live_lock:
            return cls._live_by_issue.get(key)

    @staticmethod
    def _subprocess_run_is_patched() -> bool:
        """True when unit tests replaced ``subprocess.run`` with a mock."""
        return getattr(subprocess.run, "__module__", "") != "subprocess"

    def _run_tracked(
        self,
        cmd: list,
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        text: bool = True,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """Run a subprocess that cancel can force-kill (process group).

        Unit tests that patch ``src.git_manager.subprocess.run`` still go
        through that mock so existing fixtures keep working.
        """
        self._init_proc_state()
        if self._cancelled:
            raise GitCancelledError(
                f"git cancelled before start: {' '.join(str(c) for c in cmd[:6])}"
            )
        run_kwargs: Dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "timeout": timeout,
            "capture_output": capture_output,
            "text": text,
        }
        if encoding is not None:
            run_kwargs["encoding"] = encoding
        if errors is not None:
            run_kwargs["errors"] = errors
        if self._subprocess_run_is_patched():
            return subprocess.run(cmd, **run_kwargs)
        return self._popen_wait(cmd, **run_kwargs)

    def _popen_wait(
        self,
        cmd: list,
        *,
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        capture_output: bool = True,
        text: bool = True,
        encoding: Optional[str] = None,
        errors: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """``subprocess.run`` equivalent that is registered and group-killable."""
        self._init_proc_state()
        popen_kwargs: Dict[str, Any] = {
            "args": cmd,
            "cwd": str(cwd) if cwd is not None else None,
            "env": env,
            "stdout": subprocess.PIPE if capture_output else None,
            "stderr": subprocess.PIPE if capture_output else None,
        }
        if text or encoding:
            popen_kwargs["text"] = True
            if encoding:
                popen_kwargs["encoding"] = encoding
            if errors:
                popen_kwargs["errors"] = errors
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(**popen_kwargs)
        with self._proc_lock:
            self._live_procs.append(proc)
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_process_tree(proc, force=True)
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except Exception:
                    stdout, stderr = ("", "") if text or encoding else (b"", b"")
                if self._cancelled:
                    raise GitCancelledError(
                        f"git cancelled: {' '.join(str(c) for c in cmd[:6])}"
                    )
                raise
            if self._cancelled:
                raise GitCancelledError(
                    f"git cancelled: {' '.join(str(c) for c in cmd[:6])}"
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0 if proc.returncode is None else proc.returncode,
                stdout=stdout if stdout is not None else ("" if text or encoding else b""),
                stderr=stderr if stderr is not None else ("" if text or encoding else b""),
            )
        finally:
            with self._proc_lock:
                if proc in self._live_procs:
                    self._live_procs.remove(proc)

    def cancel_processes(self, *, force: bool = True) -> int:
        """Force-kill every live git/glab child (and leftover path users)."""
        self._init_proc_state()
        self._cancelled = True
        with self._proc_lock:
            procs = list(self._live_procs)
        killed = 0
        for proc in procs:
            if getattr(proc, "poll", lambda: None)() is not None:
                continue
            kill_process_tree(proc, force=True)
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            killed += 1
        return killed

    def should_discard_on_cancel(self) -> bool:
        """True when cancel should delete this workspace (incomplete clone)."""
        self._init_proc_state()
        if self._clone_in_progress:
            return True
        if not self.temp_dir:
            return False
        try:
            if not self.temp_dir.exists():
                return False
            if not (self.temp_dir / ".git").exists():
                return True
        except OSError:
            return True
        return False

    def _rmtree_best_effort(self, path: Path, *, timeout: float = 5.0) -> bool:
        """Delete ``path`` without blocking cancel if the FS stalls (WSL/9p)."""
        err: List[BaseException] = []

        def _rm() -> None:
            try:
                if path.exists():
                    shutil.rmtree(path)
            except Exception as e:
                err.append(e)

        t = threading.Thread(target=_rm, name="discard-clone", daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning(
                f"rmtree still running after {timeout:.0f}s (clone will be orphaned): {path}"
            )
            return False
        if err:
            logger.warning(f"rmtree failed for {path}: {err[0]}")
            return False
        return True

    def discard_workspace(self) -> bool:
        """Force-delete the temp clone after cancel. Ignores age policy."""
        self._init_proc_state()
        self.cancel_processes(force=True)
        path = self.temp_dir
        if path is None:
            self._unregister_live()
            return True
        # Re-kill once more in case a child re-locked files, then delete.
        time.sleep(0.05)
        self.cancel_processes(force=True)
        if self._rmtree_best_effort(path, timeout=5.0):
            self.temp_dir = None
            self._unregister_live()
            logger.info(f"Discarded cancelled clone workspace: {path}")
            return True
        logger.error(f"Failed to discard cancelled clone {path}")
        self._unregister_live()
        return False

    def _run_git(
        self,
        args: list,
        cwd: Optional[Path] = None,
        check: bool = True,
        *,
        auth: bool = False,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the temp working directory.

        When ``auth=True`` and a settings PAT exists for the host, temporarily
        points ``origin`` at ``https://oauth2:PAT@…`` so **settings** win without
        clearing Windows credential helpers. Scrubs origin afterward. With no
        settings PAT, leaves helpers alone (Windows Credential Manager can apply).

        Always applies a hard timeout (default ``git_command_timeout_seconds``)
        so a hung push/fetch cannot pin a job slot forever.
        """
        cwd = cwd or self.temp_dir

        if cwd is None or not cwd.exists() or not cwd.is_dir():
            logger.error(f"Git operations locked: temp directory '{cwd}' does not exist")
            raise RuntimeError(f"Git operations locked: temp directory '{cwd}' does not exist")

        # Force UTF-8 log/output so commit subjects with Turkish (ğüşıöç…)
        # are not mojibake'd on Windows cp1254 locale when building MR titles.
        # Disable LFS filter hooks so a broken git-lfs install cannot fail checkout.
        cmd = [
            "git",
            "-c",
            "core.longpaths=true",
            "-c",
            "i18n.logOutputEncoding=utf-8",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.process=",
            "-c",
            "filter.lfs.required=false",
        ] + list(args)
        safe_args = self._redact_git_args(args)
        logger.debug(f"Running git command: git {' '.join(safe_args)}")

        applied_settings_pat = False
        if auth:
            applied_settings_pat = self._apply_settings_pat_to_origin()

        cmd_timeout = (
            max(30, int(timeout))
            if timeout is not None
            else self._git_command_timeout()
        )
        git_env = self._apply_pat_to_git_env(self._base_git_env())
        try:
            result = self._run_tracked(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=cmd_timeout,
                env=git_env,
            )
        except GitCancelledError:
            raise
        except subprocess.TimeoutExpired as e:
            safe_err = f"git command timed out after {cmd_timeout}s: git {' '.join(safe_args)}"
            logger.error(safe_err)
            if check:
                raise RuntimeError(safe_err) from e
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=safe_err,
            )
        finally:
            if applied_settings_pat:
                try:
                    self._scrub_remote_credentials()
                except Exception:
                    pass

        if result.returncode != 0:
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            safe_err = self._redact_secret_text(result.stderr or "")
            # rev-parse --verify is often a "does this ref exist?" probe with
            # check=False; log at debug so missing local branches are not ERROR spam.
            is_probe = (
                not check
                and args
                and args[0] == "rev-parse"
                and "--verify" in args
            )
            # Checkout often succeeds ("Switched to…") then LFS smudge fails with
            # a bogus empty git version — treat as soft success when HEAD matches.
            is_checkout = bool(args) and args[0] == "checkout"
            if (
                is_checkout
                and self._looks_like_lfs_filter_noise(combined)
                and self._checkout_landed_on_intended_branch(args)
            ):
                logger.warning(
                    f"Git checkout succeeded but LFS filter complained "
                    f"(ignored): git {' '.join(safe_args)} — {safe_err.strip()[:300]}"
                )
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=result.stdout or "",
                    stderr=result.stderr or "",
                )
            if is_probe:
                logger.debug(
                    f"Git ref missing (probe): git {' '.join(safe_args)} — {safe_err.strip()}"
                )
            else:
                logger.error(
                    f"Git command failed: git {' '.join(safe_args)}\n{safe_err}"
                )
            if check:
                raise RuntimeError(
                    f"Git command failed: git {' '.join(safe_args)}\n{safe_err}"
                )
        else:
            logger.debug(f"Git command succeeded: git {' '.join(safe_args)}")

        return result

    def _checkout_landed_on_intended_branch(self, args: list) -> bool:
        """True when a failed ``git checkout`` still left HEAD on the target branch."""
        if not args or args[0] != "checkout":
            return False
        intended = ""
        # Patterns: checkout <branch> | checkout -B <branch> <start> | checkout -b <branch>
        tokens = [a for a in args[1:] if a and not a.startswith("-")]
        if tokens:
            intended = tokens[0].strip()
        # Also handle ``checkout -B name start`` where -B is flag
        if not intended:
            for i, a in enumerate(args):
                if a in ("-B", "-b") and i + 1 < len(args):
                    intended = (args[i + 1] or "").strip()
                    break
        if not intended or intended.startswith("origin/"):
            # start_point only — resolve current branch name only
            intended = intended[len("origin/") :] if intended.startswith("origin/") else intended
        if not intended:
            return False
        try:
            head = self.get_current_branch()
        except Exception:
            return False
        return (head or "").strip() == intended

    @staticmethod
    def _redact_secret_text(text: str) -> str:
        """Strip embedded PATs/tokens from git URLs in logs/errors."""
        if not text:
            return text
        # https://oauth2:TOKEN@host  or https://user:TOKEN@host
        text = re.sub(
            r"(https?://)([^/@\s]+):([^@/\s]+)@",
            r"\1\2:***@",
            text,
        )
        pats = (
            settings.all_gitlab_pats()
            if hasattr(settings, "all_gitlab_pats")
            else []
        )
        if not pats:
            single = (settings.gitlab_pat or "").strip()
            if single:
                pats = [single]
        for pat in pats:
            if pat:
                text = text.replace(pat, "***")
        return text

    @classmethod
    def _redact_git_args(cls, args: list) -> list:
        return [cls._redact_secret_text(str(a)) for a in args]

    def _has_commits(self) -> bool:
        """Check if the repo has any commits."""
        result = self._run_git(["rev-parse", "--verify", "HEAD"], check=False)
        has_commits = result.returncode == 0
        logger.debug(f"Repository has commits: {has_commits}")
        return has_commits

    def _branch_exists(self, branch_name: str, check_remote: bool = False) -> bool:
        """Check if a branch exists locally or on remote."""
        result = self._run_git(["rev-parse", "--verify", f"refs/heads/{branch_name}"], check=False)
        if result.returncode == 0:
            logger.debug(f"Local branch exists: {branch_name}")
            return True

        if check_remote:
            result = self._run_git(["rev-parse", "--verify", f"refs/remotes/origin/{branch_name}"], check=False)
            remote_exists = result.returncode == 0
            if remote_exists:
                logger.debug(f"Remote branch exists: {branch_name}")
            return remote_exists

        return False

    def _delete_local_branch(self, branch_name: str) -> bool:
        logger.debug(f"Checking if branch exists for deletion: {branch_name}")
        if not self._branch_exists(branch_name):
            logger.debug(f"Branch does not exist, no deletion needed: {branch_name}")
            return True
        
        try:
            current = self.get_current_branch()
            if current == branch_name:
                logger.debug(f"Currently on branch {branch_name}, switching to default")
                if not self._checkout_default_branch():
                    self._run_git(["checkout", "-b", "_temp_branch_for_delete"], check=False)
            
            logger.info(f"Deleting local branch: {branch_name}")
            self._run_git(["branch", "-D", branch_name], check=False)
            logger.info(f"Deleted local branch: {branch_name}")
            return True
        except Exception as e:
            logger.warning(f"Could not delete branch {branch_name}: {e}")
            return False

    def _safe_git_ref(self, branch: str) -> Optional[str]:
        """Return branch if it is a safe git ref (not an option like --mirror)."""
        name = (branch or "").strip()
        if not name or name.startswith("-"):
            logger.error(f"Refusing option-like git ref: {branch!r}")
            return None
        return name

    def _remote_head_exists(self, branch: str) -> bool:
        """True if origin has refs/heads/{branch} (uses ls-remote)."""
        branch = self._safe_git_ref(branch) or ""
        if not branch:
            return False
        self._with_auth_remote()
        try:
            result = self._run_git(
                ["ls-remote", "--heads", "origin", "--", branch], check=False, auth=True
            )
        finally:
            self._scrub_remote_credentials()
        needle = f"refs/heads/{branch}"
        for line in (result.stdout or "").splitlines():
            if line.rstrip().endswith(needle):
                return True
        return False

    @staticmethod
    def _is_primary_base(branch: str) -> bool:
        name = (branch or "").strip().lower()
        if not name:
            return False
        if name in _PRIMARY_BASES:
            return True
        return name.startswith("release/")

    @staticmethod
    def resolve_work_branch_name(
        issue_key: Optional[str],
        source_branch: str = "",
        target_branch: str = "",
        *,
        keep_source: bool = False,
    ) -> str:
        """Resolved work branch (no clone). Same rules as instance resolver."""
        source = (source_branch or "").strip()
        target = (target_branch or "").strip()
        if keep_source and source:
            return source
        safe_key = re.sub(
            r"[^A-Za-z0-9\-]", "-", issue_key or "issue"
        )
        feature = f"feature/{safe_key}"
        if source and source != target and not GitManager._is_primary_base(source):
            return source
        return feature

    def _resolve_work_branch_name(self, issue_key: Optional[str] = None) -> str:
        """Pick the branch agents commit on (MR source side).

        Rules (params ``Source branch`` is authoritative when it is a real work
        branch):

        * Source empty, equals target, or is a primary base (``main`` /
          ``develop`` / ``release/*`` / …) → ``feature/{ISSUE_KEY}``
        * Otherwise → use Source as-is (may differ from the Jira key, e.g.
          ``feature/legacy-name`` or ``fix/hotfix-login``)

        Existence on the remote is checked later in ``ensure_feature_branch``:
        existing remote source is checked out; missing source is created from
        target.
        """
        key = issue_key or self.issue_key
        source = (self.source_branch or "").strip()
        target = (self.target_branch or "").strip()
        work = self.resolve_work_branch_name(
            key,
            source,
            target,
            keep_source=bool(getattr(self, "keep_source_work_branch", False)),
        )
        if source and source != target and not self._is_primary_base(source):
            logger.info(
                f"Using params source as work branch: {source} "
                f"(MR will be {source} → {target or '(target)'}; "
                f"issue key for commits is {key})"
            )
        else:
            logger.info(
                f"Using isolated work branch {work} "
                f"(params source was '{source or '(none)'}'; MR → {target or '(target)'})"
            )
        return work

    def _require_target_on_remote(self) -> str:
        """Target must exist on origin before any work. Returns target name."""
        target = (self.target_branch or "").strip()
        if not target:
            raise GitTargetBranchError(
                "*Yaver* could not start: no **target branch** on the issue.\n\n"
                "Add `Target branch: develop` (or your integration branch) inside `{params}`.\n"
                "The target must already exist on GitLab; the MR merges **into** it."
            )

        logger.info(f"Validating target branch on remote: origin/{target}")
        self._with_auth_remote()
        try:
            self._run_git(["fetch", "origin", "--prune"], check=False, auth=True)
        finally:
            self._scrub_remote_credentials()

        if not self._remote_head_exists(target):
            raise GitTargetBranchError(
                (
                    f"*Yaver* could not start: **target branch** missing on GitLab.\n\n"
                    f"*Target branch:* `{target}`\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`\n"
                    f"*Detail:* `origin/{target}` not found (ls-remote)\n\n"
                    "The target must already exist. Fix `Target branch` in the issue "
                    "`{params}` block (e.g. `Target branch: develop`), then move the "
                    "issue back to *To Do*."
                ),
                technical=f"ls-remote origin/{target} empty",
            )

        # Materialize local tracking ref for checkout -B
        self._with_auth_remote()
        try:
            self._run_git(["fetch", "origin", target], check=False, auth=True)
        finally:
            self._scrub_remote_credentials()

        logger.info(f"Target branch confirmed on remote: origin/{target}")
        return target

    def _working_tree_status(self) -> str:
        if not self.temp_dir or not (Path(self.temp_dir) / ".git").is_dir():
            return ""
        result = self._run_git(["status", "--porcelain"], check=False)
        return (result.stdout or "").strip()

    def _dirty_paths_summary(self, porcelain: str, *, limit: int = 8) -> str:
        paths: list[str] = []
        for line in (porcelain or "").splitlines():
            name = line[3:].strip() if len(line) > 3 else line.strip()
            if name:
                paths.append(name)
        if not paths:
            return "(none)"
        shown = paths[: max(1, limit)]
        extra = len(paths) - len(shown)
        text = ", ".join(shown)
        if extra > 0:
            text += f" (+{extra} more)"
        return text

    def _stash_uncommitted(self, reason: str) -> bool:
        """Stash tracked + untracked edits so a branch switch can proceed.

        Never ``reset --hard``: leftover work may be intentional. The stash
        stays in the clone (``git stash list``) for recovery.
        """
        porcelain = self._working_tree_status()
        if not porcelain:
            logger.info(
                f"Working tree clean; no stash ({reason})"
            )
            return False
        msg = f"vd: preserve uncommitted {reason}".strip()
        summary = self._dirty_paths_summary(porcelain)
        logger.info(
            f"Git command: git stash push -u -m {msg!r} "
            f"({reason}; files: {summary})"
        )
        result = self._run_git(
            ["stash", "push", "-u", "-m", msg],
            check=False,
        )
        if result.returncode != 0:
            safe_err = self._redact_secret_text(
                (result.stderr or result.stdout or "").strip()
            )
            logger.warning(
                f"Git command failed: git stash push -u -m {msg!r}\n{safe_err[:300]}"
            )
            return False
        logger.info(
            f"Git command succeeded: git stash push -u -m {msg!r}; "
            f"recover with git stash pop"
        )
        return True

    def _checkout_work_branch_from_target(self, work_branch: str, target: str) -> str:
        """Create or reset *work_branch* from origin/target tip, then checkout it.

        Used when the source branch does **not** exist on the remote yet.
        Does not push (push happens after agent work).
        """
        work_branch = (work_branch or "").strip()
        target = (target or "").strip()
        if not work_branch:
            raise GitSourceBranchError(
                "*Yaver* could not create a work branch: empty name."
            )
        if work_branch == target:
            raise GitSourceBranchError(
                (
                    f"*Yaver* refused to use target `{target}` as the work branch.\n\n"
                    "Source and target resolved to the same name. Set "
                    "`Source branch: feature/YOUR-KEY` or leave source as a primary "
                    "base so the agent uses `feature/{KEY}`."
                )
            )

        logger.info(
            f"Source branch '{work_branch}' not on remote — creating from "
            f"origin/{target} (MR will be {work_branch} → {target})"
        )
        self._stash_uncommitted(f"before creating {work_branch} from {target}")
        self._delete_local_branch(work_branch)

        start_point = f"origin/{target}"
        if not self._branch_exists(target, check_remote=True):
            # Fallback after fetch failure edge cases
            if self._branch_exists(target, check_remote=False):
                start_point = target
            else:
                raise GitTargetBranchError(
                    (
                        f"*Yaver* could not base work on target `{target}`: "
                        f"local ref `origin/{target}` missing after fetch.\n\n"
                        f"*Repository:* `{self.remote_url or '(unknown)'}`"
                    ),
                    technical=f"origin/{target} missing after fetch",
                )

        self._run_git(["checkout", "-B", work_branch, start_point])
        logger.info(
            f"Checked out work branch '{work_branch}' from '{start_point}' "
            f"(new branch from target for agent work)"
        )
        self.work_branch = work_branch
        # Keep source_branch aligned with actual MR source for logging/MR create
        self.source_branch = work_branch
        return work_branch

    def _checkout_existing_remote_branch(self, work_branch: str) -> str:
        """Checkout an existing remote source tip (do **not** re-base onto target).

        Preserves commits already on the source branch so multi-run / shared
        source names that differ from the Jira key still deliver work.
        """
        work_branch = (work_branch or "").strip()
        if not work_branch:
            raise GitSourceBranchError(
                "*Yaver* could not checkout work branch: empty name."
            )

        logger.info(
            f"Source branch '{work_branch}' exists on remote — checking out "
            f"origin/{work_branch} (not re-creating from target)"
        )
        self._with_auth_remote()
        try:
            self._run_git(["fetch", "origin", work_branch], check=False, auth=True)
        finally:
            self._scrub_remote_credentials()

        start_point = f"origin/{work_branch}"
        if not self._origin_ref_is_commit(work_branch):
            # ls-remote said yes but no tracking ref — last-chance fetch
            self._with_auth_remote()
            try:
                self._run_git(
                    ["fetch", "origin", f"+refs/heads/{work_branch}:refs/remotes/origin/{work_branch}"],
                    check=False,
                    auth=True,
                )
            finally:
                self._scrub_remote_credentials()
        if not self._origin_ref_is_commit(work_branch):
            logger.warning(
                f"origin/{work_branch} is not a usable commit after fetch; "
                f"falling back to local branch or target"
            )
            if self._branch_exists(work_branch, check_remote=False):
                return self._checkout_local_work_branch(work_branch)
            return self._checkout_work_branch_from_target(
                work_branch, (self.target_branch or "").strip()
            )

        current = (self.get_current_branch() or "").strip()
        if current == work_branch:
            # Reused clone already on this MR source — keep commits and
            # intentional uncommitted edits (GitLab comment / rework).
            porcelain = self._working_tree_status()
            if porcelain:
                logger.info(
                    f"Already on '{work_branch}'; skip checkout/stash — "
                    f"preserving uncommitted files: "
                    f"{self._dirty_paths_summary(porcelain)}"
                )
            else:
                logger.info(
                    f"Already on '{work_branch}'; skip checkout — "
                    f"fast-forward to {start_point} if possible"
                )
                self._run_git(["merge", "--ff-only", start_point], check=False)
            self.work_branch = work_branch
            self.source_branch = work_branch
            return work_branch

        logger.info(
            f"Switching to existing work branch '{work_branch}' "
            f"(currently '{current or '(detached)'}')"
        )
        self._stash_uncommitted(f"before checkout {work_branch}")
        if self._branch_exists(work_branch, check_remote=False):
            self._run_git(["checkout", work_branch])
        else:
            self._run_git(["checkout", "-B", work_branch, start_point])
        logger.info(
            f"Checked out existing work branch '{work_branch}' "
            f"(local commits kept; origin tip is {start_point})"
        )
        self.work_branch = work_branch
        self.source_branch = work_branch
        return work_branch

    def _origin_ref_is_commit(self, branch: str) -> bool:
        """True when ``refs/remotes/origin/{branch}`` resolves to a commit."""
        name = (branch or "").strip()
        if not name:
            return False
        result = self._run_git(
            ["rev-parse", "--verify", f"refs/remotes/origin/{name}^{{commit}}"],
            check=False,
        )
        return result.returncode == 0

    def _checkout_local_work_branch(self, work_branch: str) -> str:
        """Checkout a local-only work branch (unpushed previous run)."""
        work_branch = (work_branch or "").strip()
        if not work_branch:
            raise GitSourceBranchError(
                "*Yaver* could not checkout work branch: empty name."
            )
        logger.info(
            f"Source branch '{work_branch}' exists locally but not on remote — "
            f"checking out the local tip (not origin/{work_branch})"
        )
        current = (self.get_current_branch() or "").strip()
        if current != work_branch:
            self._stash_uncommitted(f"before checkout {work_branch}")
        self._run_git(["checkout", work_branch])
        self.work_branch = work_branch
        self.source_branch = work_branch
        return work_branch

    def _prepare_work_branch(self, work_branch: str, target: str) -> str:
        """Use remote source if present; otherwise create source from target.

        This is the required operator contract for ``Source branch`` in
        ``{params}`` when the name differs from the Jira issue key.
        """
        work_branch = (work_branch or "").strip()
        target = (target or "").strip()
        if not work_branch:
            raise GitSourceBranchError(
                "*Yaver* could not prepare a work branch: empty name."
            )
        if work_branch == target:
            raise GitSourceBranchError(
                (
                    f"*Yaver* refused to use target `{target}` as the work branch.\n\n"
                    "Source and target resolved to the same name. Set a dedicated "
                    "`Source branch` (or a primary base so the agent uses "
                    "`feature/{KEY}`)."
                )
            )

        # 1) Prefer existing remote source tip (ls-remote). Do **not** treat
        # a leftover local branch as "exists on remote" — that called
        # checkout -B origin/{work} when the branch was never pushed
        # (scheduled rework / shared Source). Git then fails:
        #   fatal: 'origin/feature/…' is not a commit
        if self._remote_head_exists(work_branch):
            return self._checkout_existing_remote_branch(work_branch)

        # 2) Previous run created the work branch locally but did not push.
        # Rework must keep that tip, not invent origin/{work}.
        if self._branch_exists(work_branch, check_remote=False):
            return self._checkout_local_work_branch(work_branch)

        # 3) Missing locally and on remote → create from target
        return self._checkout_work_branch_from_target(work_branch, target)

    def _checkout_or_create_branch(self, branch_name: str) -> str:
        """Ensure target exists, then prepare *branch_name* (remote or from target).

        Kept for tests/callers that pass an explicit work branch name.
        """
        target = self._require_target_on_remote()
        return self._prepare_work_branch(branch_name, target)

    def _checkout_source_branch(self) -> None:
        """Legacy helper: require target, prepare resolved work branch."""
        target = self._require_target_on_remote()
        work = self._resolve_work_branch_name(self.issue_key)
        self._prepare_work_branch(work, target)

    def _create_source_from_target(self, source: str, target: str) -> bool:
        """Create local work branch *source* from *target* tip.

        Returns True if checked out afterward. Used by tests and internal paths.
        """
        source = (source or "").strip()
        target = (target or "").strip()
        if not source or not target or source == target:
            return False
        try:
            # Trust caller that target exists; still try to fetch
            if not self._remote_head_exists(target) and not self._branch_exists(
                target, check_remote=True
            ) and not self._branch_exists(target, check_remote=False):
                return False
            self._checkout_work_branch_from_target(source, target)
            return True
        except (GitSourceBranchError, GitTargetBranchError, RuntimeError) as e:
            logger.warning(f"Could not create source '{source}' from '{target}': {e}")
            return False

    def _checkout_default_branch(self) -> bool:
        """Checkout target tip (read-only base) for cleanup helpers."""
        try:
            target = self._require_target_on_remote()
            self._stash_uncommitted(f"before checkout {target}")
            self._run_git(["checkout", "-B", target, f"origin/{target}"], check=False)
            if self._branch_exists(target, check_remote=False):
                return True
            return False
        except (GitTargetBranchError, RuntimeError) as e:
            logger.error(str(e))
            return False

    def ensure_feature_branch(self, issue_key: Optional[str] = None) -> Optional[str]:
        """Validate target, prepare work branch (remote source or from target).

        This is the single entrypoint used by the processor before agent work.

        * Params ``Source branch`` is used when it is not a primary base.
        * If that branch exists on the remote → checkout it.
        * If not → create it from the target tip.
        * Commit messages still use the Jira issue key (prompt kit), not the
          branch name.
        """
        key = issue_key or self.issue_key
        logger.info(
            f"ensure_feature_branch for {key}: "
            f"params source={self.source_branch!r} target={self.target_branch!r}"
        )
        target = self._require_target_on_remote()
        work = self._resolve_work_branch_name(key)
        checked_out = self._prepare_work_branch(work, target)
        # Submodule SHAs often differ per branch — refresh after checkout so the
        # agent sees the tree for the work branch (not only the clone default).
        self._update_submodules(
            reason=f"after work branch checkout ({checked_out or work})"
        )
        return checked_out
    def _format_commit_message(self, issue_key: str, summary: str, description: str = "") -> str:
        """Format a commit message per agent/BUILD_PROMPT.md git policy.

        Required subject: ``[ISSUE-KEY] type: description``
        ``summary`` should already include the type prefix (e.g. ``fix: foo``).
        If it does not, ``chore:`` is prepended so the subject stays valid.
        """
        max_title_len = 72
        prefix = f"[{issue_key}] "
        body = (summary or "").strip()
        # Strip a leading [KEY] if caller already included it
        if body.startswith(f"[{issue_key}]"):
            body = body[len(f"[{issue_key}]"):].strip()
        allowed_types = (
            "feat", "fix", "refactor", "docs", "test",
            "perf", "ci", "build", "revert", "chore",
        )
        has_type = any(
            body.lower().startswith(f"{t}:") for t in allowed_types
        )
        if not has_type:
            body = f"chore: {body}" if body else "chore: update"

        available = max_title_len - len(prefix)
        short_summary = body
        if len(body) > available:
            short_summary = body[: available - 3] + "..."

        lines = [f"{prefix}{short_summary}"]

        if description and description.strip():
            desc = description.strip()
            if len(desc) > 500:
                desc = desc[:500] + "..."
            lines.append("")
            lines.append(desc)

        return "\n".join(lines)

    def _configure_git_identity(self) -> None:
        """Configure git user identity locally in the temp directory."""
        try:
            self._run_git(["config", "user.name", settings.git_user_name])
            self._run_git(["config", "user.email", settings.git_user_email])
        except RuntimeError:
            logger.warning("Could not configure git identity locally")

    def commit_changes(self, issue_key: str, summary: str, description: str = "") -> bool:
        """Stage all changes and create a commit."""
        try:
            self._configure_git_identity()

            status_result = self._run_git(["status", "--porcelain"])
            if not status_result.stdout.strip():
                logger.info("No changes to commit.")
                return True

            self._run_git(["add", "."])

            msg = self._format_commit_message(issue_key, summary, description)
            self._run_git(["commit", "-m", msg])

            logger.info(f"Committed changes for {issue_key}")
            return True

        except RuntimeError as e:
            logger.error(f"Commit failed: {e}")
            return False

    def get_current_branch(self) -> str:
        """Get the current branch name."""
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        if result.returncode != 0:
            return "main"
        return result.stdout.strip()

    def get_last_commit_message(self) -> Optional[str]:
        """Get the last commit message (subject + body)."""
        result = self._run_git(["log", "-1", "--format=%B"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def get_last_commit_subject(self) -> Optional[str]:
        """Get the last commit subject line only."""
        result = self._run_git(["log", "-1", "--format=%s"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def get_last_commit_sha(self, *, short: bool = False) -> Optional[str]:
        """Return HEAD commit SHA (full or short), or None if unavailable."""
        fmt = "%h" if short else "%H"
        result = self._run_git(["log", "-1", f"--format={fmt}"], check=False)
        if result.returncode != 0:
            return None
        sha = (result.stdout or "").strip()
        return sha or None

    def build_commit_url(self, commit_sha: Optional[str] = None) -> Optional[str]:
        """Best-effort GitLab web URL for a commit (https://host/group/repo/-/commit/SHA)."""
        sha = (commit_sha or self.get_last_commit_sha() or "").strip()
        if not sha:
            return None
        host, project = self._gitlab_host_and_project()
        if not host or not project:
            return None
        return f"https://{host}/{project}/-/commit/{sha}"

    def status(self) -> str:
        """Get git status output."""
        result = self._run_git(["status"])
        return result.stdout

    def commits_ahead_of_target(self, branch_name: Optional[str] = None) -> int:
        """How many commits ``branch`` is ahead of ``origin/{target}`` (0 if unknown)."""
        branch = (branch_name or self.work_branch or self.get_current_branch() or "").strip()
        target = (self.target_branch or "").strip()
        if not branch or not target:
            return 0
        result = self._run_git(
            ["rev-list", "--count", f"origin/{target}..{branch}"],
            check=False,
        )
        if result.returncode != 0:
            result = self._run_git(
                ["rev-list", "--count", f"{target}..{branch}"],
                check=False,
            )
        if result.returncode != 0:
            return 0
        try:
            return max(0, int((result.stdout or "0").strip() or "0"))
        except ValueError:
            return 0

    def ensure_on_work_branch(self) -> bool:
        """Checkout prepared ``work_branch`` if HEAD drifted. Returns False on failure."""
        work = (self.work_branch or "").strip()
        if not work:
            return False
        current = (self.get_current_branch() or "").strip()
        if current == work:
            return True
        try:
            self._run_git(["checkout", work], check=True)
            return (self.get_current_branch() or "").strip() == work
        except Exception as e:
            logger.error(f"Could not checkout work branch '{work}': {e}")
            return False

    def head_is_on_remote(self, branch_name: Optional[str] = None) -> bool:
        """True when ``origin/<branch>`` already points at (or contains) local HEAD.

        Used when the agent pushed itself: orchestrator push may no-op or fail
        while the remote already has the work — we still open an MR.
        """
        if not self.remote_enabled:
            return False
        branch = self._safe_git_ref(
            (branch_name or "").strip()
            or (self.work_branch or "").strip()
            or self.get_current_branch()
        )
        if not branch:
            return False
        try:
            head = (self.get_last_commit_sha() or "").strip()
        except Exception:
            head = ""
        if not head:
            return False
        try:
            # Refresh remote ref; ignore fetch failures (offline / missing branch).
            self._run_git(
                ["fetch", "origin", "--", branch],
                check=False,
                auth=True,
            )
            result = self._run_git(
                ["rev-parse", f"refs/remotes/origin/{branch}"],
                check=False,
            )
            # Do not use ``x or 1`` — returncode 0 is success (falsy).
            rev_rc = getattr(result, "returncode", 1)
            if rev_rc is None or int(rev_rc) != 0:
                return False
            remote = (result.stdout or "").strip()
            if not remote:
                return False
            if remote == head:
                return True
            # Abbreviated SHAs / tip already contains HEAD (agent pushed same tip).
            if remote.startswith(head) or head.startswith(remote):
                return True
            # HEAD is an ancestor of origin/branch (agent pushed further commits).
            anc = self._run_git(
                ["merge-base", "--is-ancestor", head, f"refs/remotes/origin/{branch}"],
                check=False,
            )
            anc_rc = getattr(anc, "returncode", 1)
            return anc_rc is not None and int(anc_rc) == 0
        except Exception as e:
            logger.debug(f"head_is_on_remote check failed for {branch}: {e}")
            return False

    def push(self, branch_name: Optional[str] = None) -> bool:
        """Push work branch to origin.

        Returns True when the remote has our commits (fresh push **or** the
        agent already pushed the same tip). Callers still open the MR after
        a successful return.
        """
        self.last_push_error = None
        if not self.remote_enabled:
            logger.info("Push not available (no remote configured).")
            self.last_push_error = "Push not available (no remote configured)."
            return False

        # Prefer prepared work_branch over drifted HEAD (B5)
        branch = self._safe_git_ref(
            (branch_name or "").strip()
            or (self.work_branch or "").strip()
            or self.get_current_branch()
        )
        if not branch:
            logger.error("Push refused: branch name missing or looks like a git option")
            self.last_push_error = (
                "Push refused: branch name missing or looks like a git option"
            )
            return False

        # auth=True → settings PAT on origin for the push when configured;
        # otherwise host Windows credential helpers remain available.
        try:
            self._with_auth_remote()
            try:
                self._run_git(["push", "-u", "origin", "--", branch], auth=True)
                logger.info(f"Pushed branch '{branch}' to origin.")
                return True
            except RuntimeError:
                logger.warning(f"Push failed, attempting to pull and merge...")
                try:
                    self._run_git(["fetch", "origin", "--", branch], check=False, auth=True)
                    self._run_git(
                        ["merge", f"origin/{branch}", "-m", f"Merge remote branch {branch}"],
                        check=False,
                    )
                    self._run_git(["push", "-u", "origin", "--", branch], auth=True)
                    logger.info(f"Pushed branch '{branch}' after merge.")
                    return True
                except RuntimeError as e2:
                    # Agent may have already pushed the same tip (or further).
                    if self.head_is_on_remote(branch):
                        logger.info(
                            f"Push failed but HEAD is already on origin/{branch} "
                            f"(agent likely pushed); treating push as success so "
                            f"the orchestrator can open an MR. Detail: {e2}"
                        )
                        return True
                    reason = summarize_git_error(e2) or str(e2).strip()
                    self.last_push_error = reason or "git push failed"
                    logger.error(f"Push failed after merge attempt: {e2}")
                    return False
        finally:
            self._scrub_remote_credentials()

    def _gitlab_host_and_project(self) -> tuple[str, str]:
        """Return (api_host, path_with_namespace) from the issue repository URL."""
        raw = self.remote_url or ""
        if not isinstance(raw, str):
            raw = str(raw) if raw else ""
        raw = raw.strip()
        if not raw:
            return "gitlab.com", ""
        if not raw.startswith("http"):
            raw = "https://" + raw
        parsed = urlparse(raw)
        host = (parsed.hostname or "gitlab.com").lower()
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = (parsed.path or "").strip("/").removesuffix(".git")
        return host, path

    def _glab_env(self) -> Dict[str, str]:
        """Env for glab subprocesses: inject host-specific GITLAB_TOKEN."""
        env = dict(os.environ)
        host, _ = self._gitlab_host_and_project()
        pat = self._pat_for_remote(self.remote_url or f"https://{host}")
        if pat:
            try:
                self._assert_remote_host_allowed(self.remote_url or f"https://{host}")
            except GitCloneError:
                logger.error(
                    f"Refusing to pass GitLab PAT to glab for unallowed host {host}"
                )
                # Do not inject token for untrusted hosts
                env["GITLAB_HOST"] = host
                env.setdefault("GITLAB_PROTOCOL", "https")
                return env
            env["GITLAB_TOKEN"] = pat
            # Common aliases used by different glab versions
            env.setdefault("GITLAB_ACCESS_TOKEN", pat)
            env.setdefault("GL_TOKEN", pat)
        env["GITLAB_HOST"] = host
        # Prefer HTTPS API; git push still uses authenticated remote URL separately
        env.setdefault("GITLAB_PROTOCOL", "https")
        # Windows glab/console often default to a legacy code page; force UTF-8
        # so MR titles with Turkish characters are not corrupted.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("LC_ALL", "C.UTF-8")
        env.setdefault("LANG", "C.UTF-8")
        return env

    def _run_glab(
        self,
        args: List[str],
        *,
        check: bool = False,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run glab with auth from settings (never log the token).

        Applies ``git_command_timeout_seconds`` so a hung MR create cannot pin
        a job slot forever.
        """
        cmd = ["glab", *args]
        logger.debug(f"Running glab: glab {' '.join(args)}")
        cmd_timeout = (
            max(30, int(timeout))
            if timeout is not None
            else self._git_command_timeout()
        )
        try:
            return self._run_tracked(
                cmd,
                cwd=self.temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._glab_env(),
                timeout=cmd_timeout,
            )
        except GitCancelledError:
            raise
        except subprocess.TimeoutExpired as e:
            safe_err = f"glab timed out after {cmd_timeout}s: glab {' '.join(args)}"
            logger.error(safe_err)
            if check:
                raise RuntimeError(safe_err) from e
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=safe_err,
            )

    def _create_mr_via_api(
        self,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str,
    ) -> Optional[str]:
        """Create MR via GitLab REST API using host-specific PAT (fallback if glab fails)."""
        host, project = self._gitlab_host_and_project()
        pat = self._pat_for_remote(self.remote_url or f"https://{host}")
        if not pat:
            logger.error("Cannot create MR via API: no GitLab PAT for this host")
            return None
        try:
            self._assert_remote_host_allowed(self.remote_url or f"https://{host}")
        except GitCloneError as e:
            logger.error(f"Cannot create MR via API: host not allowed: {e}")
            return None
        if not project:
            logger.error("Cannot create MR via API: project path missing from repository URL")
            return None
        enc = quote(project, safe="")
        url = f"https://{host}/api/v4/projects/{enc}/merge_requests"
        payload = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "description": body or title,
        }
        try:
            import httpx

            headers = {
                "PRIVATE-TOKEN": pat,
                # Explicit charset — GitLab expects UTF-8 JSON for titles.
                "Content-Type": "application/json; charset=utf-8",
            }
            # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
            with httpx.Client(timeout=30.0, verify=False) as client:
                resp = client.post(url, headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    web = data.get("web_url")
                    logger.info(f"Merge request created via API: {web}")
                    return web
                # Already exists
                if resp.status_code == 409 or "already exists" in (resp.text or "").lower():
                    logger.info("MR already exists (API 409); resolving URL")
                    return self._get_existing_mr_url(source_branch)
                logger.error(
                    f"GitLab API MR create failed ({resp.status_code}): "
                    f"{self._redact_secret_text(resp.text[:500])}"
                )
                return None
        except Exception as e:
            logger.error(f"GitLab API MR create error: {e}")
            return None

    def _mr_matches_target(self, mr: Any, target_branch: str) -> bool:
        want = (target_branch or self.target_branch or "").strip().lower()
        if not want or not isinstance(mr, dict):
            return True
        got = str(
            mr.get("target_branch")
            or mr.get("targetBranch")
            or ""
        ).strip().lower()
        return not got or got == want

    def _get_existing_mr_url(
        self, branch: str, target_branch: Optional[str] = None
    ) -> Optional[str]:
        want_tgt = (target_branch or self.target_branch or "").strip()
        # 1) glab with token env
        try:
            cmd = ["mr", "list", "--source-branch", branch]
            if want_tgt:
                cmd.extend(["--target-branch", want_tgt])
            cmd.append("--json")
            result = self._run_glab(cmd)
            if result.returncode == 0 and result.stdout.strip():
                mrs = json.loads(result.stdout)
                if isinstance(mrs, list):
                    for mr in mrs:
                        if self._mr_matches_target(mr, want_tgt):
                            return mr.get("web_url") or mr.get("url")
        except Exception:
            pass

        # 2) REST API fallback
        host, project = self._gitlab_host_and_project()
        pat = self._pat_for_remote(self.remote_url or f"https://{host}")
        if not pat or not project:
            return None
        try:
            import httpx

            enc = quote(project, safe="")
            q = (
                f"?source_branch={quote(branch)}&state=opened"
            )
            if want_tgt:
                q += f"&target_branch={quote(want_tgt)}"
            url = (
                f"https://{host}/api/v4/projects/{enc}/merge_requests{q}"
            )
            # INTENTIONAL: verify=False (on-prem / TLS intercept; no custom-CA path yet).
            with httpx.Client(timeout=30.0, verify=False) as client:
                resp = client.get(url, headers={"PRIVATE-TOKEN": pat})
                if resp.status_code == 200:
                    mrs = resp.json()
                    if isinstance(mrs, list):
                        for mr in mrs:
                            if self._mr_matches_target(mr, want_tgt):
                                return mr.get("web_url")
        except Exception as e:
            logger.debug(f"API MR list failed: {e}")
        return None

    def create_merge_request(self, title: str, body: str = "", target_branch: Optional[str] = None) -> Optional[str]:
        if not self.remote_enabled:
            logger.info("Merge request not available (no remote configured).")
            return None

        # Prefer prepared work_branch (never open MR from protected bases)
        branch = (self.work_branch or "").strip() or self.get_current_branch()
        protected = set(_PRIMARY_BASES) | {
            (self.target_branch or "").strip().lower(),
        }
        if not branch or branch.lower() in protected or branch.lower().startswith("release/"):
            logger.warning(
                f"Cannot create MR from protected/empty branch '{branch}'."
            )
            return None

        if not target_branch:
            target_branch = (self.target_branch or self.source_branch or "").strip()
        existing_mr = self._get_existing_mr_url(branch, target_branch=target_branch)
        if existing_mr:
            logger.info(f"MR already exists: {existing_mr}")
            return existing_mr
        if not target_branch:
            logger.error("Cannot create MR: no target branch on GitManager")
            return None

        # Issue target branch only (no silent fall back to main/develop)
        candidates = [target_branch]
        last_err = ""
        # Non-ASCII titles (e.g. Turkish ğüşıöç) are often corrupted by glab
        # on Windows console code pages. Prefer JSON REST (UTF-8) first.
        title_s = title if isinstance(title, str) else str(title or "")
        body_s = body if isinstance(body, str) else str(body or "")
        if any(ord(ch) > 127 for ch in title_s + body_s):
            for target in candidates:
                api_url = self._create_mr_via_api(title_s, body_s, branch, target)
                if api_url:
                    return api_url
        try:
            for target in candidates:
                cmd = [
                    "mr", "create",
                    "--title", title_s,
                    "--source-branch", branch,
                    "--target-branch", target,
                    "--yes",
                ]
                if body_s:
                    cmd.extend(["--description", body_s])
                result = self._run_glab(cmd)
                if result.returncode == 0:
                    output = result.stdout + result.stderr
                    for line in output.splitlines():
                        if "http" in line and (
                            "merge_request" in line
                            or "gitlab" in line
                            or "/-/merge" in line
                        ):
                            url = line.strip().split()[-1]
                            logger.info(f"Merge request created: {url} (target={target})")
                            return url
                    logger.info(f"Merge request created (target={target}).")
                    api_url = self._create_mr_via_api(
                        title_s, body_s, branch, target
                    )
                    if api_url:
                        return api_url
                    existing = self._get_existing_mr_url(
                        branch, target_branch=target
                    )
                    if existing:
                        return existing
                    logger.error(
                        "glab reported success but no MR URL was parsed"
                    )
                    return None

                err = (result.stderr or "") + (result.stdout or "")
                last_err = err
                if "already exists" in err.lower() or "409" in err:
                    logger.info("MR already exists, getting URL...")
                    return self._get_existing_mr_url(branch)
                # Try next candidate when target branch is missing
                if "target_branch" in err.lower() or "does not exist" in err.lower():
                    logger.warning(f"MR target '{target}' unavailable; trying next fallback")
                    continue
                # Auth / generic failure — try API for this target before giving up
                logger.warning(
                    f"glab MR create failed for target={target}; trying REST API. "
                    f"Detail: {self._redact_secret_text(err)[:300]}"
                )
                api_url = self._create_mr_via_api(title_s, body_s, branch, target)
                if api_url:
                    return api_url
                if "target_branch" in err.lower() or "does not exist" in err.lower():
                    continue
                # keep trying other targets after API miss
                continue

            # Final API pass over candidates if glab missing entirely
            for target in candidates:
                api_url = self._create_mr_via_api(title_s, body_s, branch, target)
                if api_url:
                    return api_url

            logger.error(
                f"Merge request failed after glab+API fallbacks: "
                f"{self._redact_secret_text(last_err)}"
            )
            return None
        except FileNotFoundError:
            logger.warning("'glab' CLI not found; creating MR via GitLab REST API")
            for target in candidates:
                api_url = self._create_mr_via_api(title, body, branch, target)
                if api_url:
                    return api_url
            return None
        except Exception as e:
            logger.error(f"Merge request error: {e}")
            # Last chance API
            for target in candidates:
                api_url = self._create_mr_via_api(title, body, branch, target)
                if api_url:
                    return api_url
            return None

    def add_mr_comment(self, mr_url: str, comment: str) -> bool:
        """Add a comment to an existing GitLab merge request."""
        if not self.remote_enabled:
            logger.info("MR comments not available (no remote configured).")
            return False

        try:
            mr_id = mr_url.rstrip("/").split("/")[-1]

            if not mr_id.isdigit():
                logger.warning(f"Could not extract MR ID from URL: {mr_url}")
                return False

            result = self._run_glab(["mr", "note", mr_id, "-m", comment])

            if result.returncode == 0:
                logger.info(f"Comment added to MR #{mr_id}")
                return True
            else:
                logger.error(
                    f"Failed to add comment to MR: "
                    f"{self._redact_secret_text(result.stderr or '')}"
                )
                return False
        except FileNotFoundError:
            logger.error("'glab' CLI not found. Install with: brew install glab (mac) or apt install gitlab-cli (linux)")
            return False
        except Exception as e:
            logger.error(f"MR comment error: {e}")
            return False

    def get_mr_url(self) -> Optional[str]:
        """Get the URL of the current branch's merge request."""
        if not self.remote_enabled:
            return None
        try:
            branch = self.get_current_branch()
            return self._get_existing_mr_url(branch)
        except Exception as e:
            logger.error(f"Error getting MR URL: {e}")
            return None

    def get_working_directory(self) -> Optional[Path]:
        """Get the temp working directory path."""
        return self.temp_dir

    def cleanup(self, *, success: Optional[bool] = None) -> bool:
        """Clean up the temp directory according to temp_cleanup_policy.

        Policies:
        - never: keep directory
        - always: delete directory
        - on_success: delete only when success is True
        - age: delete this directory only if older than temp_cleanup_max_age_days
          (also call ``purge_stale_temp_dirs`` for a full base sweep)
        """
        self._unregister_live()
        if not self.temp_dir or not self.temp_dir.exists():
            return True

        try:
            resolved = self.temp_dir.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved in session_bound_workspace_paths():
            logger.info(
                f"Keeping session-bound temp directory: {self.temp_dir}"
            )
            return True

        policy = (settings.temp_cleanup_policy or "age").strip().lower()

        if policy == "never":
            logger.info(f"Keeping temp directory: {self.temp_dir}")
            return True

        if policy == "age":
            max_age = float(getattr(settings, "temp_cleanup_max_age_days", 1.0) or 1.0)
            try:
                mtime = self.temp_dir.stat().st_mtime
            except OSError as e:
                logger.warning(f"Could not stat temp dir {self.temp_dir}: {e}")
                return False
            age_days = (time.time() - mtime) / 86400.0
            if age_days < max_age:
                logger.info(
                    f"Cleanup policy age: keeping {self.temp_dir} "
                    f"(age {age_days:.2f}d < {max_age}d)"
                )
                return True
            should_delete = True
        else:
            should_delete = policy == "always" or (
                policy == "on_success" and success is True
            )

        if not should_delete:
            logger.info(
                f"Cleanup policy '{policy}' - temp directory preserved: {self.temp_dir}"
            )
            return True

        try:
            shutil.rmtree(self.temp_dir)
            logger.info(f"Removed temp directory: {self.temp_dir}")
            self.temp_dir = None
            return True
        except Exception as e:
            logger.error(f"Failed to remove temp directory {self.temp_dir}: {e}")
            return False


def session_bound_workspace_paths() -> Set[Path]:
    """Clone dirs referenced by OpenCode session binds (must survive purge)."""
    try:
        from src.state.session_bind_store import session_bind_store

        return set(session_bind_store.working_directories())
    except Exception:
        return set()


def purge_stale_temp_dirs(
    *,
    max_age_days: Optional[float] = None,
    base_dir: Optional[Path] = None,
    protect_paths: Optional[set] = None,
) -> int:
    """Delete temp clone directories older than ``max_age_days`` under the temp base.

    Returns the number of directories removed. Safe to call when the base is missing.
    Used on daemon start and periodically so ``age`` policy actually frees disk.
    ``protect_paths`` are live clone dirs that must not be removed.
    Session-bound workspaces are always protected.
    """
    age = max_age_days
    if age is None:
        age = float(getattr(settings, "temp_cleanup_max_age_days", 1.0) or 1.0)
    if age < 0:
        age = 0.0

    base = base_dir
    if base is None:
        base = (Path.cwd() / settings.temp_dir_base).resolve()
    if not base.exists() or not base.is_dir():
        return 0

    protected: set = set()
    for p in list(protect_paths or ()) + list(session_bound_workspace_paths()):
        try:
            protected.add(Path(p).resolve())
        except (OSError, TypeError):
            continue

    cutoff = time.time() - (age * 86400.0)
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError as e:
        logger.warning(f"Cannot list temp base {base}: {e}")
        return 0

    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if resolved in protected:
            logger.info(f"Skipping live temp directory: {entry}")
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
            logger.info(f"Purged stale temp directory (>{age}d): {entry}")
        except Exception as e:
            logger.warning(f"Failed to purge stale temp dir {entry}: {e}")
    if removed:
        logger.info(
            f"Stale temp purge removed {removed} "
            f"director{'y' if removed == 1 else 'ies'}"
        )
    return removed
