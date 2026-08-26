import type {
  BulkDeleteJobsResult,
  GitlabConnectionTestResult,
  JobChatPayload,
  JobDetailResponse,
  JobItem,
  JobsPayload,
  OpencodeSessionsPayload,
  JiraConnectionTestResult,
  JiraIssueTypesPayload,
  ModelsPayload,
  PollPayload,
  ScheduleCreateBody,
  ScheduleItem,
  SchedulePreview,
  SchedulesPayload,
  SettingsPatch,
  SettingsPayload,
  StoragePayload,
  TaskDetail,
  QueuePayload,
} from './types'

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** FastAPI `detail` may be a string, object, or validation list. */
export function formatApiError(detail: unknown, fallback: string): string {
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
    return parts.filter(Boolean).join('; ') || fallback
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(path, { ...init, headers })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new ApiError(
      formatApiError(
        (body as { detail?: unknown })?.detail,
        `Request failed: ${res.status}`,
      ),
      res.status,
    )
  }
  return body as T
}

export function normalizeJob(raw: Partial<JobItem> | Record<string, unknown>): JobItem {
  const j = raw as JobItem
  return {
    job_id: String(j.job_id || ''),
    issue_key: String(j.issue_key || ''),
    summary: j.summary || '',
    description: j.description || '',
    workflow_type: j.workflow_type || 'execution',
    agent: j.agent || '',
    model: j.model ?? null,
    backend: typeof j.backend === 'string' ? j.backend : '',
    status: j.status || 'unknown',
    task_id: j.task_id ?? null,
    task_ids: j.task_ids || (j.task_id ? [j.task_id] : []),
    opencode_session_id: j.opencode_session_id ?? null,
    opencode_session_ids: j.opencode_session_ids || [],
    session_log_path: j.session_log_path ?? null,
    session_log_paths: j.session_log_paths || [],
    prompt_path: j.prompt_path ?? null,
    prompt_paths: j.prompt_paths || [],
    retry_attempts: j.retry_attempts || [],
    error_message: j.error_message ?? null,
    started_at: j.started_at ?? null,
    completed_at: j.completed_at ?? null,
    updated_at: j.updated_at ?? null,
    live: Boolean(j.live),
    feature_branch: j.feature_branch ?? null,
    merge_request_url: j.merge_request_url ?? null,
    commit_sha: j.commit_sha ?? null,
    commit_subject: j.commit_subject ?? null,
    commit_url: j.commit_url ?? null,
    delivery_status: j.delivery_status ?? null,
    delivery_note: j.delivery_note ?? null,
    working_directory: j.working_directory ?? null,
    source: j.source || 'jira',
    gitlab_project: j.gitlab_project ?? null,
    gitlab_mr_iid: j.gitlab_mr_iid ?? null,
  }
}

export function dashboardWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}

export function fetchMeta() {
  return request<{ version: string; server_time: string; app_name: string }>('/api/meta')
}

export function fetchPoll() {
  return request<PollPayload>('/api/poll')
}

export function fetchQueue(opts?: { status?: string; limit?: number }) {
  const params = new URLSearchParams()
  if (opts?.status) params.set('status', opts.status)
  if (opts?.limit != null) params.set('limit', String(opts.limit))
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<QueuePayload>(`/api/queue${q}`)
}

export function cancelQueueItem(queueId: string) {
  return request<{ ok: boolean; queue_id: string; status: string }>(
    `/api/queue/${encodeURIComponent(queueId)}`,
    { method: 'DELETE' },
  )
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
  const payload = await request<JobsPayload>(`/api/jobs${q}`)
  return {
    ...payload,
    jobs: (payload.jobs || []).map(normalizeJob),
  }
}

export async function fetchJobById(jobId: string): Promise<JobDetailResponse> {
  const body = await request<JobDetailResponse>(
    `/api/jobs/${encodeURIComponent(jobId)}`,
  )
  return {
    ...body,
    job: normalizeJob(body.job),
    issue: body.issue
      ? {
          ...body.issue,
          jobs: (body.issue.jobs || []).map(normalizeJob),
        }
      : null,
  }
}

export async function fetchJobArtifacts(jobId: string): Promise<{
  job_id: string
  prompts: import('./types').TextArtifact[]
  session_logs: import('./types').TextArtifact[]
}> {
  return request(`/api/jobs/${encodeURIComponent(jobId)}/artifacts`)
}

export function fetchJobChat(jobId: string) {
  return request<JobChatPayload>(`/api/jobs/${encodeURIComponent(jobId)}/chat`)
}

export function fetchTaskDetail(
  issueKey: string,
  opts?: { live?: boolean; artifacts?: boolean },
) {
  const params = new URLSearchParams()
  if (opts?.live) params.set('live', 'true')
  if (opts?.artifacts) params.set('artifacts', 'true')
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<TaskDetail>(
    `/api/tasks/${encodeURIComponent(issueKey)}${q}`,
  ).then((d) => ({
    ...d,
    jobs: (d.jobs || []).map(normalizeJob),
  }))
}

export function cancelTask(issueKey: string) {
  return request<{ ok: boolean; message?: string }>(
    `/api/tasks/${encodeURIComponent(issueKey)}/cancel`,
    { method: 'POST' },
  )
}

export function deleteJob(jobId: string, opts?: { deleteArtifacts?: boolean }) {
  const params = new URLSearchParams()
  if (opts?.deleteArtifacts === false) params.set('delete_artifacts', 'false')
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<{
    ok: boolean
    job_id: string
    issue_key?: string
    store_deleted?: boolean
    artifacts_deleted?: string[]
    message?: string
  }>(`/api/jobs/${encodeURIComponent(jobId)}${q}`, { method: 'DELETE' })
}

export function deleteJobs(jobIds: string[], opts?: { deleteArtifacts?: boolean }) {
  return request<BulkDeleteJobsResult>('/api/jobs/bulk-delete', {
    method: 'POST',
    body: JSON.stringify({
      job_ids: jobIds,
      delete_artifacts: opts?.deleteArtifacts !== false,
    }),
  })
}

export function fetchSettings() {
  return request<SettingsPayload>('/api/settings')
}

export function patchSettings(body: SettingsPatch) {
  return request<SettingsPayload>('/api/settings', {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

export function fetchModels(refresh = false, backend?: string) {
  const params = new URLSearchParams()
  if (refresh) params.set('refresh', 'true')
  if (backend) params.set('backend', backend)
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<ModelsPayload>(`/api/models${q}`)
}

export function testGitlabConnection(body: {
  host: string
  pat?: string
  max_projects?: number
}) {
  return request<GitlabConnectionTestResult>('/api/settings/gitlab/test', {
    method: 'POST',
    body: JSON.stringify({
      host: body.host,
      pat: body.pat?.trim() || undefined,
      max_projects: body.max_projects ?? 25,
    }),
  })
}

export function testJiraConnection(body: {
  host?: string
  email?: string
  api_token?: string
  max_projects?: number
}) {
  return request<JiraConnectionTestResult>('/api/settings/jira/test', {
    method: 'POST',
    body: JSON.stringify({
      host: body.host?.trim() || undefined,
      email: body.email?.trim() || undefined,
      api_token: body.api_token?.trim() || undefined,
      max_projects: body.max_projects ?? 25,
    }),
  })
}

export function fetchIssueTypes(projectKey?: string) {
  const params = new URLSearchParams()
  if (projectKey?.trim()) params.set('project_key', projectKey.trim())
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<JiraIssueTypesPayload>(`/api/jira/issue-types${q}`)
}

export function fetchStorage() {
  return request<StoragePayload>('/api/storage')
}

export function deleteTempFolder(name: string) {
  return request<{ ok: boolean; name: string; path?: string }>(
    '/api/storage/delete',
    {
      method: 'POST',
      body: JSON.stringify({ name }),
    },
  )
}

export function fetchOpencodeSessions() {
  return request<OpencodeSessionsPayload>('/api/opencode-sessions')
}

export function resetOpencodeSession(bindId: string) {
  return request<{ ok: boolean; bind_id: string; message?: string }>(
    `/api/opencode-sessions/${encodeURIComponent(bindId)}`,
    { method: 'DELETE' },
  )
}

export function fetchSchedules(opts?: { status?: string }) {
  const params = new URLSearchParams()
  if (opts?.status) params.set('status', opts.status)
  const q = params.toString() ? `?${params.toString()}` : ''
  return request<SchedulesPayload>(`/api/schedules${q}`)
}

export function createSchedule(body: ScheduleCreateBody) {
  const payload: Record<string, unknown> = {
    title: body.title,
    description: body.description ?? '',
    repository_url: body.repository_url,
    target_branch: body.target_branch,
    mode: body.mode,
    scheduled_at: body.scheduled_at,
    source_branch_mode: body.source_branch_mode || 'custom',
    source_branch:
      body.source_branch_mode === 'issue_key' ? '' : (body.source_branch ?? ''),
  }
  if (body.project_key) payload.project_key = body.project_key
  if (body.issue_type) payload.issue_type = body.issue_type
  if (body.dispatch_now) payload.dispatch_now = true
  if (body.model) payload.model = body.model
  if (body.backend) payload.backend = body.backend
  return request<{
    ok: boolean
    schedule: ScheduleItem
    issue_key?: string
    dispatched?: boolean
    dispatch_error?: string
  }>(
    '/api/schedules',
    { method: 'POST', body: JSON.stringify(payload) },
  )
}

export function dispatchSchedule(scheduleId: string) {
  return request<{
    ok: boolean
    message?: string
    schedule?: ScheduleItem
    issue_key?: string
  }>(
    `/api/schedules/${encodeURIComponent(scheduleId)}/dispatch`,
    { method: 'POST' },
  )
}

export function cancelSchedule(scheduleId: string) {
  return request<{ ok: boolean; message?: string; schedule?: ScheduleItem }>(
    `/api/schedules/${encodeURIComponent(scheduleId)}/cancel`,
    { method: 'POST' },
  )
}

export function previewScheduleIssue(issueKey: string) {
  const params = new URLSearchParams({ issue_key: issueKey.trim() })
  return request<SchedulePreview>(`/api/schedules/preview?${params.toString()}`)
}

export async function downloadIssueReport(body: {
  kind: 'general' | 'job'
  note: string
  job_id?: string
}): Promise<string> {
  const res = await fetch('/api/reports', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      kind: body.kind,
      note: body.note,
      job_id: body.kind === 'job' ? body.job_id : undefined,
    }),
  })
  if (!res.ok) {
    const payload = await res.json().catch(() => ({}))
    throw new ApiError(
      formatApiError(
        (payload as { detail?: unknown })?.detail,
        `Request failed: ${res.status}`,
      ),
      res.status,
    )
  }
  const blob = await res.blob()
  const filename = filenameFromDisposition(
    res.headers.get('Content-Disposition'),
    body.kind === 'job'
      ? `yaver-report-${body.job_id || 'job'}.zip`
      : 'yaver-report-general.zip',
  )
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 2_000)
  }
  return filename
}

function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header)
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim())
    } catch {
      return star[1].trim()
    }
  }
  const plain = /filename="([^"]+)"/i.exec(header) || /filename=([^;]+)/i.exec(header)
  return plain?.[1]?.trim() || fallback
}

export function scheduleExistingIssue(body: {
  issue_key: string
  scheduled_at: string
  dispatch_now?: boolean
  model?: string
  backend?: string
}) {
  return request<{
    ok: boolean
    schedule: ScheduleItem
    issue_key?: string
    dispatched?: boolean
    dispatch_error?: string
  }>(
    '/api/schedules/from-issue',
    { method: 'POST', body: JSON.stringify(body) },
  )
}
