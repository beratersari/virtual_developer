/** Thin HTTP helpers — display layer only. */

import type {
  DashboardPayload,
  JobsPayload,
  JiraIssueTypesPayload,
  ModelsPayload,
  ScheduleCreateBody,
  ScheduleItem,
  SchedulePreview,
  SchedulesPayload,
  SettingsPayload,
  TaskDetail,
} from './types'

/**
 * FastAPI may return ``detail`` as a string, an object, or a list of
 * validation errors. Never pass a raw object into ``new Error(...)`` — that
 * becomes the useless message ``[object Object]``.
 */
export function formatApiError(
  detail: unknown,
  fallback: string,
): string {
  if (detail == null || detail === '') return fallback
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item
      if (item && typeof item === 'object') {
        const o = item as { msg?: string; loc?: unknown; type?: string }
        const where = Array.isArray(o.loc)
          ? o.loc.filter((x) => x !== 'body').join('.')
          : ''
        const msg = o.msg || o.type || JSON.stringify(item)
        return where ? `${where}: ${msg}` : String(msg)
      }
      return String(item)
    })
    const joined = parts.filter(Boolean).join('; ')
    return joined || fallback
  }
  if (typeof detail === 'object') {
    const o = detail as { message?: string; error?: string; msg?: string }
    if (o.message) return String(o.message)
    if (o.error) return String(o.error)
    if (o.msg) return String(o.msg)
    try {
      return JSON.stringify(detail)
    } catch {
      return fallback
    }
  }
  return String(detail)
}

export async function fetchDashboard(): Promise<DashboardPayload> {
  const res = await fetch('/api/dashboard')
  if (!res.ok) {
    throw new Error(`Dashboard API error: ${res.status}`)
  }
  return res.json()
}

export async function fetchJobs(opts?: {
  issueKey?: string
  page?: number
  pageSize?: number
}): Promise<JobsPayload> {
  const params = new URLSearchParams()
  const key = opts?.issueKey?.trim()
  if (key) params.set('issue_key', key)
  if (opts?.page != null) params.set('page', String(opts.page))
  if (opts?.pageSize != null) params.set('page_size', String(opts.pageSize))
  const q = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`/api/jobs${q}`)
  if (!res.ok) {
    throw new Error(`Jobs API error: ${res.status}`)
  }
  return res.json()
}

export async function fetchTaskDetail(issueKey: string): Promise<TaskDetail> {
  const res = await fetch(`/api/tasks/${encodeURIComponent(issueKey)}`)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(formatApiError(body?.detail, `Task detail failed: ${res.status}`))
  }
  return res.json()
}

/** Single job from store + optional issue payload + job-scoped system logs. */
export async function fetchJobById(
  jobId: string,
): Promise<{
  job: Record<string, unknown>
  issue: TaskDetail | null
  system_logs?: import('./types').SystemLogLine[]
  server_time?: string
}> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(formatApiError(body?.detail, `Job detail failed: ${res.status}`))
  }
  return res.json()
}

/** Permanently delete a historical job (and optional session/prompt artifacts). */
export async function deleteJob(
  jobId: string,
  opts?: { deleteArtifacts?: boolean },
): Promise<{
  ok: boolean
  job_id: string
  issue_key?: string
  store_deleted?: boolean
  artifacts_deleted?: string[]
  message?: string
}> {
  const params = new URLSearchParams()
  if (opts?.deleteArtifacts === false) {
    params.set('delete_artifacts', 'false')
  }
  const q = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}${q}`, {
    method: 'DELETE',
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(body?.detail, `Delete failed: ${res.status}`))
  }
  return body
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

/** Permanently delete multiple historical jobs (partial success allowed). */
export async function deleteJobs(
  jobIds: string[],
  opts?: { deleteArtifacts?: boolean },
): Promise<BulkDeleteJobsResult> {
  const res = await fetch('/api/jobs/bulk-delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      job_ids: jobIds,
      delete_artifacts: opts?.deleteArtifacts !== false,
    }),
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(body?.detail, `Bulk delete failed: ${res.status}`))
  }
  return body as BulkDeleteJobsResult
}

export async function cancelTask(issueKey: string): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch(`/api/tasks/${encodeURIComponent(issueKey)}/cancel`, {
    method: 'POST',
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(body?.detail, `Cancel failed: ${res.status}`))
  }
  return body
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

/** Test GitLab host PAT — lists user + projects the token can see. */
export async function testGitlabConnection(body: {
  host: string
  pat?: string
  max_projects?: number
}): Promise<GitlabConnectionTestResult> {
  const res = await fetch('/api/settings/gitlab/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: body.host,
      pat: body.pat?.trim() || undefined,
      max_projects: body.max_projects ?? 25,
    }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(data?.detail, `GitLab test failed: ${res.status}`))
  }
  return data as GitlabConnectionTestResult
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

/** Test Jira connection — /myself + projects.
 *  email + token → Basic (Cloud); token only → Bearer (prod PAT).
 */
export async function testJiraConnection(body: {
  host?: string
  email?: string
  api_token?: string
  max_projects?: number
}): Promise<JiraConnectionTestResult> {
  const res = await fetch('/api/settings/jira/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      host: body.host?.trim() || undefined,
      email: body.email?.trim() || undefined,
      api_token: body.api_token?.trim() || undefined,
      max_projects: body.max_projects ?? 25,
    }),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(data?.detail, `Jira test failed: ${res.status}`))
  }
  return data as JiraConnectionTestResult
}

export async function patchSettings(
  body: Partial<
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
      | 'default_model'
      | 'gitlab_allowed_hosts'
    >
  > & {
    /** Write-only; omit to keep current token */
    jira_api_token?: string
    /** Write-only legacy single PAT */
    gitlab_pat?: string
    /** Full list of host credentials (preferred). Empty pat keeps existing. */
    gitlab_credentials?: { host: string; pat?: string }[]
  },
): Promise<SettingsPayload> {
  const res = await fetch('/api/settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = body?.detail
    let msg = `Settings update failed: ${res.status}`
    if (typeof detail === 'string') msg = detail
    else if (Array.isArray(detail)) {
      msg = detail
        .map((d: { msg?: string } | string) =>
          typeof d === 'string' ? d : d?.msg || JSON.stringify(d),
        )
        .join('; ')
    }
    throw new Error(msg)
  }
  return res.json()
}

/** Fetch OpenCode model inventory from the backend (no client-side discovery). */
export async function fetchModels(refresh = false): Promise<ModelsPayload> {
  const q = refresh ? '?refresh=true' : ''
  const res = await fetch(`/api/models${q}`)
  if (!res.ok) {
    throw new Error(`Models API error: ${res.status}`)
  }
  return res.json()
}

export function dashboardWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}

/** Creatable Jira issue types for the configured (or given) project. */
export async function fetchIssueTypes(
  projectKey?: string,
): Promise<JiraIssueTypesPayload> {
  const params = new URLSearchParams()
  if (projectKey?.trim()) params.set('project_key', projectKey.trim())
  const q = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`/api/jira/issue-types${q}`)
  if (!res.ok) {
    throw new Error(`Issue types API error: ${res.status}`)
  }
  return res.json()
}

export async function fetchSchedules(opts?: {
  status?: string
}): Promise<SchedulesPayload> {
  const params = new URLSearchParams()
  if (opts?.status) params.set('status', opts.status)
  const q = params.toString() ? `?${params.toString()}` : ''
  const res = await fetch(`/api/schedules${q}`)
  if (!res.ok) {
    throw new Error(`Schedules API error: ${res.status}`)
  }
  return res.json()
}

export async function createSchedule(
  body: ScheduleCreateBody,
): Promise<{ ok: boolean; schedule: ScheduleItem; issue_key?: string }> {
  // Always send explicit source_branch_mode; for issue_key omit empty custom branch
  const payload: Record<string, unknown> = {
    title: body.title,
    description: body.description ?? '',
    repository_url: body.repository_url,
    target_branch: body.target_branch,
    mode: body.mode,
    scheduled_at: body.scheduled_at,
    source_branch_mode: body.source_branch_mode || 'custom',
  }
  if (body.project_key) payload.project_key = body.project_key
  if (body.issue_type) payload.issue_type = body.issue_type
  if (body.source_branch_mode === 'issue_key') {
    payload.source_branch = ''
  } else {
    payload.source_branch = body.source_branch ?? ''
  }

  const res = await fetch('/api/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(
      formatApiError(data?.detail, `Create schedule failed: ${res.status}`),
    )
  }
  return data
}

export async function cancelSchedule(
  scheduleId: string,
): Promise<{ ok: boolean; message?: string; schedule?: ScheduleItem }> {
  const res = await fetch(
    `/api/schedules/${encodeURIComponent(scheduleId)}/cancel`,
    { method: 'POST' },
  )
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(
      formatApiError(data?.detail, `Cancel schedule failed: ${res.status}`),
    )
  }
  return data
}

/** Load existing issue + validate {params} template (hard-fail on invalid). */
export async function previewScheduleIssue(
  issueKey: string,
): Promise<SchedulePreview> {
  const params = new URLSearchParams({ issue_key: issueKey.trim() })
  const res = await fetch(`/api/schedules/preview?${params.toString()}`)
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(data?.detail, `Preview failed: ${res.status}`))
  }
  return data as SchedulePreview
}

/** Schedule an existing Jira issue for later dispatch. */
export async function scheduleExistingIssue(body: {
  issue_key: string
  scheduled_at: string
}): Promise<{ ok: boolean; schedule: ScheduleItem; issue_key?: string }> {
  const res = await fetch('/api/schedules/from-issue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(formatApiError(data?.detail, `Schedule existing failed: ${res.status}`))
  }
  return data
}
