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
