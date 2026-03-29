"""Configuration management for JIRA Virtual Developer."""

import os
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    
    # JIRA Configuration - all optional with defaults for init command
    jira_host: str = Field(default="", description="JIRA instance URL")
    jira_username: str = Field(default="", description="JIRA username/email")
    jira_api_token: str = Field(default="", description="JIRA API token")
    jira_projects: str = Field(default="PROJ", description="Comma-separated project keys")
    
    # Webhook Configuration
    webhook_port: int = Field(default=3000)
    webhook_path: str = Field(default="/webhook/jira")
    webhook_secret: Optional[str] = Field(default=None)
    
    # Oh My OpenAgent Configuration
    opencode_cli: str = Field(default="oh-my-opencode", description="OpenCode CLI command")
    project_root: Path = Field(default=Path.cwd(), description="Project root directory")
    sisyphus_plans_dir: Path = Field(default=Path(".sisyphus/plans"))
    
    # Git Configuration (for commits in target project folder)
    git_user_name: str = Field(default="DevBot", description="Git user name for commits")
    git_user_email: str = Field(default="devbot@example.com", description="Git user email for commits")
    
    # Remote GitLab repository (optional)
    project_gitlab_url: str = Field(default="", description="GitLab repo URL to clone into PROJECT_ROOT")
    gitlab_pat: str = Field(default="", description="GitLab Personal Access Token for push/merge-request")
    
    # Agent Configuration
    default_agent: str = Field(default="sisyphus")
    planning_agent: str = Field(default="prometheus")
    orchestrator_agent: str = Field(default="atlas")
    execution_category: str = Field(default="deep")

    # Code Review Configuration
    code_review_agent: str = Field(
        default="sisyphus",
        description="Agent to use for code review (uses read-only review prompt)"
    )
    code_review_model: str = Field(
        default="opencode/big-pickle",
        description="Model to use for code review via oh-my-openagent"
    )

    # -------------------------------------------------------------------------
    # System Prompts — customise the instructions sent to each agent workflow.
    # Use literal \n in the env-var value for newlines.
    # -------------------------------------------------------------------------
    prompt_planning: str = Field(
        default=(
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
        ),
        description="System prompt for Prometheus planning workflow",
    )

    prompt_execution: str = Field(
        default=(
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
            "- Code follows project conventions"
        ),
        description="System prompt for Atlas execution/orchestration workflow",
    )

    prompt_direct_execution: str = Field(
        default=(
            "## Instructions\n"
            "1. Analyze the task and current codebase\n"
            "2. Create todos for multi-step work\n"
            "3. Implement the solution following existing patterns\n"
            "4. Run verification (tests, type checking)\n"
            "5. Report completion with summary of changes\n"
            "\n"
            "## Constraints\n"
            "- Follow existing code style\n"
            "- Add tests for new functionality\n"
            "- Do not break existing tests\n"
            "- Minimal, focused changes"
        ),
        description="System prompt for Sisyphus direct-execution workflow",
    )

    prompt_code_review: str = Field(
        default=(
            "You are performing a **code review** on the changes that were just made for this JIRA issue.\n"
            "This is a **read-only review** — do NOT make any edits or changes to the code.\n"
            "\n"
            "### Review Steps\n"
            "1. **Examine Changes**: Run `git diff HEAD~1` (or `git log --oneline -5` then diff) to see what was changed\n"
            "2. **Read Modified Files**: Read the full content of any modified files to understand context\n"
            "3. **Analyze Code Quality**: Check for:\n"
            "   - Correctness: Does the code do what the issue description asks?\n"
            "   - Bug risks: Potential null references, off-by-one errors, race conditions\n"
            "   - Code style: Consistency with existing codebase patterns\n"
            "   - Error handling: Are edge cases covered?\n"
            "   - Security: Any obvious security concerns (hardcoded secrets, injection risks, etc.)\n"
            "   - Test coverage: Were tests added or updated?\n"
            "   - Documentation: Are comments and docstrings adequate?\n"
            "\n"
            "### Output Format\n"
            "Provide your review in this exact format:\n"
            "\n"
            "**REVIEW VERDICT**: PASS | NEEDS_ATTENTION | CONCERNS\n"
            "\n"
            "**Summary**: One paragraph overview of the changes and overall quality.\n"
            "\n"
            "**Findings**:\n"
            "- [GOOD] Things done well\n"
            "- [WARN] Things that could be improved (non-blocking)\n"
            "- [ISSUE] Potential problems that should be addressed\n"
            "\n"
            "**Recommendation**: Final recommendation for the human reviewer.\n"
            "\n"
            "## Constraints\n"
            "- Do NOT edit any files\n"
            "- Do NOT run any build/test commands\n"
            "- Only READ files and git history\n"
            "- Be constructive and specific in feedback\n"
            "- Focus on substantive issues, not nitpicking"
        ),
        description="System prompt for Code Review workflow (runs after successful execution)",
    )

    prompt_oracle: str = Field(
        default=(
            "## Response Format\n"
            "1. **Direct Answer**: Clear response to the question\n"
            "2. **Rationale**: Why this approach is recommended\n"
            "3. **Alternatives**: Other options considered\n"
            "4. **Trade-offs**: Pros/cons of each approach\n"
            "5. **Implementation Hints**: Key files/patterns to use\n"
            "\n"
            "Be thorough but concise. Focus on practical guidance."
        ),
        description="System prompt for Oracle architecture-consultation workflow",
    )

    # Feature Flags
    auto_start_plans: bool = Field(default=False)
    max_concurrent_jobs: int = Field(default=3)
    enable_webhook: bool = Field(default=True)
    enable_polling: bool = Field(default=False)
    poll_interval_seconds: int = Field(default=30)

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
        """Get absolute path to plans directory."""
        return self.project_root / self.sisyphus_plans_dir
    
    @property
    def state_dir(self) -> Path:
        """Get state directory path."""
        return self.project_root / ".jira-agent" / "state"
    
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
    def trigger_mentions_list(self) -> List[str]:
        """Get trigger mentions as a list."""
        if not self.trigger_mentions:
            return ["@DevBot", "@AI"]
        return [item.strip() for item in self.trigger_mentions.split(",") if item.strip()]
    
    def is_configured(self) -> bool:
        """Check if required JIRA settings are configured."""
        return all([
            self.jira_host and self.jira_host.strip(),
            self.jira_username and self.jira_username.strip(),
            self.jira_api_token and self.jira_api_token.strip(),
        ])
    
    def validate_or_raise(self):
        """Validate settings and raise error if not configured."""
        if not self.is_configured():
            missing = []
            if not self.jira_host:
                missing.append("JIRA_HOST")
            if not self.jira_username:
                missing.append("JIRA_USERNAME")
            if not self.jira_api_token:
                missing.append("JIRA_API_TOKEN")
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}\n"
                "Please set these in your .env file or environment variables.\n"
                "Run: cp .env.example .env && nano .env"
            )


# Global settings instance - lazy loaded to handle missing config gracefully
_settings: Optional[Settings] = None

def get_settings() -> Settings:
    """Get settings instance, creating it if needed."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

# For backwards compatibility
settings = get_settings()
