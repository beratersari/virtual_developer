/** Types mirror backend DTOs. No business rules here. */

export type Meta = {
  version: string
  server_time: string
  app_name: string
}

export type TaskItem = {
  issue_key: string
  summary: string
  status: string
  progress_percentage: number
  workflow_type?: string | null
  jira_assignee?: string | null
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
  feature_branch?: string | null
  merge_request_url?: string | null
  live: boolean
  task_id?: string | null
  opencode_session_id?: string | null
  opencode_session_ids?: string[]
}

export type TasksPayload = {
  tasks: TaskItem[]
  total: number
  server_time: string
}

export type PolledIssue = {
  key: string
  summary: string
  jira_status: string
  labels: string[]
  assignee?: string | null
  matched_label: boolean
  matched_assignee: boolean
  is_todo: boolean
  will_process: boolean
  local_status?: string | null
  matched_labels: string[]
}

export type PollPayload = {
  phase: string
  last_poll_at?: string | null
  next_poll_at?: string | null
  seconds_until_next_poll?: number | null
  poll_interval_seconds: number
  source?: string | null
  board_id?: string | null
  issues: PolledIssue[]
  matched_count: number
  will_process_count: number
  error?: string | null
  cycle: number
  server_time: string
}

/** DTO from GET /api/models — inventory is backend-only. */
export type ModelOption = {
  id: string
  name: string
  provider: string
  source: string
  /** Pre-formatted display string from the server */
  label: string
}

export type ModelsPayload = {
  default_model: string
  models: ModelOption[]
  opencode_config_model?: string | null
  opencode_config_path?: string | null
  error?: string | null
  server_time?: string
}

export type GitlabHostCredential = {
  host: string
  /** True when a PAT is stored for this host (value never returned). */
  pat_configured: boolean
}

export type SettingsPayload = {
  jira_host: string
  jira_board_id: string
  jira_projects: string
  poll_interval_seconds: number
  trigger_labels: string
  trigger_on_assignment: boolean
  max_concurrent_jobs: number
  /**
   * Wall-clock seconds per OpenCode/agent attempt (same budget for both).
   * Runtime only — mirrors AGENT_TASK_TIMEOUT_SECONDS.
   */
  agent_task_timeout_seconds: number
  default_branch: string
  dashboard_host: string
  dashboard_port: number
  jira_token_configured: boolean
  gitlab_pat_configured: boolean
  /** True when a Cloud Basic email is configured (not a secret). */
  jira_email_configured: boolean
  /**
   * Optional Cloud Basic auth email. Empty → Bearer PAT (prod/on-prem).
   * Set for Jira Cloud API tokens (dev).
   */
  jira_email?: string
  /** Derived host list (legacy / summary). */
  gitlab_allowed_hosts?: string
  /** Per-host GitLab credentials (hosts + flags only). */
  gitlab_credentials?: GitlabHostCredential[]
  /** Runtime DEFAULT_MODEL — inventory list is GET /api/models, not settings */
  default_model: string
}

/** One editable GitLab host row (PAT write-only). */
export type GitlabHostCredentialDraft = {
  host: string
  /** Leave empty to keep existing PAT for this host. */
  pat: string
  pat_configured: boolean
}

/** Write-only secrets for PATCH /api/settings — never returned by GET. */
export type SettingsWriteSecrets = {
  jira_api_token?: string
  gitlab_pat?: string
}

/** One push/commit/MR delivery from a job run (tasks may have many). */
export type GitDelivery = {
  job_id?: string | null
  feature_branch?: string | null
  merge_request_url?: string | null
  commit_sha?: string | null
  commit_subject?: string | null
  commit_url?: string | null
  created_at?: string | null
  status?: string | null
}

export type JobItem = {
  job_id: string
  issue_key: string
  summary: string
  /** Snapshot of Jira description at job start (not live issue text). */
  description?: string
  workflow_type: string
  agent: string
  status: string
  task_id?: string | null
  task_ids?: string[]
  opencode_session_id?: string | null
  opencode_session_ids?: string[]
  session_log_path?: string | null
  prompt_path?: string | null
  progress_percentage: number
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
  updated_at?: string | null
  live: boolean
  /** Git delivery for this run (set after successful push / MR). */
  feature_branch?: string | null
  merge_request_url?: string | null
  commit_sha?: string | null
  commit_subject?: string | null
  commit_url?: string | null
  /** delivered | no_new_commits — soft complete when agent OK but no new commits */
  delivery_status?: string | null
  delivery_note?: string | null
}

export type JobsPayload = {
  jobs: JobItem[]
  total: number
  page?: number
  page_size?: number
  issue_key_filter?: string | null
  server_time: string
}

export type ScheduleItem = {
  schedule_id: string
  title: string
  description?: string
  repository_url: string
  source_branch: string
  target_branch: string
  mode: string
  issue_type?: string
  scheduled_at: string
  status: string
  issue_key: string
  project_key?: string
  label?: string
  /** new = created via form; existing = scheduled from issue key */
  source?: string
  created_at?: string | null
  updated_at?: string | null
  dispatched_at?: string | null
  error_message?: string | null
}

/** Response from GET /api/schedules/preview */
export type SchedulePreview = {
  ok: boolean
  issue_key: string
  title: string
  description?: string
  jira_status?: string
  issue_type?: string
  labels?: string[]
  template_valid: boolean
  repository_url: string
  source_branch: string
  target_branch: string
  mode: string
  message?: string
  error?: string
}

export type SchedulesPayload = {
  schedules: ScheduleItem[]
  total: number
  server_time?: string
}

export type ScheduleCreateBody = {
  title: string
  description?: string
  repository_url: string
  /** Required when source_branch_mode is "custom" */
  source_branch?: string
  target_branch: string
  mode: 'plan' | 'build'
  scheduled_at: string
  project_key?: string
  /** Jira issue type name (Task, Story, ExtBug, Görev, …) */
  issue_type?: string
  /**
   * custom — use source_branch as given.
   * issue_key — after create, set Source to feature/{NEW_JIRA_KEY}.
   */
  source_branch_mode?: 'custom' | 'issue_key'
}

export type JiraIssueType = {
  id: string
  name: string
  subtask: boolean
}

export type JiraIssueTypesPayload = {
  ok?: boolean
  project_key?: string
  issue_types: JiraIssueType[]
  error?: string | null
  server_time?: string
}

export type DashboardPayload = {
  type: string
  meta: Meta
  tasks: TasksPayload
  jobs?: JobsPayload
  poll: PollPayload
  settings: SettingsPayload
}

export type TextArtifact = {
  path: string
  name?: string
  size_bytes?: number
  content: string
  truncated: boolean
  error?: string | null
}

export type SystemLogLine = {
  timestamp: string
  message: string
  /** Present when the daemon tagged the line with a job_id */
  job_id?: string
  issue_key?: string
}

export type TaskDetail = {
  issue_key: string
  summary: string
  description: string
  /** Jira board column name when live fetch succeeded */
  jira_status?: string | null
  /** True when description/summary came from Jira REST this request */
  jira_live?: boolean
  status: string
  progress_percentage: number
  live: boolean
  can_cancel: boolean
  /** True when status is plan_ready and work can be started from the dashboard. */
  can_start?: boolean
  workflow_type?: string | null
  plan_path?: string | null
  current_task_id?: string | null
  current_opencode_session_id?: string | null
  task_ids?: string[]
  job_ids?: string[]
  current_job_id?: string | null
  opencode_session_ids?: string[]
  opencode_sessions?: Array<Record<string, unknown>>
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
  feature_branch?: string | null
  merge_request_url?: string | null
  /** All commit/MR deliveries across runs for this issue. */
  git_deliveries?: GitDelivery[]
  retry_history: Array<Record<string, unknown>>
  prompts: {
    workflow_type?: string
    agent?: string
    /** Captured *.prompt.txt files — what was actually sent to the agent. */
    captured_prompt_files?: TextArtifact[]
    error?: string
  }
  session_logs: TextArtifact[]
  system_logs: SystemLogLine[]
  jobs?: JobItem[]
  server_time: string
}
