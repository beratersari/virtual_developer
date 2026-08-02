"""Configuration management for JIRA Virtual Developer."""

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

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

    # Oh My OpenAgent Configuration
    opencode_cli: str = Field(default="opencode", description="OpenCode CLI command")
    project_root: Path = Field(default=Path.cwd(), description="Project root directory")
    sisyphus_plans_dir: Path = Field(default=Path(".sisyphus/plans"))
    default_model: str = Field(default="ollama/Qwen3.5-397B-A17B-FP8", description="Default model for agent tasks")
    
    # Git Configuration (for commits in target project folder)
    git_user_name: str = Field(default="DevBot", description="Git user name for commits")
    git_user_email: str = Field(default="devbot@example.com", description="Git user email for commits")
    
    # GitLab credentials — repository URL and source branch come from each Jira issue
    # (see src/issue_git_spec.py: Repository + Source + Target; MR source → target)
    #
    # Preferred: per-host PATs as JSON object:
    #   GITLAB_HOST_PATS={"gitlab.com":"glpat-…","gitlab.internal.com":"glpat-…"}
    # Legacy (still supported): single GITLAB_PAT + GITLAB_ALLOWED_HOSTS (same PAT for each host)
    gitlab_host_pats: str = Field(
        default="",
        description='JSON object mapping hostname → PAT, e.g. {"gitlab.com":"glpat-…"}',
    )
    gitlab_pat: str = Field(
        default="",
        description="Legacy single GitLab PAT (used with GITLAB_ALLOWED_HOSTS when map empty)",
    )
    # Hosts that may receive GITLAB_PAT (clone/push/MR). Required when legacy PAT is set.
    # Comma-separated hostnames, e.g. "gitlab.example.com,gitlab.com"
    gitlab_allowed_hosts: str = Field(
        default="",
        description="Legacy comma-separated hosts for single GITLAB_PAT (fail-closed when PAT is set)",
    )
    
    # Agent Configuration
    default_agent: str = Field(default="sisyphus")
    planning_agent: str = Field(default="prometheus")
    orchestrator_agent: str = Field(default="atlas")
    execution_category: str = Field(default="deep")

    # Agent prompts: one kit file with ## §section_id headers (see agent/AGENT_PROMPT.md)
    prompt_kit_file: Path = Field(
        default=Path("agent/AGENT_PROMPT.md"),
        description="Unified agent prompt kit",
    )
    
    # How many agent jobs run at once (raise for large boards / many subtasks)
    max_concurrent_jobs: int = Field(
        default=6,
        description="Max concurrent agent jobs (1–32; also writable on dashboard)",
    )
    # Parallel Jira transitions / dispatch inside one poll cycle
    poll_dispatch_workers: int = Field(
        default=8,
        description="Thread pool size for dispatching issues after a poll",
    )
    # Board poller is always on (sole intake path)
    poll_interval_seconds: int = Field(default=30)

    # Ops dashboard (FastAPI UI; no auth in v1 — keep loopback unless explicitly opened)
    dashboard_host: str = Field(default="127.0.0.1", description="Dashboard bind host")
    dashboard_port: int = Field(default=8080, description="Dashboard HTTP port")
    dashboard_enabled: bool = Field(default=True, description="Serve ops dashboard with the daemon")
    dashboard_allow_remote: bool = Field(
        default=False,
        description="If false, non-loopback dashboard_host is forced back to 127.0.0.1",
    )

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
    # Git clone hard timeout (B11) — hung clones must not hold job slots forever
    git_clone_timeout_seconds: int = Field(
        default=300,
        description="Max seconds for git clone (hard kill; default 5 minutes)",
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
    def gitlab_allowed_hosts_list(self) -> List[str]:
        """Hosts that have (or are allowed) a GitLab PAT (lowercase)."""
        return sorted(self.gitlab_host_pat_map().keys())

    def gitlab_host_pat_map(self) -> Dict[str, str]:
        """Resolved hostname → PAT map (prefer ``gitlab_host_pats`` JSON).

        Legacy fallback: if the JSON map is empty and ``gitlab_pat`` is set,
        each host in ``gitlab_allowed_hosts`` gets that same PAT.
        """
        out: Dict[str, str] = {}
        raw = (self.gitlab_host_pats or "").strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k, v in data.items():
                        host = str(k or "").strip().lower()
                        pat = str(v or "").strip()
                        if host and pat:
                            out[host] = pat
            except json.JSONDecodeError:
                logger.warning("GITLAB_HOST_PATS is not valid JSON; ignoring map")

        if out:
            return out

        # Legacy single PAT + host list
        pat = (self.gitlab_pat or "").strip()
        if not pat:
            return {}
        hosts = [
            h.strip().lower()
            for h in (self.gitlab_allowed_hosts or "").split(",")
            if h.strip()
        ]
        return {h: pat for h in hosts}

    def gitlab_pat_for_host(self, host: str) -> str:
        """Return the PAT for ``host`` (exact or parent-domain match), or ''."""
        h = (host or "").strip().lower()
        if not h:
            return ""
        mapping = self.gitlab_host_pat_map()
        if not mapping:
            return ""
        if h in mapping:
            return mapping[h]
        # subdomain: api.gitlab.example.com → gitlab.example.com
        for allowed, pat in mapping.items():
            if h == allowed or h.endswith("." + allowed):
                return pat
        return ""

    def gitlab_has_any_pat(self) -> bool:
        """True if at least one host has a configured PAT."""
        return bool(self.gitlab_host_pat_map())

    def set_gitlab_host_pat_map(self, mapping: Dict[str, str]) -> None:
        """Persist host→PAT map into runtime settings (JSON + legacy mirrors)."""
        cleaned: Dict[str, str] = {}
        for k, v in (mapping or {}).items():
            host = str(k or "").strip().lower()
            pat = str(v or "").strip()
            if host and pat:
                cleaned[host] = pat
        self.gitlab_host_pats = json.dumps(cleaned, separators=(",", ":")) if cleaned else ""
        # Mirror for older code paths / display
        self.gitlab_allowed_hosts = ",".join(sorted(cleaned.keys()))
        # Legacy single PAT: keep only when exactly one host (avoids wrong-host use)
        if len(cleaned) == 1:
            self.gitlab_pat = next(iter(cleaned.values()))
        else:
            self.gitlab_pat = ""

    def all_gitlab_pats(self) -> List[str]:
        """All configured PAT values (for log redaction)."""
        return list(dict.fromkeys(self.gitlab_host_pat_map().values()))
    
    def _kit_section(self, section_id: str) -> str:
        from src.orchestrator.prompt_kit import get_section

        return get_section(section_id, kit_path=self.prompt_kit_file)

    @property
    def prompt_planning(self) -> str:
        """Planning role rules (kit §role.planning)."""
        return self._kit_section("role.planning")

    @property
    def prompt_execution(self) -> str:
        """Atlas execution role rules (kit §role.execution)."""
        return self._kit_section("role.execution")

    @property
    def prompt_direct_execution(self) -> str:
        """Sisyphus direct-execution rules (kit §role.direct)."""
        return self._kit_section("role.direct")

    @property
    def prompt_oracle(self) -> str:
        """Oracle consultation rules (kit §role.oracle)."""
        return self._kit_section("role.oracle")

    def prompt_commit_policy(
        self,
        issue_key: str,
        *,
        work_branch: Optional[str] = None,
    ) -> str:
        """Issue-keyed git policy from kit §policy.commit."""
        from src.orchestrator.prompt_kit import get_section

        return get_section(
            "policy.commit",
            kit_path=self.prompt_kit_file,
            issue_key=issue_key,
            work_branch=work_branch,
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
