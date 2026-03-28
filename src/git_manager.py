"""Git manager for JIRA Virtual Developer.

Handles branch creation, commits, and git operations within the PROJECT_ROOT.
"""

import re
import subprocess
from pathlib import Path
from typing import Optional

from src.config import settings


class GitManager:
    """Manages git operations within the project root directory.
    
    All operations are scoped to settings.project_root (e.g., sample_project).
    
    If PROJECT_GITLAB_URL is set in .env:
      - Clones the repo into PROJECT_ROOT (if not already present)
      - Enables push() and create_merge_request() features
    If not set:
      - Uses existing PROJECT_ROOT folder (no clone)
      - Only local commit/branch operations available
    """
    
    def __init__(self, project_root: Optional[Path] = None):
        self.project_root = project_root or settings.project_root
        self.remote_enabled: bool = False
        self._ensure_git_repo()
        self._ensure_remote_repo()
    
    def _ensure_git_repo(self) -> None:
        """Ensure the project root is a git repository.
        
        Does NOT create the directory if it doesn't exist — only works in an
        existing target project folder.
        """
        if not self.project_root.exists() or not self.project_root.is_dir():
            print(f"[GitManager] LOCKED: Target project folder '{self.project_root}' does not exist.")
            return
        
        if not (self.project_root / ".git").exists():
            # Initialize git repo if not present (folder must already exist)
            self._run_git(["init"], cwd=self.project_root)
            # Set default branch to main
            self._run_git(["checkout", "-b", "main"], cwd=self.project_root, check=False)
            # Create initial commit if needed
            if not self._has_commits():
                self._run_git(["add", "."], cwd=self.project_root, check=False)
                self._run_git(["commit", "-m", "Initial commit"], cwd=self.project_root, check=False)
    
    def _ensure_remote_repo(self) -> None:
        """Clone remote repo if PROJECT_GITLAB_URL is set.
        
        Configures remote with GITLAB_PAT for push/auth.
        Sets self.remote_enabled = True if remote is available.
        
        Does NOT create the directory — only works if target project folder exists.
        """
        # Guard: must have an existing target folder
        if not self.project_root.exists() or not self.project_root.is_dir():
            print(f"[GitManager] LOCKED: Cannot configure remote — target folder '{self.project_root}' does not exist.")
            self.remote_enabled = False
            return
        
        gitlab_url = settings.project_gitlab_url.strip()
        gitlab_pat = settings.gitlab_pat.strip()
        
        if not gitlab_url:
            self.remote_enabled = False
            return
        
        # If project_root already has .git with a remote, assume it's already cloned
        has_remote = False
        try:
            result = self._run_git(["remote", "get-url", "origin"], check=False)
            has_remote = result.returncode == 0 and result.stdout.strip()
        except Exception:
            pass
        
        if not has_remote:
            # Need to clone
            # First, backup existing content if any (not .git)
            # For simplicity, if project_root has non-git files, we assume user wants to keep them
            # So we only clone if project_root is empty or only has .git
            
            files = list(self.project_root.iterdir())
            non_git_files = [f for f in files if f.name != ".git"]
            
            if non_git_files:
                # Folder has existing content — don't overwrite
                print(f"[GitManager] PROJECT_ROOT has existing content, skipping clone of {gitlab_url}")
                # Still try to set remote for push if not present
                self._configure_remote_with_pat(gitlab_url, gitlab_pat)
            else:
                # Folder is empty or only .git — clone into it
                print(f"[GitManager] Cloning {gitlab_url} into {self.project_root}...")
                # Clone to temp, then move contents
                import tempfile
                import shutil
                with tempfile.TemporaryDirectory() as tmpdir:
                    clone_url = self._build_clone_url(gitlab_url, gitlab_pat)
                    clone_result = subprocess.run(
                        ["git", "clone", clone_url, tmpdir],
                        capture_output=True, text=True
                    )
                    if clone_result.returncode != 0:
                        print(f"[GitManager] Clone failed: {clone_result.stderr}")
                        self.remote_enabled = False
                        return
                    # Move cloned contents to project_root
                    for item in Path(tmpdir).iterdir():
                        dest = self.project_root / item.name
                        if dest.exists():
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        shutil.move(str(item), str(dest))
                print("[GitManager] Clone complete.")
        
        # Configure remote with PAT
        self._configure_remote_with_pat(gitlab_url, gitlab_pat)
        self.remote_enabled = True
        print("[GitManager] Remote features enabled (push, merge-request).")
    
    def _build_clone_url(self, base_url: str, pat: str) -> str:
        """Build HTTPS clone URL with PAT for authentication."""
        # base_url like https://gitlab.com/group/repo.git
        # Result: https://oauth2:{PAT}@gitlab.com/group/repo.git
        if pat:
            # Remove https:// prefix, add with PAT
            if base_url.startswith("https://"):
                return base_url.replace("https://", f"https://oauth2:{pat}@")
            if base_url.startswith("http://"):
                return base_url.replace("http://", f"http://oauth2:{pat}@")
        return base_url
    
    def _configure_remote_with_pat(self, gitlab_url: str, pat: str) -> None:
        """Set git remote origin URL with PAT for push."""
        try:
            url = self._build_clone_url(gitlab_url, pat)
            self._run_git(["remote", "set-url", "origin", url], check=False)
        except Exception:
            pass
    
    def _has_commits(self) -> bool:
        """Check if the repo has any commits."""
        result = self._run_git(["rev-parse", "--verify", "HEAD"], cwd=self.project_root, check=False)
        return result.returncode == 0
    
    def _run_git(self, args: list, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command ONLY in the target project folder.
        
        No fallback to cwd. If project_root does not exist or is not accessible,
        prints a lock message and raises an error.
        """
        cwd = cwd or self.project_root
        
        # Guard: must only run in the target project folder
        if cwd is None or not cwd.exists() or not cwd.is_dir():
            print("[GitManager] LOCKED: Target project folder does not exist or is not accessible.")
            raise RuntimeError(f"Git operations locked: target directory '{cwd}' does not exist or is not accessible.")
        
        cmd = ["git"] + args
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
        return result
    
    def _branch_exists(self, branch_name: str) -> bool:
        """Check if a branch exists (local)."""
        result = self._run_git(["rev-parse", "--verify", f"refs/heads/{branch_name}"], check=False)
        return result.returncode == 0
    
    def _get_unique_branch_name(self, base_name: str) -> str:
        """Find a unique branch name, appending -v2, -v3, etc. if needed.
        
        Args:
            base_name: e.g., "feature/PROJ-123"
        
        Returns:
            A unique branch name (e.g., "feature/PROJ-123" or "feature/PROJ-123-v2")
        """
        if not self._branch_exists(base_name):
            return base_name
        
        # Try v2, v3, ...
        for i in range(2, 1000):
            candidate = f"{base_name}-v{i}"
            if not self._branch_exists(candidate):
                return candidate
        
        raise RuntimeError(f"Could not find unique branch name for {base_name}")
    
    def ensure_feature_branch(self, issue_key: str) -> str:
        """Create or checkout a feature branch for the given JIRA issue.
        
        Branch naming: feature/{ISSUE_KEY}
        If exists, tries feature/{ISSUE_KEY}-v2, -v3, etc.
        
        Args:
            issue_key: JIRA issue identifier (e.g., "PROJ-123")
        
        Returns:
            The branch name that was checked out (e.g., "feature/PROJ-123")
        """
        # Sanitize issue key for branch name (remove any special chars)
        safe_key = re.sub(r'[^A-Za-z0-9\-]', '-', issue_key)
        base_branch = f"feature/{safe_key}"
        
        branch_name = self._get_unique_branch_name(base_branch)
        
        # Checkout the branch (create if new, switch if exists)
        if self._branch_exists(branch_name):
            # Switch to existing branch
            self._run_git(["checkout", branch_name], cwd=self.project_root)
        else:
            # Create and switch to new branch
            self._run_git(["checkout", "-b", branch_name], cwd=self.project_root)
        
        print(f"[GitManager] Working on branch: {branch_name}")
        return branch_name
    
    def _format_commit_message(self, issue_key: str, summary: str, description: str = "") -> str:
        """Format a commit message according to commitMsgFormat.md.
        
        Format:
            [{JIRA_ISSUE_ID}] {Short description}
            
            {Detailed description}
            
            Closes: {JIRA_ISSUE_ID}
        """
        # Shorten summary if too long for title line (max ~72 chars total)
        max_title_len = 72
        prefix = f"[{issue_key}] "
        available = max_title_len - len(prefix)
        
        short_summary = summary
        if len(summary) > available:
            short_summary = summary[:available - 3] + "..."
        
        lines = [f"{prefix}{short_summary}", ""]
        
        if description and description.strip():
            # Add description, truncated reasonably
            desc = description.strip()
            if len(desc) > 500:
                desc = desc[:500] + "..."
            lines.append(desc)
            lines.append("")
        
        lines.append(f"Closes: {issue_key}")
        
        return "\n".join(lines)
    
    def _configure_git_identity(self) -> None:
        """Configure git user identity locally in the project root.
        
        This sets user.name and user.email for the repository only (not global),
        using values from settings.git_user_name and settings.git_user_email.
        """
        try:
            self._run_git(["config", "user.name", settings.git_user_name], cwd=self.project_root)
            self._run_git(["config", "user.email", settings.git_user_email], cwd=self.project_root)
        except RuntimeError:
            # If it fails, log but don't crash — commit will still try
            print(f"[GitManager] Warning: could not configure git identity locally")
    
    def commit_changes(self, issue_key: str, summary: str, description: str = "") -> bool:
        """Stage all changes and create a commit.
        
        Args:
            issue_key: JIRA issue identifier (e.g., "PROJ-123")
            summary: Short commit summary
            description: Optional detailed description
        
        Returns:
            True if commit was successful, False otherwise
        """
        try:
            # Configure git identity locally in project_root (not global)
            self._configure_git_identity()
            
            # Check for changes
            status_result = self._run_git(["status", "--porcelain"], cwd=self.project_root)
            if not status_result.stdout.strip():
                print("[GitManager] No changes to commit.")
                return True
            
            # Stage all changes
            self._run_git(["add", "."], cwd=self.project_root)
            
            # Create commit with formatted message
            msg = self._format_commit_message(issue_key, summary, description)
            self._run_git(["commit", "-m", msg], cwd=self.project_root)
            
            print(f"[GitManager] Committed changes for {issue_key}")
            return True
            
        except RuntimeError as e:
            print(f"[GitManager] Commit failed: {e}")
            return False
    
    def get_current_branch(self) -> str:
        """Get the current branch name."""
        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.project_root, check=False)
        if result.returncode != 0:
            return "main"  # No commits yet, default branch
        return result.stdout.strip()
    
    def status(self) -> str:
        """Get git status output."""
        result = self._run_git(["status"], cwd=self.project_root)
        return result.stdout
    
    def push(self, branch_name: Optional[str] = None) -> bool:
        """Push current (or specified) branch to remote origin.
        
        Only works if remote_enabled is True (PROJECT_GITLAB_URL was set).
        
        Args:
            branch_name: Branch to push. Defaults to current branch.
        
        Returns:
            True if push succeeded, False otherwise.
        """
        if not self.remote_enabled:
            print("[GitManager] Push not available (no remote configured).")
            return False
        
        try:
            branch = branch_name or self.get_current_branch()
            self._run_git(["push", "-u", "origin", branch], cwd=self.project_root)
            print(f"[GitManager] Pushed branch '{branch}' to origin.")
            return True
        except RuntimeError as e:
            print(f"[GitManager] Push failed: {e}")
            return False
    
    def create_merge_request(self, title: str, body: str = "") -> Optional[str]:
        """Create a GitLab merge request using 'glab' CLI.
        
        Only works if remote_enabled is True.
        
        Args:
            title: MR title
            body: MR description (optional)
        
        Returns:
            MR URL if created, None otherwise.
        """
        if not self.remote_enabled:
            print("[GitManager] Merge request not available (no remote configured).")
            return None
        
        branch = self.get_current_branch()
        if branch in ("main", "master"):
            print("[GitManager] Cannot create MR from main/master branch.")
            return None
        
        try:
            # Use glab mr create
            cmd = ["glab", "mr", "create", "--title", title, "--source-branch", branch]
            if body:
                cmd.extend(["--description", body])
            result = subprocess.run(cmd, cwd=self.project_root, capture_output=True, text=True)
            if result.returncode == 0:
                # Extract URL from output
                output = result.stdout + result.stderr
                # glab outputs the MR URL
                for line in output.splitlines():
                    if "gitlab.com" in line or "http" in line:
                        url = line.strip()
                        print(f"[GitManager] Merge request created: {url}")
                        return url
                print("[GitManager] Merge request created.")
                return "created"
            else:
                print(f"[GitManager] Merge request failed: {result.stderr}")
                return None
        except FileNotFoundError:
            print("[GitManager] 'glab' CLI not found. Install with: brew install glab (mac) or apt install gitlab-cli (linux)")
            return None
        except Exception as e:
            print(f"[GitManager] Merge request error: {e}")
            return None
