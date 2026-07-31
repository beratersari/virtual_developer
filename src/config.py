"""Configuration management for JIRA Virtual Developer."""

import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.logger import logger


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # JIRA Configuration
    # - On-prem Server/DC PAT: set JIRA_HOST + JIRA_API_TOKEN (Bearer)
    # - Jira Cloud API token: set JIRA_HOST + JIRA_EMAIL + JIRA_API_TOKEN (Basic email:token)
    jira_host: str = Field(default="", description="JIRA instance URL")
    jira_email: str = Field(
        default="",
        description="Atlassian account email (required for Jira Cloud API tokens; unused for on-prem Bearer PAT)",
    )
    jira_api_token: str = Field(
        default="",
        description="JIRA API token (Cloud) or personal access token (on-prem)",
    )
    jira_projects: str = Field(default="PROJ", description="Comma-separated project keys")
    jira_board_id: str = Field(
        default="",
        description="JIRA board id (from URL or GET /rest/agile/1.0/board)",
    )
    
    # Webhook Configuration
    webhook_port: int = Field(default=3000)
    webhook_path: str = Field(default="/webhook/jira")
    webhook_secret: Optional[str] = Field(default=None)
    
    # Oh My OpenAgent Configuration
    opencode_cli: str = Field(default="opencode", description="OpenCode CLI command")
    project_root: Path = Field(default=Path.cwd(), description="Project root directory")
    sisyphus_plans_dir: Path = Field(default=Path(".sisyphus/plans"))
    default_model: str = Field(default="ollama/Qwen3.5-397B-A17B-FP8", description="Default model for agent tasks")
    
    # Git Configuration (for commits in target project folder)
    git_user_name: str = Field(default="DevBot", description="Git user name for commits")
    git_user_email: str = Field(default="devbot@example.com", description="Git user email for commits")
    
    # Default branch to checkout before creating feature branches (optional)
    # If not set, falls back to 'main', then logs a message if neither exists
    default_branch: str = Field(default="develop", description="Default branch to checkout before creating feature branches")
    
    # Remote GitLab repository (optional)
    project_gitlab_url: str = Field(default="", description="GitLab repo URL to clone into PROJECT_ROOT")
    gitlab_pat: str = Field(default="", description="GitLab Personal Access Token for push/merge-request")
    
    # Agent Configuration
    default_agent: str = Field(default="sisyphus")
    planning_agent: str = Field(default="prometheus")
    orchestrator_agent: str = Field(default="atlas")
    execution_category: str = Field(default="deep")

    # -------------------------------------------------------------------------
    # System Prompts — loaded from markdown files with fallback to inline defaults.
    # Configure file paths via environment variables or keep defaults.
    # -------------------------------------------------------------------------
    prompt_planning_file: Path = Field(
        default=Path("agent/rules/PLANNING.md"),
        description="Path to planning prompt markdown file",
    )
    
    prompt_execution_file: Path = Field(
        default=Path("agent/rules/EXECUTION.md"),
        description="Path to execution prompt markdown file",
    )
    
    prompt_direct_execution_file: Path = Field(
        default=Path("agent/rules/DIRECT_EXECUTION.md"),
        description="Path to direct execution prompt markdown file",
    )
    
    prompt_oracle_file: Path = Field(
        default=Path("agent/rules/ORACLE.md"),
        description="Path to oracle prompt markdown file",
    )
    
    # Feature Flags
    auto_start_plans: bool = Field(default=False)
    max_concurrent_jobs: int = Field(default=3)
    enable_webhook: bool = Field(default=True)
    enable_polling: bool = Field(default=False)
    poll_interval_seconds: int = Field(default=30)

    # Temp Directory Configuration — per-issue clones are always required
    temp_dir_base: Path = Field(
        default=Path(".temp"),
        description="Base directory for temp working folders (relative to agent root)"
    )
    temp_dir_format: str = Field(
        default="{remote_name}_{jira_issue_id}_{timestamp}",
        description="Temp folder naming format. Available: {remote_name}, {jira_issue_id}, {timestamp}, {uuid}"
    )
    temp_cleanup_policy: str = Field(
        default="never",
        description="Temp folder cleanup policy: 'always', 'on_success', 'never'"
    )
    
    # Agent Task Configuration
    agent_task_timeout_seconds: int = Field(
        default=1800,
        description="Maximum time in seconds for an agent task to complete (default: 30 minutes)"
    )
    agent_task_max_retries: int = Field(
        default=3,
        description="Maximum number of retry attempts for failed agent tasks"
    )
    agent_task_retry_delay_seconds: int = Field(
        default=5,
        description="Initial delay in seconds between retry attempts (doubles with each retry)"
    )
    agent_task_retry_backoff_multiplier: float = Field(
        default=2.0,
        description="Multiplier for exponential backoff between retries"
    )
    agent_task_retry_on_timeout: bool = Field(
        default=True,
        description="Whether to retry tasks that timeout"
    )
    agent_task_retry_on_error: bool = Field(
        default=True,
        description="Whether to retry tasks that fail with errors"
    )
    
    # Redis / Celery
    redis_url: str = Field(default="redis://localhost:6379/0")
    
    # Logging
    log_level: str = Field(default="INFO")
    log_file: Optional[Path] = Field(default=Path("logs/jira-agent.log"))
    
    # Trigger Configuration - stored as strings, parsed as properties
    trigger_on_assignment: bool = Field(default=True)
    trigger_labels: str = Field(default="ai-assist,bot")
    trigger_mentions: str = Field(default="@DevBot,@AI")
    
    @property
    def full_plans_dir(self) -> Path:
        return Path.cwd() / self.sisyphus_plans_dir
    
    @property
    def state_dir(self) -> Path:
        from pathlib import Path as PathLib
        return PathLib.cwd() / ".jira-agent" / "state"
    
    @property
    def jira_projects_list(self) -> List[str]:
        """Get JIRA projects as a list."""
        if not self.jira_projects:
            return ["PROJ"]
        return [p.strip() for p in self.jira_projects.split(",") if p.strip()]
    
    @property
    def trigger_labels_list(self) -> List[str]:
        """Get trigger labels as a list."""
        if not self.trigger_labels:
            return ["ai-assist", "bot"]
        return [item.strip() for item in self.trigger_labels.split(",") if item.strip()]
    
    @property
    def prompt_planning(self) -> str:
        """Load planning prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_planning_file,
            self._get_default_planning_prompt(),
            "planning"
        )
    
    @property
    def prompt_execution(self) -> str:
        """Load execution prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_execution_file,
            self._get_default_execution_prompt(),
            "execution"
        )
    
    @property
    def prompt_direct_execution(self) -> str:
        """Load direct execution prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_direct_execution_file,
            self._get_default_direct_execution_prompt(),
            "direct execution"
        )
    
    @property
    def prompt_oracle(self) -> str:
        """Load oracle prompt from file if it exists, otherwise use default."""
        return self._load_prompt_from_file(
            self.prompt_oracle_file,
            self._get_default_oracle_prompt(),
            "oracle"
        )
    
    def _load_prompt_from_file(self, prompt_file: Path, default_prompt: str, prompt_name: str) -> str:
        from pathlib import Path as PathLib
        import os
        
        cwd = PathLib.cwd()
        logger.info(f"Loading {prompt_name} prompt from file")
        logger.debug(f"Current working directory: {cwd}")
        logger.debug(f"Input prompt_file parameter: {prompt_file}")
        
        paths_to_try = []
        
        # Prefer configured path, then agent/rules (canonical), then legacy agent/prompts.
        env_var_name = f"PROMPT_{prompt_name.upper().replace(' ', '_')}_FILE"
        env_path = os.environ.get(env_var_name)
        if env_path:
            paths_to_try.append(("env_var", PathLib(env_path)))
            logger.debug(f"Found env var {env_var_name}: {env_path}")
        else:
            logger.debug(f"No env var {env_var_name} set")
        
        if prompt_file.is_absolute():
            paths_to_try.append(("absolute", prompt_file))
        else:
            paths_to_try.append(("relative_cwd", cwd / prompt_file))
        
        paths_to_try.append(("agent_rules", cwd / "agent" / "rules" / prompt_file.name))
        paths_to_try.append(("agent_prompts_legacy", cwd / "agent" / "prompts" / prompt_file.name))
        
        logger.debug(f"Will try {len(paths_to_try)} paths in order:")
        for i, (source, path) in enumerate(paths_to_try, 1):
            exists = "✓ EXISTS" if path.exists() else "✗ NOT FOUND"
            is_file = " (file)" if path.exists() and path.is_file() else ""
            is_dir = " (dir)" if path.exists() and path.is_dir() else ""
            logger.debug(f"  {i}. [{source}] {path} {exists}{is_file}{is_dir}")
        
        loaded_from = None
        for source, path in paths_to_try:
            logger.debug(f"Trying prompt path: {path} (source: {source})")
            
            if not path.exists():
                logger.debug(f"  Path does not exist: {path}")
                continue
            
            if not path.is_file():
                logger.debug(f"  Path exists but is not a file (is_dir={path.is_dir()}): {path}")
                continue
            
            try:
                content = path.read_text(encoding="utf-8")
                file_size = len(content)
                line_count = len(content.splitlines())
                logger.info(f"Successfully loaded {prompt_name} prompt: {file_size} bytes, {line_count} lines from {source}")
                loaded_from = path
                return content
            except Exception as e:
                logger.error(f"Error reading {prompt_name} prompt from {path}: {e}")
                continue
        
        logger.warning(
            f"{prompt_name.capitalize()} prompt file not found in any location. "
            f"Using default inline prompt ({len(default_prompt)} chars)."
        )
        return default_prompt
    
    def _get_default_planning_prompt(self) -> str:
        """Default planning prompt (fallback if file not found)."""
        return (
            "As Prometheus, create a comprehensive work plan for this JIRA issue.\n"
            "\n"
            "1. **Interview Mode**: Ask clarifying questions if requirements are ambiguous\n"
            "2. **Research**: Explore the codebase to understand existing patterns\n"
            "3. **Plan Generation**: Create a detailed plan with:\n"
            "   - Task breakdown with checkboxes\n"
            "   - File references and locations\n"
            "   - Implementation approach\n"
            "   - Testing strategy\n"
            "   - Estimated effort\n"
            "\n"
            "Output the plan to the designated plan file."
        )
    
    def _get_default_execution_prompt(self) -> str:
        """Default execution prompt (fallback if file not found)."""
        return (
            "## Delegation Guidelines\n"
            '- Use `category="visual-engineering"` for UI/UX work\n'
            '- Use `category="deep"` for complex problem-solving\n'
            '- Use `category="quick"` for simple fixes\n'
            '- Use `subagent_type="oracle"` for architecture decisions\n'
            '- Use `subagent_type="explore"` for codebase research\n'
            "\n"
            "## Success Criteria\n"
            "- All plan checkboxes checked\n"
            "- Tests passing\n"
            "- No type errors\n"
            "- Code follows project conventions\n"
            "\n"
            "## Commit message format (MANDATORY)\n"
            "`[JIRA-ISSUE-ID] <type>: <description>`\n"
            "Types: feat, fix, refactor, docs, test, perf, ci, build, revert, chore\n"
            "Examples: `[KEY] feat: ...` · `[KEY] fix: ...` · `[KEY] chore: ...`"
        )
    
    def _get_default_direct_execution_prompt(self) -> str:
        """Default direct execution prompt (fallback if file not found).

        Commit format must match agent/rules/EXECUTION.md:
        [JIRA-KEY] type: description
        """
        return (
            "## Instructions\n"
            "1. Analyze the task and current codebase\n"
            "2. Create todos for multi-step work\n"
            "3. Implement the solution following existing patterns\n"
            "4. Run verification (tests, type checking)\n"
            "5. **COMMIT YOUR CHANGES** (mandatory if you modified any files)\n"
            "6. Report completion with summary of changes and commit hash\n"
            "\n"
            "## Commit message format (MANDATORY — same as EXECUTION.md)\n"
            "After code changes you MUST create a git commit yourself. Use ONLY this format:\n"
            "\n"
            "```\n"
            "[JIRA-ISSUE-ID] <type>: <description>\n"
            "```\n"
            "\n"
            "Allowed types: feat, fix, refactor, docs, test, perf, ci, build, revert, chore\n"
            "\n"
            "Doğru format örnekleri:\n"
            "  [JIRA-ISSUE-ID] feat: Yeni özellik eklendi\n"
            "  [JIRA-ISSUE-ID] fix: Hata düzeltildi\n"
            "  [JIRA-ISSUE-ID] refactor: Kodun çalışma şeklini değiştirmeyen iyileştirme\n"
            "  [JIRA-ISSUE-ID] docs: Dökümantasyon işleri\n"
            "  [JIRA-ISSUE-ID] test: Birim testler\n"
            "  [JIRA-ISSUE-ID] perf: Çalışma mantığını değiştirmeyen performans iyileştirmesi\n"
            "  [JIRA-ISSUE-ID] ci: CI/CD değişiklikleri\n"
            "  [JIRA-ISSUE-ID] build: Build sistemi ile ilgili değişiklikler\n"
            "  [JIRA-ISSUE-ID] revert: Kodu geri almak\n"
            "  [JIRA-ISSUE-ID] chore: Genel işler, küçük düzeltmeler\n"
            "\n"
            "Rules:\n"
            "- Subject MUST be `[ISSUE-KEY] type: description`\n"
            "- Do NOT push or create merge requests (the system does that)\n"
            "- Do NOT commit .env, credentials, or secret files\n"
            "\n"
            "## Constraints\n"
            "- Follow existing code style\n"
            "- Add tests for new functionality\n"
            "- Do not break existing tests\n"
            "- Minimal, focused changes"
        )
    
    def _get_default_oracle_prompt(self) -> str:
        """Default oracle prompt (fallback if file not found)."""
        return (
            "## Response Format\n"
            "1. **Direct Answer**: Clear response to the question\n"
            "2. **Rationale**: Why this approach is recommended\n"
            "3. **Alternatives**: Other options considered\n"
            "4. **Trade-offs**: Pros/cons of each approach\n"
            "5. **Implementation Hints**: Key files/patterns to use\n"
            "\n"
            "Be thorough but concise. Focus on practical guidance."
        )
    

    @property
    def trigger_mentions_list(self) -> List[str]:
        """Get trigger mentions as a list."""
        if not self.trigger_mentions:
            return ["@DevBot", "@AI"]
        return [item.strip() for item in self.trigger_mentions.split(",") if item.strip()]
    
    def is_configured(self) -> bool:
        """Check if required JIRA settings are configured."""
        return all([
            self.jira_host and self.jira_host.strip(),
            self.jira_api_token and self.jira_api_token.strip(),
        ])
    
    def validate_or_raise(self):
        """Validate settings and raise error if not configured."""
        if not self.is_configured():
            missing = []
            if not self.jira_host:
                missing.append("JIRA_HOST")
            if not self.jira_api_token:
                missing.append("JIRA_API_TOKEN")
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}\n"
                "Please set these in your .env file or environment variables.\n"
                "Run: cp .env.example .env && nano .env"
            )


# Global settings instance - lazy loaded to handle missing config gracefully
_settings: Optional[Settings] = None
_current_temp_dir: Optional[Path] = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

def set_current_temp_dir(temp_dir: Optional[Path]) -> None:
    global _current_temp_dir
    _current_temp_dir = temp_dir
    logger.debug(f"Current temp directory set to: {temp_dir}")

def get_current_temp_dir() -> Optional[Path]:
    return _current_temp_dir

settings = get_settings()
