"""Dashboard API response models (presentation DTOs; no business rules)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MetaResponse(BaseModel):
    version: str
    server_time: str
    app_name: str = "JIRA Virtual Developer"


class TaskItem(BaseModel):
    issue_key: str
    summary: str
    status: str
    progress_percentage: int = 0
    workflow_type: Optional[str] = None
    jira_assignee: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    feature_branch: Optional[str] = None
    merge_request_url: Optional[str] = None
    live: bool = False
    task_id: Optional[str] = None
    opencode_session_id: Optional[str] = None
    opencode_session_ids: List[str] = Field(default_factory=list)


class TasksResponse(BaseModel):
    tasks: List[TaskItem]
    total: int
    server_time: str


class JobItem(BaseModel):
    """One processing run (job) for a Jira issue."""

    job_id: str
    issue_key: str
    summary: str = ""
    description: str = ""
    workflow_type: str = "execution"
    agent: str = ""
    status: str = "running"
    task_id: Optional[str] = None
    task_ids: List[str] = Field(default_factory=list)
    opencode_session_id: Optional[str] = None
    opencode_session_ids: List[str] = Field(default_factory=list)
    session_log_path: Optional[str] = None
    prompt_path: Optional[str] = None
    progress_percentage: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None
    live: bool = False


class JobsResponse(BaseModel):
    jobs: List[JobItem]
    total: int
    page: int = 1
    page_size: int = 25
    issue_key_filter: Optional[str] = None
    server_time: str


class PolledIssueItem(BaseModel):
    key: str
    summary: str = ""
    jira_status: str = ""
    labels: List[str] = Field(default_factory=list)
    assignee: Optional[str] = None
    matched_label: bool = False
    matched_assignee: bool = False
    is_todo: bool = False
    will_process: bool = False
    local_status: Optional[str] = None
    matched_labels: List[str] = Field(default_factory=list)


class PollStatusResponse(BaseModel):
    phase: str
    last_poll_at: Optional[str] = None
    next_poll_at: Optional[str] = None
    seconds_until_next_poll: Optional[int] = None
    poll_interval_seconds: int = 30
    source: Optional[str] = None
    board_id: Optional[str] = None
    issues: List[PolledIssueItem] = Field(default_factory=list)
    matched_count: int = 0
    will_process_count: int = 0
    error: Optional[str] = None
    cycle: int = 0
    server_time: str


class ModelOption(BaseModel):
    """One model from GET /api/models (display DTO; inventory is backend-only)."""

    id: str
    name: str = ""
    provider: str = ""
    source: str = "cli"  # cli | config | config_default | settings
    # Pre-formatted for the UI — frontend must not invent labels
    label: str = ""


class ModelsResponse(BaseModel):
    """OpenCode model inventory — sole source for the Settings model list."""

    default_model: str = ""
    models: List[ModelOption] = Field(default_factory=list)
    opencode_config_model: Optional[str] = None
    opencode_config_path: Optional[str] = None
    error: Optional[str] = None
    server_time: str = ""


class SettingsView(BaseModel):
    """Safe settings projection (secrets never included as plaintext values)."""

    jira_host: str = ""
    jira_board_id: str = ""
    jira_projects: str = ""
    poll_interval_seconds: int = 30
    trigger_labels: str = ""
    trigger_on_assignment: bool = True
    auto_start_plans: bool = False
    max_concurrent_jobs: int = 3
    default_branch: str = "(from Jira issue)"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    # Presence flags only
    jira_token_configured: bool = False
    gitlab_pat_configured: bool = False
    jira_email_configured: bool = False
    # Runtime DEFAULT_MODEL only — full inventory is GET /api/models
    default_model: str = ""


class SettingsUpdate(BaseModel):
    """Writable settings (runtime only; no secrets)."""

    jira_board_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    poll_interval_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    trigger_labels: Optional[str] = Field(default=None, max_length=500)
    trigger_on_assignment: Optional[bool] = None
    auto_start_plans: Optional[bool] = None
    max_concurrent_jobs: Optional[int] = Field(default=None, ge=1, le=64)
    default_model: Optional[str] = Field(default=None, max_length=200)


class DashboardEnvelope(BaseModel):
    """Full snapshot pushed over WebSocket."""

    type: str = "dashboard"
    meta: MetaResponse
    tasks: TasksResponse
    jobs: Optional[JobsResponse] = None
    poll: PollStatusResponse
    settings: SettingsView
