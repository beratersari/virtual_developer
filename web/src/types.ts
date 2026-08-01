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

export type SettingsPayload = {
  jira_host: string
  jira_board_id: string
  jira_projects: string
  poll_interval_seconds: number
  trigger_labels: string
  trigger_on_assignment: boolean
  auto_start_plans: boolean
  max_concurrent_jobs: number
  default_branch: string
  dashboard_host: string
  dashboard_port: number
  jira_token_configured: boolean
  gitlab_pat_configured: boolean
  jira_email_configured: boolean
  /** Runtime DEFAULT_MODEL — inventory list is GET /api/models, not settings */
  default_model: string
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
}

export type JobsPayload = {
  jobs: JobItem[]
  total: number
  issue_key_filter?: string | null
  server_time: string
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
