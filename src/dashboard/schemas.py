"""Dashboard API response models (presentation DTOs; no business rules)."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator


class MetaResponse(BaseModel):
    version: str
    server_time: str
    app_name: str = "JIRA Virtual Developer"


class BulkJobDeleteRequest(BaseModel):
    """Body for POST /api/jobs/bulk-delete."""

    job_ids: List[str] = Field(default_factory=list, max_length=100)
    delete_artifacts: bool = True


class ScheduleCreateRequest(BaseModel):
    """Body for POST /api/schedules."""

    title: str
    description: str = ""
    repository_url: str
    # Required when source_branch_mode is "custom"; ignored for "issue_key"
    source_branch: str = ""
    target_branch: str
    mode: str  # plan | build
    scheduled_at: str
    project_key: Optional[str] = None
    # Jira issue type name (Task, Story, ExtBug, Görev, …). Resolved per project.
    issue_type: str = "Task"
    # custom = use source_branch; issue_key = feature/{NEW_JIRA_KEY} after create
    source_branch_mode: str = "custom"
    # Start process_event immediately (does not wait for scheduled_at)
    dispatch_now: bool = False


class ScheduleExistingRequest(BaseModel):
    """Body for POST /api/schedules/from-issue — schedule an existing Jira issue."""

    issue_key: str
    scheduled_at: str
    dispatch_now: bool = False


class ScheduleItem(BaseModel):
    schedule_id: str
    title: str = ""
    description: str = ""
    repository_url: str = ""
    source_branch: str = ""
    target_branch: str = ""
    mode: str = ""
    issue_type: str = "Task"
    scheduled_at: str = ""
    status: str = "scheduled"
    issue_key: str = ""
    project_key: str = ""
    label: str = "SCHEDULED_AI_JOB"
    source: str = "new"  # new | existing
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    dispatched_at: Optional[str] = None
    error_message: Optional[str] = None


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


class JobRetryAttempt(BaseModel):
    """One failed attempt that triggered a retry, nested under a parent job."""

    attempt_number: int = 0
    label: str = ""  # e.g. "retry1" — matches session file _retryN suffix
    reason: str = ""  # "error" | "timeout"
    delay_seconds: float = 0.0
    failed_session_log_path: Optional[str] = None
    error_message: Optional[str] = None
    return_code: Optional[int] = None
    opencode_session_id: Optional[str] = None
    task_id: Optional[str] = None
    timestamp: Optional[str] = None


class JobItem(BaseModel):
    """One processing run (job) for a Jira issue."""

    job_id: str
    issue_key: str
    summary: str = ""
    description: str = ""
    workflow_type: str = "execution"
    agent: str = ""
    # OpenCode model id used for this run (settings default_model at start)
    model: Optional[str] = None
    status: str = "running"
    task_id: Optional[str] = None
    task_ids: List[str] = Field(default_factory=list)
    opencode_session_id: Optional[str] = None
    opencode_session_ids: List[str] = Field(default_factory=list)
    session_log_path: Optional[str] = None
    # All OpenCode logs for this job (initial + _retryN), ordered oldest→newest
    session_log_paths: List[str] = Field(default_factory=list)
    prompt_path: Optional[str] = None
    prompt_paths: List[str] = Field(default_factory=list)
    # Failed attempts that scheduled retries (nested; not separate dashboard jobs)
    retry_attempts: List[JobRetryAttempt] = Field(default_factory=list)
    progress_percentage: int = 0
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    updated_at: Optional[str] = None
    live: bool = False
    # Git delivery for this run (set after successful push / MR)
    feature_branch: Optional[str] = None
    merge_request_url: Optional[str] = None
    commit_sha: Optional[str] = None
    commit_subject: Optional[str] = None
    commit_url: Optional[str] = None
    # delivered | no_new_commits | etc. (soft completion when no new commits)
    delivery_status: Optional[str] = None
    delivery_note: Optional[str] = None
    # Temp clone used for this run (job record or session bind)
    working_directory: Optional[str] = None
    # jira (default) | gitlab — same job/chat UI, different intake
    source: str = "jira"
    gitlab_project: Optional[str] = None
    gitlab_mr_iid: Optional[int] = None


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


class OpencodeSessionBind(BaseModel):
    """One persisted OpenCode session keyed by repository + work + target."""

    bind_id: str
    repository_url: str = ""
    repository_key: str = ""
    branch: str = ""
    target_branch: str = ""
    session_id: str = ""
    issue_key: str = ""
    job_id: Optional[str] = None
    working_directory: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SettingsView(BaseModel):
    """Safe settings projection (secrets never included as plaintext values)."""

    jira_host: str = ""
    jira_board_id: str = ""
    jira_projects: str = ""
    poll_interval_seconds: int = 30
    trigger_labels: str = ""
    trigger_on_assignment: bool = True
    max_concurrent_jobs: int = 3
    # Single wall-clock budget for agent runner + OpenCode process (same value)
    agent_task_timeout_seconds: int = 1800
    agent_task_max_retries: int = 3
    agent_task_max_incomplete_retries: int = 256
    default_branch: str = "(from Jira issue)"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    # Presence flags only — never return token/PAT values
    jira_token_configured: bool = False
    gitlab_pat_configured: bool = False
    # Optional Cloud Basic email (not a secret). Empty → Bearer PAT.
    jira_email_configured: bool = False
    jira_email: str = ""
    # Legacy flat list of hosts (derived from credential map)
    gitlab_allowed_hosts: str = ""
    # Per-host GitLab credentials (hosts only + configured flag; no PAT values)
    gitlab_credentials: List["GitlabHostCredentialView"] = Field(default_factory=list)
    # Runtime DEFAULT_MODEL only — full inventory is GET /api/models
    default_model: str = ""
    gitlab_webhook_enabled: bool = False
    gitlab_bot_mentions: str = ""
    gitlab_webhook_secret_configured: bool = False
    gitlab_webhook_path: str = "/webhooks/gitlab"
    # Saved remotes for the schedule New-issue picker (not secrets)
    project_repositories: List["ProjectRepositoryItem"] = Field(default_factory=list)


class ProjectRepositoryItem(BaseModel):
    """One bookmarked git remote for the New-issue form."""

    label: str = Field(default="", max_length=80)
    url: str = Field(..., min_length=3, max_length=500)
    target_branch: str = Field(default="", max_length=255)
    source_branch: str = Field(default="", max_length=255)

    @field_validator("url", mode="before")
    @classmethod
    def _git_url(cls, value: Any) -> str:
        from src.issue_git_spec import _looks_like_git_url, _normalize_repo_url

        url = _normalize_repo_url(str(value or ""))
        if not _looks_like_git_url(url):
            raise ValueError(
                "Must be an http(s), ssh, or git@ repository URL (e.g. https://gitlab.com/g/r.git)"
            )
        return url


class GitlabHostCredentialView(BaseModel):
    """Safe projection of one GitLab host credential (no PAT value)."""

    host: str
    pat_configured: bool = False


class GitlabHostCredentialUpdate(BaseModel):
    """One host row from the Settings UI.

    * ``pat`` omit/empty → keep existing PAT for that host (if any)
    * ``pat`` non-empty → set/replace PAT for host
    * ``previous_host`` + empty pat → copy stored PAT from the old hostname
    * Hosts omitted from the list on full replace are removed
    """

    host: str = Field(..., min_length=1, max_length=253)
    pat: Optional[str] = Field(default=None, max_length=4000)
    previous_host: Optional[str] = Field(
        default=None,
        max_length=253,
        description="If the operator renamed this host and pat is empty, copy the stored PAT from previous_host",
    )


class GitlabConnectionTestRequest(BaseModel):
    """Body for POST /api/settings/gitlab/test."""

    host: str = Field(..., min_length=1, max_length=253)
    # Write-only optional PAT; if empty, use stored PAT for host
    pat: Optional[str] = Field(default=None, max_length=4000)
    max_projects: int = Field(default=25, ge=1, le=50)


class JiraConnectionTestRequest(BaseModel):
    """Body for POST /api/settings/jira/test.

    Omitted/empty token uses the stored runtime token. Never echoed back.
    Optional ``email`` enables Cloud Basic auth for the probe.
    """

    host: Optional[str] = Field(default=None, max_length=500)
    email: Optional[str] = Field(
        default=None,
        max_length=320,
        description="Optional Cloud email for Basic auth; omit for Bearer",
    )
    api_token: Optional[str] = Field(default=None, max_length=4000)
    max_projects: int = Field(default=25, ge=1, le=50)


class SettingsUpdate(BaseModel):
    """Writable settings (runtime only).

    Secret fields (``jira_api_token``, per-host ``gitlab_credentials[].pat``)
    are write-only: omit/empty to leave unchanged. They are never echoed in
    SettingsView.
    """

    jira_host: Optional[str] = Field(default=None, max_length=500)
    jira_email: Optional[str] = Field(
        default=None,
        max_length=320,
        description="Optional Cloud email for Basic auth; empty clears (Bearer)",
    )
    jira_api_token: Optional[str] = Field(
        default=None,
        max_length=4000,
        description="Write-only Jira PAT / API token (omit to keep current)",
    )
    # Full replace list of host credentials (preferred)
    gitlab_credentials: Optional[List[GitlabHostCredentialUpdate]] = None
    # Legacy single PAT + hosts (still accepted; merged into map)
    gitlab_pat: Optional[str] = Field(
        default=None,
        max_length=4000,
        description="Legacy write-only single GitLab PAT",
    )
    gitlab_allowed_hosts: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Legacy comma-separated hosts for single GITLAB_PAT",
    )
    jira_board_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Jira Agile board id (digits only, e.g. 1)",
    )

    @field_validator("jira_board_id", mode="before")
    @classmethod
    def _jira_board_id_digits(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        # Strip accidental markdown wrapping: `1` or ``1``
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "`'\"":
            text = text[1:-1].strip()
        if not text.isdigit():
            raise ValueError(
                "Jira board ID must be a number (Agile board id from the board URL, e.g. 1)"
            )
        return text
    poll_interval_seconds: Optional[int] = Field(default=None, ge=5, le=3600)
    trigger_labels: Optional[str] = Field(default=None, max_length=500)
    trigger_on_assignment: Optional[bool] = None
    max_concurrent_jobs: Optional[int] = Field(default=None, ge=1, le=64)
    # Agent and OpenCode share this one timeout (orchestrator aborts the serve turn)
    agent_task_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=30,
        le=86400,
        description="Wall-clock seconds per OpenCode/agent attempt (30s–24h)",
    )
    agent_task_max_retries: Optional[int] = Field(
        default=None,
        ge=0,
        le=64,
        description="Retries after timeout or hard error (0 = no retry)",
    )
    agent_task_max_incomplete_retries: Optional[int] = Field(
        default=None,
        ge=0,
        le=256,
        description=(
            "Serve retries after compact-then-stop / incomplete session "
            "(independent of error retries)"
        ),
    )
    default_model: Optional[str] = Field(default=None, max_length=200)
    project_repositories: Optional[List[ProjectRepositoryItem]] = Field(
        default=None,
        max_length=40,
        description="Full replace of saved git remotes for the New-issue form",
    )


class QueueItem(BaseModel):
    """One waiting or running intake message (Jira issue or GitLab MR comment)."""

    queue_id: str
    status: str = "queued"
    source: str = "jira"
    issue_key: str = ""
    summary: str = ""
    message: str = ""
    repository_url: str = ""
    source_branch: str = ""
    work_branch: str = ""
    target_branch: str = ""
    lock_key: str = ""
    job_id: Optional[str] = None
    merge_request_url: str = ""
    gitlab_note_id: str = ""
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class QueueResponse(BaseModel):
    items: List[QueueItem] = Field(default_factory=list)
    queued_count: int = 0
    running_count: int = 0
    total: int = 0
    server_time: str = ""
