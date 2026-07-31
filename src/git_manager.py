"""Git manager for JIRA Virtual Developer.

Handles branch creation, commits, and git operations within temp working directories.
Each JIRA issue gets its own isolated temp folder cloned from remote.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional
import shutil
from src.config import settings, set_current_temp_dir
from src.logger import logger
import os
from datetime import datetime


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
        logger.debug(f"Settings - use_temp_working_dir: {settings.use_temp_working_dir}, "
                     f"project_gitlab_url: {'configured' if settings.project_gitlab_url else 'not configured'}")
        
        if issue_key:
            self._setup_temp_working_dir()
        
        if self.temp_dir:
            set_current_temp_dir(self.temp_dir)
            logger.info(f"Temp directory set: {self.temp_dir}")

    def _setup_temp_working_dir(self) -> None:
        """Setup temp working directory for this JIRA issue."""
        logger.info(f"Setting up temp working directory for issue: {self.issue_key}")
        
        if not settings.use_temp_working_dir:
            logger.error("Temp working directories are disabled in config")
            raise RuntimeError("Temp working directories are disabled in config")

        # Validate remote URL
        gitlab_url = settings.project_gitlab_url.strip()
        if not gitlab_url:
            logger.error("PROJECT_GITLAB_URL not configured")
            raise RuntimeError("PROJECT_GITLAB_URL not configured. Cannot create temp working directory.")

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
            logger.error(f"Clone failed: {result.stderr}")
            raise RuntimeError(f"Failed to clone repository: {result.stderr}")

        logger.info("Clone completed successfully")

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
        logger.debug(f"Running git command: git {' '.join(args)}")
        
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        
        if result.returncode != 0:
            logger.error(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
            if check:
                raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
        else:
            logger.debug(f"Git command succeeded: git {' '.join(args)}")
        
        return result

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
        """Format a commit message according to commitMsgFormat.md."""
        max_title_len = 72
        prefix = f"[{issue_key}] "
        available = max_title_len - len(prefix)

        short_summary = summary
        if len(summary) > available:
            short_summary = summary[:available - 3] + "..."

        lines = [f"{prefix}{short_summary}", ""]

        if description and description.strip():
            desc = description.strip()
            if len(desc) > 500:
                desc = desc[:500] + "..."
            lines.append(desc)
            lines.append("")

        lines.append(f"Closes: {issue_key}")

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
            self._run_git(["push", "-u", "origin", branch])
            logger.info(f"Pushed branch '{branch}' to origin.")
            return True
        except RuntimeError:
            logger.warning(f"Push failed, attempting to pull and merge...")
            try:
                self._run_git(["fetch", "origin", branch], check=False)
                self._run_git(["merge", f"origin/{branch}", "-m", f"Merge remote branch {branch}"], check=False)
                self._run_git(["push", "-u", "origin", branch])
                logger.info(f"Pushed branch '{branch}' after merge.")
                return True
            except RuntimeError as e2:
                logger.error(f"Push failed after merge attempt: {e2}")
                return False

    def _get_existing_mr_url(self, branch: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["glab", "mr", "list", "--source-branch", branch, "--json"],
                cwd=self.temp_dir,
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                import json
                mrs = json.loads(result.stdout)
                if mrs and len(mrs) > 0:
                    return mrs[0].get("web_url")
        except Exception:
            pass
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

        try:
            cmd = [
                "glab", "mr", "create",
                "--title", title,
                "--source-branch", branch,
                "--target-branch", target_branch
            ]
            if body:
                cmd.extend(["--description", body])
            result = subprocess.run(cmd, cwd=self.temp_dir, capture_output=True, text=True)
            if result.returncode == 0:
                output = result.stdout + result.stderr
                for line in output.splitlines():
                    if "gitlab.com" in line or "http" in line:
                        url = line.strip()
                        logger.info(f"Merge request created: {url}")
                        return url
                logger.info("Merge request created.")
                return "created"
            else:
                if "already exists" in result.stderr.lower() or "409" in result.stderr:
                    logger.info("MR already exists, getting URL...")
                    return self._get_existing_mr_url(branch)
                logger.error(f"Merge request failed: {result.stderr}")
                return None
        except FileNotFoundError:
            logger.error("'glab' CLI not found.")
            return None
        except Exception as e:
            logger.error(f"Merge request error: {e}")
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

            cmd = ["glab", "mr", "note", mr_id, "-m", comment]
            result = subprocess.run(cmd, cwd=self.temp_dir, capture_output=True, text=True)

            if result.returncode == 0:
                logger.info(f"Comment added to MR #{mr_id}")
                return True
            else:
                logger.error(f"Failed to add comment to MR: {result.stderr}")
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

            cmd = ["glab", "mr", "list", "--source-branch", branch, "--json"]
            result = subprocess.run(cmd, cwd=self.temp_dir, capture_output=True, text=True)

            if result.returncode == 0 and result.stdout.strip():
                import json
                mrs = json.loads(result.stdout)
                if mrs and len(mrs) > 0:
                    return mrs[0].get("web_url")

            return None
        except Exception as e:
            logger.error(f"Error getting MR URL: {e}")
            return None

    def get_working_directory(self) -> Optional[Path]:
        """Get the temp working directory path."""
        return self.temp_dir

    def cleanup(self) -> bool:
        """Clean up the temp directory if cleanup policy allows."""
        if not self.temp_dir or not self.temp_dir.exists():
            return True

        policy = settings.temp_cleanup_policy

        if policy == "never":
            logger.info(f"Keeping temp directory: {self.temp_dir}")
            return True

        logger.info(f"Cleanup policy '{policy}' - temp directory preserved: {self.temp_dir}")
        return True
