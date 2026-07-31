"""Git manager for JIRA Virtual Developer.

Handles branch creation, commits, and git operations within temp working directories.
Each JIRA issue gets its own isolated temp folder cloned from remote.
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


class GitManager:
    """Manages git operations in isolated temp directories per JIRA issue.

    Flow:
    1. Validate remote URL (PROJECT_GITLAB_URL)
    2. If valid: Create temp folder as {remote_name}_{jira_issue_id}_{timestamp}
    3. Clone remote repo into temp folder
    4. All git operations happen in temp folder
    5. Temp folder preserved after work (cleanup_policy = 'never')
    """

    def __init__(self, issue_key: Optional[str] = None):
        self.issue_key = issue_key
        self.temp_dir: Optional[Path] = None
        self.remote_enabled: bool = False
        self.remote_url: Optional[str] = None
        self.remote_name: str = "unknown"

        logger.info(f"Initializing GitManager for issue: {issue_key}")
        logger.debug(
            f"Settings - project_gitlab_url: "
            f"{'configured' if settings.project_gitlab_url else 'not configured'}"
        )
        
        if issue_key:
            self._setup_temp_working_dir()
        
        if self.temp_dir:
            set_current_temp_dir(self.temp_dir)
            logger.info(f"Temp directory set: {self.temp_dir}")

    def _setup_temp_working_dir(self) -> None:
        """Setup isolated temp clone for this JIRA issue (always required)."""
        logger.info(f"Setting up temp working directory for issue: {self.issue_key}")

        # Validate remote URL
        gitlab_url = settings.project_gitlab_url.strip()
        if not gitlab_url:
            logger.error("PROJECT_GITLAB_URL not configured")
            raise RuntimeError(
                "PROJECT_GITLAB_URL not configured. "
                "Temp working directories are mandatory; cannot run without a remote to clone."
            )

        self.remote_url = gitlab_url
        self.remote_name = self._extract_remote_name(gitlab_url)
        self.remote_enabled = True
        logger.info(f"Remote configured - name: {self.remote_name}, enabled: {self.remote_enabled}")

        # Create temp directory
        self.temp_dir = self._create_temp_directory()
        logger.info(f"Temp working directory created: {self.temp_dir}")

        # Clone remote into temp directory
        logger.info(f"Starting repository clone...")
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

        folder_name = f"{self.remote_name}_{self.issue_key}_{timestamp}"

        base_temp = Path.cwd() / settings.temp_dir_base
        base_temp.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Base temp directory: {base_temp}")

        temp_path = base_temp / folder_name

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
        
        gitlab_pat = settings.gitlab_pat.strip()
        clone_url = self._build_clone_url(self.remote_url or "", gitlab_pat)
        
        logger.info(f"Cloning repository into {self.temp_dir}...")
        logger.debug(f"Remote URL: {self.remote_url}")
        logger.debug(f"Clone URL has PAT: {bool(gitlab_pat)}")

        result = subprocess.run(
            ["git", "clone", "--no-single-branch", clone_url, str(self.temp_dir)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Never surface PAT-bearing URLs from git stderr to callers/logs
            safe_err = (result.stderr or "").replace(gitlab_pat, "***") if gitlab_pat else result.stderr
            logger.error(f"Clone failed: {safe_err}")
            raise RuntimeError(f"Failed to clone repository: {safe_err}")

        logger.info("Clone completed successfully")
        # Scrub credentials from origin so PAT is not left on disk in .git/config
        self._scrub_remote_credentials()

        self._sync_remote_branches()

    def _build_clone_url(self, base_url: str, pat: str) -> str:
        """Build HTTPS clone URL with PAT for authentication."""
        if pat:
            if base_url.startswith("https://"):
                logger.debug("Building HTTPS clone URL with PAT")
                return base_url.replace("https://", f"https://oauth2:{pat}@")
            if base_url.startswith("http://"):
                logger.debug("Building HTTP clone URL with PAT")
                return base_url.replace("http://", f"http://oauth2:{pat}@")
        logger.debug("Building clone URL without PAT")
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
        """Temporarily re-embed PAT in origin for push/fetch operations."""
        if not self.remote_url:
            return
        pat = (settings.gitlab_pat or "").strip()
        if not pat:
            return
        auth_url = self._build_clone_url(self.remote_url, pat)
        self._run_git(["remote", "set-url", "origin", auth_url], check=False)

    def _sync_remote_branches(self) -> None:
        """Sync all remote branches locally."""
        logger.info("Syncing remote branches...")
        try:
            logger.debug("Running git fetch --all")
            self._run_git(["fetch", "--all"], check=False)

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

    def _run_git(self, args: list, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the temp working directory."""
        cwd = cwd or self.temp_dir

        if cwd is None or not cwd.exists() or not cwd.is_dir():
            logger.error(f"Git operations locked: temp directory '{cwd}' does not exist")
            raise RuntimeError(f"Git operations locked: temp directory '{cwd}' does not exist")

        cmd = ["git"] + args
        safe_args = self._redact_git_args(args)
        logger.debug(f"Running git command: git {' '.join(safe_args)}")
        
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
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
        pat = (settings.gitlab_pat or "").strip()
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

    def _checkout_or_create_branch(self, branch_name: str) -> str:
        logger.info(f"Checking out or creating branch: {branch_name}")
        self._delete_local_branch(branch_name)
        
        logger.info("Fetching all remote refs...")
        self._run_git(["fetch", "origin", "--prune"], check=False)
        
        result = self._run_git(["ls-remote", "--heads", "origin", branch_name], check=False)
        remote_exists = branch_name in result.stdout
        
        logger.info(f"Remote branch '{branch_name}' exists: {remote_exists}")
        
        if remote_exists:
            logger.info(f"Checking out origin/{branch_name}...")
            self._run_git(["checkout", "-b", branch_name, f"origin/{branch_name}"])
            logger.info(f"Checked out remote branch: {branch_name}")
        else:
            logger.info("No remote branch found, creating new from default...")
            if not self._checkout_default_branch():
                raise RuntimeError("No default branch available to create new branch from")
            self._run_git(["checkout", "-b", branch_name])
            logger.info(f"Created new branch: {branch_name}")
        
        return branch_name

    def _checkout_default_branch(self) -> bool:
        """Checkout the default branch before creating a feature branch."""
        default_branch = settings.default_branch.strip() if settings.default_branch else ""

        branches_to_try = []
        if default_branch:
            branches_to_try.append(default_branch)
        branches_to_try.append("main")

        for branch in branches_to_try:
            if self._branch_exists(branch):
                self._run_git(["checkout", branch])
                logger.info(f"Checked out default branch: {branch}")
                return True

        logger.error(f"No default branch found. Tried: {branches_to_try}")
        return False

    def ensure_feature_branch(self, issue_key: Optional[str] = None) -> Optional[str]:
        """Create or checkout a feature branch for the given JIRA issue."""
        safe_key = re.sub(r'[^A-Za-z0-9\-]', '-', issue_key or self.issue_key or "issue")
        branch_name = f"feature/{safe_key}"

        return self._checkout_or_create_branch(branch_name)

    def _format_commit_message(self, issue_key: str, summary: str, description: str = "") -> str:
        """Format a commit message per agent/rules/EXECUTION.md.

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

    def status(self) -> str:
        """Get git status output."""
        result = self._run_git(["status"])
        return result.stdout

    def push(self, branch_name: Optional[str] = None) -> bool:
        if not self.remote_enabled:
            logger.info("Push not available (no remote configured).")
            return False

        branch = branch_name or self.get_current_branch()

        try:
            self._with_auth_remote()
            try:
                self._run_git(["push", "-u", "origin", branch])
                logger.info(f"Pushed branch '{branch}' to origin.")
                return True
            except RuntimeError:
                logger.warning(f"Push failed, attempting to pull and merge...")
                try:
                    self._run_git(["fetch", "origin", branch], check=False)
                    self._run_git(
                        ["merge", f"origin/{branch}", "-m", f"Merge remote branch {branch}"],
                        check=False,
                    )
                    self._run_git(["push", "-u", "origin", branch])
                    logger.info(f"Pushed branch '{branch}' after merge.")
                    return True
                except RuntimeError as e2:
                    logger.error(f"Push failed after merge attempt: {e2}")
                    return False
        finally:
            self._scrub_remote_credentials()

    def _gitlab_host_and_project(self) -> tuple[str, str]:
        """Return (api_host, path_with_namespace) from PROJECT_GITLAB_URL."""
        raw = self.remote_url or getattr(settings, "project_gitlab_url", "") or ""
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
        """Env for glab subprocesses: inject GITLAB_TOKEN from .env settings."""
        env = dict(os.environ)
        pat = (settings.gitlab_pat or "").strip()
        host, _ = self._gitlab_host_and_project()
        if pat:
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
        """Create MR via GitLab REST API using GITLAB_PAT (fallback if glab fails)."""
        pat = (settings.gitlab_pat or "").strip()
        if not pat:
            logger.error("Cannot create MR via API: GITLAB_PAT is empty")
            return None
        host, project = self._gitlab_host_and_project()
        if not project:
            logger.error("Cannot create MR via API: project path missing from PROJECT_GITLAB_URL")
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
            with httpx.Client(timeout=30.0, verify=True) as client:
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
        pat = (settings.gitlab_pat or "").strip()
        if not pat:
            return None
        host, project = self._gitlab_host_and_project()
        if not project:
            return None
        try:
            import httpx

            enc = quote(project, safe="")
            url = (
                f"https://{host}/api/v4/projects/{enc}/merge_requests"
                f"?source_branch={quote(branch)}&state=opened"
            )
            with httpx.Client(timeout=30.0) as client:
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

        branch = self.get_current_branch()
        if branch in ("main", "master"):
            logger.warning("Cannot create MR from main/master branch.")
            return None

        existing_mr = self._get_existing_mr_url(branch)
        if existing_mr:
            logger.info(f"MR already exists: {existing_mr}")
            return existing_mr

        if not target_branch:
            target_branch = settings.default_branch.strip() if settings.default_branch else "main"

        # Prefer configured default, then common fallbacks when remote lacks the branch
        candidates = []
        for b in (target_branch, "main", "master", "develop"):
            if b and b not in candidates:
                candidates.append(b)

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
