"""Git manager for JIRA Virtual Developer.

Handles branch creation, commits, and git operations within temp working directories.
Each JIRA issue gets its own isolated temp folder cloned from the repository URL
declared on the issue (see ``src.issue_git_spec``).
"""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

from src.config import settings, set_current_temp_dir
from src.logger import logger


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


# Primary integration bases — never used as the agent work branch
_PRIMARY_BASES = frozenset({"main", "master", "develop", "trunk", "dev"})


class GitManager:
    """Manages git operations in isolated temp directories per JIRA issue.

    Flow (GitLab MR: source → target):
    1. Repository URL + source/target from the Jira ``{params}`` block
    2. Temp folder ``{remote_name}_{jira_issue_id}_{timestamp}``
    3. Clone remote repo
    4. **Require** ``origin/{target}`` exists
    5. Resolve work branch: params **Source** (unless Source is a primary base
       / equals target → then ``feature/{KEY}``)
    6. If ``origin/{source}`` exists → checkout that tip; else create work
       branch **from** ``origin/{target}``
    7. Agent works on that branch; push + MR **source → target**
       (commit subjects always use the Jira issue key, not the branch name)
    """

    def __init__(
        self,
        issue_key: Optional[str] = None,
        *,
        remote_url: Optional[str] = None,
        source_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
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
        # Actual branch checked out for agent work (set by ensure_feature_branch)
        self.work_branch: Optional[str] = None

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
        logger.info(f"Setting up temp working directory for issue: {self.issue_key}")

        gitlab_url = (self.remote_url or "").strip()
        if not gitlab_url:
            raise GitCloneError(
                "*Virtual Developer* could not clone: no repository URL was provided on the issue.\n\n"
                "Add `Repository: https://gitlab.example.com/group/repo.git` to the description."
            )
        if not self.target_branch:
            raise GitTargetBranchError(
                "*Virtual Developer* could not prepare the workspace: no target branch on the issue.\n\n"
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

        self.temp_dir = self._create_temp_directory()
        logger.info(f"Temp working directory created: {self.temp_dir}")

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

    def _create_temp_directory(self) -> Path:
        """Create temp directory with format: {remote_name}_{jira_issue_id}_{timestamp}"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        def _safe(token: str) -> str:
            cleaned = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in (token or "unknown")
            )
            cleaned = cleaned.strip("._-") or "unknown"
            return cleaned[:80]

        folder_name = f"{_safe(self.remote_name)}_{_safe(self.issue_key)}_{timestamp}"

        base_temp = (Path.cwd() / settings.temp_dir_base).resolve()
        base_temp.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Base temp directory: {base_temp}")

        temp_path = base_temp / folder_name
        # Ensure path stays under base_temp (no traversal via issue_key)
        try:
            temp_path.resolve().relative_to(base_temp)
        except ValueError as e:
            raise RuntimeError(f"Unsafe temp path rejected: {temp_path}") from e

        counter = 1
        original_path = temp_path
        while temp_path.exists():
            temp_path = Path(str(original_path) + f"_{counter}")
            counter += 1
            logger.debug(f"Path already exists, trying: {temp_path}")

        temp_path.mkdir(parents=True)
        logger.info(f"Created temp directory: {temp_path}")
        return temp_path

    def _clone_into_temp(self) -> None:
        """Clone the remote repository into the temp directory."""
        if not self.temp_dir:
            raise RuntimeError("Temp directory not initialized")

        # Fail closed: never inject PAT for untrusted/unknown hosts
        self._assert_remote_host_allowed(self.remote_url or "")

        gitlab_pat = self._pat_for_remote(self.remote_url or "")
        # Clean URL only — PAT never appears in argv (use GIT_ASKPASS instead)
        clone_url = (self.remote_url or "").strip()

        logger.info(f"Cloning repository into {self.temp_dir}...")
        logger.debug(f"Remote URL: {self.remote_url}")
        logger.debug(f"Clone will use askpass auth: {bool(gitlab_pat)}")

        clone_timeout = max(30, int(getattr(settings, "git_clone_timeout_seconds", 300) or 300))
        try:
            result = subprocess.run(
                ["git", "clone", "--no-single-branch", clone_url, str(self.temp_dir)],
                capture_output=True,
                text=True,
                env=self._git_auth_env() if gitlab_pat else None,
                timeout=clone_timeout,
            )
        except subprocess.TimeoutExpired as e:
            safe_err = f"git clone timed out after {clone_timeout}s"
            logger.error(safe_err)
            raise GitCloneError(
                (
                    f"*Virtual Developer* could not **clone** the repository "
                    f"(timed out after {clone_timeout}s).\n\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`\n\n"
                    "Check network access to GitLab, then move the issue back to *To Do*."
                ),
                technical=str(e),
            ) from e

        if result.returncode != 0:
            # Never surface PAT-bearing URLs from git stderr to callers/logs
            safe_err = (result.stderr or "").replace(gitlab_pat, "***") if gitlab_pat else (
                result.stderr or ""
            )
            safe_err = self._redact_secret_text(safe_err)
            logger.error(f"Clone failed: {safe_err}")
            repo_display = self.remote_url or "(unknown)"
            raise GitCloneError(
                (
                    f"*Virtual Developer* could not **clone** the repository.\n\n"
                    f"*Repository:* `{repo_display}`\n"
                    f"*Detail:* {safe_err.strip()[:800] or 'git clone failed'}\n\n"
                    "Check that the URL is correct, the project is reachable, "
                    "and a GitLab PAT is configured for this host in dashboard "
                    "Settings (or `GITLAB_HOST_PATS`). Then move the issue back to *To Do*."
                ),
                technical=safe_err,
            )

        logger.info("Clone completed successfully")
        # Ensure origin has no embedded credentials
        self._scrub_remote_credentials()

        self._sync_remote_branches()

    @staticmethod
    def _host_from_url(url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if not raw.startswith("http"):
            raw = "https://" + raw
        return (urlparse(raw).hostname or "").lower()

    def _pat_for_remote(self, url: str = "") -> str:
        """Resolve GitLab PAT for this remote URL (per-host map)."""
        host = self._host_from_url(url or self.remote_url or "")
        if not host:
            return ""
        if hasattr(settings, "gitlab_pat_for_host"):
            return (settings.gitlab_pat_for_host(host) or "").strip()
        # Legacy fallback
        return (settings.gitlab_pat or "").strip()

    def _assert_remote_host_allowed(self, url: str) -> None:
        """Refuse to use a PAT against hosts without a configured credential.

        When no PATs are configured at all, any host is allowed (public clone).
        When any host PAT exists, the repository host must match one of them.
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
                "*Virtual Developer* could not clone: repository URL has no host.\n\n"
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
                "*Virtual Developer* refused to authenticate: "
                "no GitLab host→PAT mapping is configured while a PAT is set.\n\n"
                "Add hosts in dashboard Settings (GitLab credentials), set "
                "`GITLAB_HOST_PATS={\"gitlab.example.com\":\"glpat-…\"}`, "
                "or set `GITLAB_ALLOWED_HOSTS` with legacy `GITLAB_PAT`."
            )
        raise GitCloneError(
            (
                f"*Virtual Developer* refused to send credentials to host "
                f"`{host}`.\n\n"
                f"Configured hosts: `{', '.join(allowed) or '(none)'}`.\n"
                "Add this host with a PAT in dashboard Settings, or update the "
                "issue Repository URL."
            )
        )

    def _git_auth_env(self, *, url: str = "") -> Dict[str, str]:
        """Build env for git so credentials come from askpass, not argv.

        PAT is chosen for the remote host and placed in a process environment
        variable consumed by a short askpass helper — never in argv.
        """
        env = dict(os.environ)
        pat = self._pat_for_remote(url or self.remote_url or "")
        if not pat:
            return env
        askpass = self._ensure_askpass_script()
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "never"
        # Username for HTTPS; password comes from VD_GIT_PASSWORD via askpass
        env["VD_GIT_PASSWORD"] = pat
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "credential.helper"
        env["GIT_CONFIG_VALUE_0"] = ""
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

    def _sync_remote_branches(self) -> None:
        """Sync all remote branches locally."""
        logger.info("Syncing remote branches...")
        try:
            logger.debug("Running git fetch --all")
            self._with_auth_remote()
            try:
                self._run_git(["fetch", "--all"], check=False, auth=True)
            finally:
                self._scrub_remote_credentials()

            result = self._run_git(["branch", "-r"], check=False)

            if result.returncode == 0:
                remote_branches = [
                    b.strip().replace("origin/", "")
                    for b in result.stdout.splitlines()
                    if b.strip() and "HEAD" not in b
                ]

                logger.debug(f"Found {len(remote_branches)} remote branches: {remote_branches}")

                for remote_branch in remote_branches:
                    local_exists = self._run_git(
                        ["rev-parse", "--verify", f"refs/heads/{remote_branch}"],
                        check=False
                    ).returncode == 0

                    if not local_exists:
                        logger.debug(f"Creating local tracking branch: {remote_branch}")
                        self._run_git(
                            ["branch", "--track", remote_branch, f"origin/{remote_branch}"],
                            check=False
                        )

                logger.info(f"Synced {len(remote_branches)} remote branches")
            else:
                logger.warning(f"Failed to list remote branches: {result.stderr}")
        except Exception as e:
            logger.warning(f"Could not sync remote branches: {e}")

    def _run_git(
        self,
        args: list,
        cwd: Optional[Path] = None,
        check: bool = True,
        *,
        auth: bool = False,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the temp working directory.

        When ``auth=True``, inject askpass env so push/fetch can authenticate
        without putting the PAT on the command line.
        """
        cwd = cwd or self.temp_dir

        if cwd is None or not cwd.exists() or not cwd.is_dir():
            logger.error(f"Git operations locked: temp directory '{cwd}' does not exist")
            raise RuntimeError(f"Git operations locked: temp directory '{cwd}' does not exist")

        cmd = ["git"] + args
        safe_args = self._redact_git_args(args)
        logger.debug(f"Running git command: git {' '.join(safe_args)}")

        use_auth = auth and bool(self._pat_for_remote(self.remote_url or ""))
        env = self._git_auth_env() if use_auth else None
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
        )
        
        if result.returncode != 0:
            safe_err = self._redact_secret_text(result.stderr or "")
            logger.error(f"Git command failed: git {' '.join(safe_args)}\n{safe_err}")
            if check:
                raise RuntimeError(f"Git command failed: git {' '.join(safe_args)}\n{safe_err}")
        else:
            logger.debug(f"Git command succeeded: git {' '.join(safe_args)}")
        
        return result

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

    def _remote_head_exists(self, branch: str) -> bool:
        """True if origin has refs/heads/{branch} (uses ls-remote)."""
        branch = (branch or "").strip()
        if not branch:
            return False
        self._with_auth_remote()
        try:
            result = self._run_git(
                ["ls-remote", "--heads", "origin", branch], check=False, auth=True
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
        safe_key = re.sub(
            r"[^A-Za-z0-9\-]", "-", issue_key or self.issue_key or "issue"
        )
        feature = f"feature/{safe_key}"
        source = (self.source_branch or "").strip()
        target = (self.target_branch or "").strip()

        if source and source != target and not self._is_primary_base(source):
            logger.info(
                f"Using params source as work branch: {source} "
                f"(MR will be {source} → {target or '(target)'}; "
                f"issue key for commits is {issue_key or self.issue_key})"
            )
            return source

        logger.info(
            f"Using isolated work branch {feature} "
            f"(params source was '{source or '(none)'}'; MR → {target or '(target)'})"
        )
        return feature

    def _require_target_on_remote(self) -> str:
        """Target must exist on origin before any work. Returns target name."""
        target = (self.target_branch or "").strip()
        if not target:
            raise GitTargetBranchError(
                "*Virtual Developer* could not start: no **target branch** on the issue.\n\n"
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
                    f"*Virtual Developer* could not start: **target branch** missing on GitLab.\n\n"
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

    def _checkout_work_branch_from_target(self, work_branch: str, target: str) -> str:
        """Create or reset *work_branch* from origin/target tip, then checkout it.

        Used when the source branch does **not** exist on the remote yet.
        Does not push (push happens after agent work).
        """
        work_branch = (work_branch or "").strip()
        target = (target or "").strip()
        if not work_branch:
            raise GitSourceBranchError(
                "*Virtual Developer* could not create a work branch: empty name."
            )
        if work_branch == target:
            raise GitSourceBranchError(
                (
                    f"*Virtual Developer* refused to use target `{target}` as the work branch.\n\n"
                    "Source and target resolved to the same name. Set "
                    "`Source branch: feature/YOUR-KEY` or leave source as a primary "
                    "base so the agent uses `feature/{KEY}`."
                )
            )

        logger.info(
            f"Source branch '{work_branch}' not on remote — creating from "
            f"origin/{target} (MR will be {work_branch} → {target})"
        )
        self._delete_local_branch(work_branch)

        start_point = f"origin/{target}"
        if not self._branch_exists(target, check_remote=True):
            # Fallback after fetch failure edge cases
            if self._branch_exists(target, check_remote=False):
                start_point = target
            else:
                raise GitTargetBranchError(
                    (
                        f"*Virtual Developer* could not base work on target `{target}`: "
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
                "*Virtual Developer* could not checkout work branch: empty name."
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
        if not self._branch_exists(work_branch, check_remote=True):
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
        if not self._branch_exists(work_branch, check_remote=True):
            raise GitSourceBranchError(
                (
                    f"*Virtual Developer* could not checkout source branch `{work_branch}`: "
                    f"`origin/{work_branch}` missing after fetch even though ls-remote "
                    f"reported it.\n\n"
                    f"*Repository:* `{self.remote_url or '(unknown)'}`"
                )
            )

        self._delete_local_branch(work_branch)
        self._run_git(["checkout", "-B", work_branch, start_point])
        logger.info(
            f"Checked out existing work branch '{work_branch}' from '{start_point}'"
        )
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
                "*Virtual Developer* could not prepare a work branch: empty name."
            )
        if work_branch == target:
            raise GitSourceBranchError(
                (
                    f"*Virtual Developer* refused to use target `{target}` as the work branch.\n\n"
                    "Source and target resolved to the same name. Set a dedicated "
                    "`Source branch` (or a primary base so the agent uses "
                    "`feature/{KEY}`)."
                )
            )

        # 1) Prefer existing remote source tip
        if self._remote_head_exists(work_branch):
            return self._checkout_existing_remote_branch(work_branch)

        # 2) After target fetch, origin/{work} may already be present
        if self._branch_exists(work_branch, check_remote=True):
            return self._checkout_existing_remote_branch(work_branch)

        # 3) Missing on remote → create from target
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
        return self._prepare_work_branch(work, target)
    def _format_commit_message(self, issue_key: str, summary: str, description: str = "") -> str:
        """Format a commit message per agent/AGENT_PROMPT.md §policy.commit.

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

    def push(self, branch_name: Optional[str] = None) -> bool:
        if not self.remote_enabled:
            logger.info("Push not available (no remote configured).")
            return False

        # Prefer prepared work_branch over drifted HEAD (B5)
        branch = (
            (branch_name or "").strip()
            or (self.work_branch or "").strip()
            or self.get_current_branch()
        )

        try:
            self._with_auth_remote()
            try:
                self._run_git(["push", "-u", "origin", branch], auth=True)
                logger.info(f"Pushed branch '{branch}' to origin.")
                return True
            except RuntimeError:
                logger.warning(f"Push failed, attempting to pull and merge...")
                try:
                    self._run_git(["fetch", "origin", branch], check=False, auth=True)
                    self._run_git(
                        ["merge", f"origin/{branch}", "-m", f"Merge remote branch {branch}"],
                        check=False,
                    )
                    self._run_git(["push", "-u", "origin", branch], auth=True)
                    logger.info(f"Pushed branch '{branch}' after merge.")
                    return True
                except RuntimeError as e2:
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
        host = parsed.hostname or "gitlab.com"
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
        return env

    def _run_glab(self, args: List[str], *, check: bool = False) -> subprocess.CompletedProcess:
        """Run glab with auth from settings (never log the token)."""
        cmd = ["glab", *args]
        logger.debug(f"Running glab: glab {' '.join(args)}")
        return subprocess.run(
            cmd,
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
            env=self._glab_env(),
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
                "Content-Type": "application/json",
            }
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

    def _get_existing_mr_url(self, branch: str) -> Optional[str]:
        # 1) glab with token env
        try:
            result = self._run_glab(
                ["mr", "list", "--source-branch", branch, "--json"]
            )
            if result.returncode == 0 and result.stdout.strip():
                mrs = json.loads(result.stdout)
                if mrs and len(mrs) > 0:
                    return mrs[0].get("web_url") or mrs[0].get("url")
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
            url = (
                f"https://{host}/api/v4/projects/{enc}/merge_requests"
                f"?source_branch={quote(branch)}&state=opened"
            )
            with httpx.Client(timeout=30.0, verify=False) as client:
                resp = client.get(url, headers={"PRIVATE-TOKEN": pat})
                if resp.status_code == 200:
                    mrs = resp.json()
                    if mrs:
                        return mrs[0].get("web_url")
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

        existing_mr = self._get_existing_mr_url(branch)
        if existing_mr:
            logger.info(f"MR already exists: {existing_mr}")
            return existing_mr

        if not target_branch:
            target_branch = (self.target_branch or self.source_branch or "").strip()
        if not target_branch:
            logger.error("Cannot create MR: no target branch on GitManager")
            return None

        # Issue target branch only (no silent fall back to main/develop)
        candidates = [target_branch]
        last_err = ""
        try:
            for target in candidates:
                cmd = [
                    "mr", "create",
                    "--title", title,
                    "--source-branch", branch,
                    "--target-branch", target,
                    "--yes",
                ]
                if body:
                    cmd.extend(["--description", body])
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
                    return "created"

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
                api_url = self._create_mr_via_api(title, body, branch, target)
                if api_url:
                    return api_url
                if "target_branch" in err.lower() or "does not exist" in err.lower():
                    continue
                # keep trying other targets after API miss
                continue

            # Final API pass over candidates if glab missing entirely
            for target in candidates:
                api_url = self._create_mr_via_api(title, body, branch, target)
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
        """
        if not self.temp_dir or not self.temp_dir.exists():
            return True

        policy = (settings.temp_cleanup_policy or "never").strip().lower()

        if policy == "never":
            logger.info(f"Keeping temp directory: {self.temp_dir}")
            return True

        should_delete = policy == "always" or (policy == "on_success" and success is True)
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
