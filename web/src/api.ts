/** Thin HTTP helpers — display layer only. */

import type {
  DashboardPayload,
  JobsPayload,
  ModelsPayload,
  SettingsPayload,
  TaskDetail,
} from './types'

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
    throw new Error(body.detail || `Task detail failed: ${res.status}`)
  }
  return res.json()
}

export async function cancelTask(issueKey: string): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch(`/api/tasks/${encodeURIComponent(issueKey)}/cancel`, {
    method: 'POST',
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.detail || `Cancel failed: ${res.status}`)
  }
  return body
}

export async function startTask(issueKey: string): Promise<{ ok: boolean; message?: string }> {
  const res = await fetch(`/api/tasks/${encodeURIComponent(issueKey)}/start`, {
    method: 'POST',
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    throw new Error(body.detail || `Start failed: ${res.status}`)
  }
  return body
}

export async function patchSettings(
  body: Partial<
    Pick<
      SettingsPayload,
      | 'jira_board_id'
      | 'poll_interval_seconds'
      | 'trigger_labels'
      | 'trigger_on_assignment'
      | 'auto_start_plans'
      | 'max_concurrent_jobs'
      | 'default_model'
    >
  >,
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
