/** Presentation DTOs — mirror `src/dashboard/schemas.py`. No business rules. */

export type Meta = {
  version: string
  server_time: string
  app_name: string
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

export type ModelOption = {
  id: string
  name: string
  provider: string
  source: string
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
  pat_configured: boolean
}

export type ProjectRepository = {
  label: string
  url: string
  target_branch?: string
  source_branch?: string
}

export type SettingsPayload = {
  jira_host: string
  jira_board_id: string
  jira_projects: string
  poll_interval_seconds: number
  trigger_labels: string
  trigger_on_assignment: boolean
  max_concurrent_jobs: number
  agent_task_timeout_seconds: number
  agent_task_max_retries: number
  agent_task_max_incomplete_retries: number
  default_branch: string
  dashboard_host: string
  dashboard_port: number
  jira_token_configured: boolean
  gitlab_pat_configured: boolean
  jira_email_configured: boolean
  jira_email?: string
  gitlab_allowed_hosts?: string
  gitlab_credentials?: GitlabHostCredential[]
  default_model: string
  agent_backend?: string
  codex_base_url?: string
  codex_wire_api?: string
  codex_api_key_configured?: boolean
  gitlab_webhook_enabled?: boolean
  gitlab_bot_mentions?: string
  gitlab_webhook_secret_configured?: boolean
  gitlab_webhook_path?: string
  project_repositories?: ProjectRepository[]
}

export type GitlabHostCredentialDraft = {
  host: string
  pat: string
  pat_configured: boolean
  original_host?: string
}

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

export type JobRetryAttempt = {
  attempt_number: number
  label: string
  reason: string
  delay_seconds: number
  failed_session_log_path?: string | null
  error_message?: string | null
  return_code?: number | null
  opencode_session_id?: string | null
  task_id?: string | null
  timestamp?: string | null
}

export type JobItem = {
  job_id: string
  issue_key: string
  summary: string
  description?: string
  workflow_type: string
  agent: string
  /** Worker model id used for this run */
  model?: string | null
  /** opencode | codex (empty = infer on the client) */
  backend?: string
  status: string
  task_id?: string | null
  task_ids?: string[]
  opencode_session_id?: string | null
  opencode_session_ids?: string[]
  session_log_path?: string | null
  session_log_paths?: string[]
  prompt_path?: string | null
  prompt_paths?: string[]
  retry_attempts?: JobRetryAttempt[]
  progress_percentage: number
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
  updated_at?: string | null
  live: boolean
  feature_branch?: string | null
  merge_request_url?: string | null
  commit_sha?: string | null
  commit_subject?: string | null
  commit_url?: string | null
  delivery_status?: string | null
  delivery_note?: string | null
  working_directory?: string | null
  source?: string
  gitlab_project?: string | null
  gitlab_mr_iid?: number | null
}

export type QueueItem = {
  queue_id: string
  status: string
  source: string
  issue_key: string
  summary: string
  message: string
  repository_url?: string
  source_branch?: string
  work_branch?: string
  target_branch?: string
  lock_key?: string
  job_id?: string | null
  merge_request_url?: string
  gitlab_note_id?: string
  error_message?: string | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export type QueuePayload = {
  items: QueueItem[]
  queued_count: number
  running_count: number
  total: number
  server_time: string
}

export type JobsPayload = {
  jobs: JobItem[]
  total: number
  page?: number
  page_size?: number
  issue_key_filter?: string | null
  server_time: string
}

export type OpencodeSessionBind = {
  bind_id: string
  repository_url: string
  repository_key?: string
  branch: string
  target_branch?: string
  session_id: string
  issue_key?: string
  job_id?: string | null
  working_directory?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export type OpencodeSessionsPayload = {
  sessions: OpencodeSessionBind[]
  total: number
  server_time?: string
}

export type ScheduleItem = {
  schedule_id: string
  title: string
  description?: string
  repository_url: string
  source_branch: string
  target_branch: string
  mode: string
  model?: string
  backend?: string
  issue_type?: string
  scheduled_at: string
  status: string
  issue_key: string
  project_key?: string
  label?: string
  source?: string
  created_at?: string | null
  updated_at?: string | null
  dispatched_at?: string | null
  error_message?: string | null
}

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
  model?: string
  backend?: string
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
  source_branch?: string
  target_branch: string
  mode: 'plan' | 'build'
  scheduled_at: string
  project_key?: string
  issue_type?: string
  source_branch_mode?: 'custom' | 'issue_key'
  dispatch_now?: boolean
  model?: string
  backend?: string
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

export type ChatPart = {
  id: string
  type: string
  created_at?: string | null
  text?: string
  truncated?: boolean
  tool?: string
  call_id?: string | null
  status?: string
  title?: string
  input?: Record<string, unknown>
  output?: string
  reason?: string
  auto?: boolean
}

export type ChatMessage = {
  id: string
  session_id: string
  role: string
  raw_role?: string
  finish?: string | null
  summary?: boolean | Record<string, unknown> | null
  agent?: string | null
  created_at?: string | null
  parts: ChatPart[]
}

export type ChatSessionInfo = {
  session_id: string
  title?: string | null
  directory?: string | null
  message_count: number
  truncated?: boolean
  error?: string | null
}

export type JobChatPayload = {
  job_id: string
  session_ids: string[]
  sessions: ChatSessionInfo[]
  messages: ChatMessage[]
  server_time?: string
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
  job_id?: string
  issue_key?: string
}

export type TaskDetail = {
  issue_key: string
  summary: string
  description: string
  jira_status?: string | null
  jira_live?: boolean
  status: string
  progress_percentage: number
  live: boolean
  can_cancel: boolean
  can_start?: boolean
  workflow_type?: string | null
  plan_path?: string | null
  current_task_id?: string | null
  current_opencode_session_id?: string | null
  task_ids?: string[]
  job_ids?: string[]
  current_job_id?: string | null
  opencode_session_ids?: string[]
  opencode_sessions?: Array<{ id?: string; [key: string]: unknown }>
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
  feature_branch?: string | null
  merge_request_url?: string | null
  git_deliveries?: GitDelivery[]
  retry_history: Array<Record<string, unknown>>
  prompts: {
    workflow_type?: string
    agent?: string
    captured_prompt_files?: TextArtifact[]
    error?: string
  }
  session_logs: TextArtifact[]
  system_logs: SystemLogLine[]
  jobs?: JobItem[]
  server_time: string
}

export type JobDetailResponse = {
  job: JobItem
  issue: TaskDetail | null
  system_logs?: SystemLogLine[]
  server_time?: string
}

export type BulkDeleteJobsResult = {
  ok: boolean
  deleted: string[]
  failed: { job_id: string; error: string }[]
  deleted_count: number
  failed_count: number
  message?: string
  server_time?: string
}

export type GitlabConnectionTestResult = {
  ok: boolean
  host?: string
  error?: string
  message?: string
  user?: { id?: number | null; username?: string; name?: string | null }
  projects?: {
    id?: number
    name?: string
    path_with_namespace?: string
    web_url?: string
    visibility?: string
  }[]
  project_count?: number
  projects_error?: string | null
  http_status?: number
  server_time?: string
}

export type JiraConnectionTestResult = {
  ok: boolean
  host?: string
  error?: string
  message?: string
  auth_mode?: string
  is_cloud?: boolean
  user?: {
    display_name?: string
    account?: string
    email?: string | null
  }
  projects?: {
    id?: string | number
    key?: string
    name?: string
    project_type?: string
    style?: string
  }[]
  project_count?: number
  projects_error?: string | null
  http_status?: number
  server_time?: string
}

export type SettingsPatch = Partial<
  Pick<
    SettingsPayload,
    | 'jira_host'
    | 'jira_email'
    | 'jira_board_id'
    | 'poll_interval_seconds'
    | 'trigger_labels'
    | 'trigger_on_assignment'
    | 'max_concurrent_jobs'
    | 'agent_task_timeout_seconds'
    | 'agent_task_max_retries'
    | 'agent_task_max_incomplete_retries'
    | 'default_model'
    | 'agent_backend'
    | 'codex_base_url'
    | 'codex_wire_api'
    | 'gitlab_allowed_hosts'
    | 'project_repositories'
  >
> & {
  jira_api_token?: string
  gitlab_pat?: string
  gitlab_credentials?: { host: string; pat?: string; previous_host?: string }[]
  codex_api_key?: string
}
