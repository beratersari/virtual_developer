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
}

export type DashboardPayload = {
  type: string
  meta: Meta
  tasks: TasksPayload
  poll: PollPayload
  settings: SettingsPayload
}
