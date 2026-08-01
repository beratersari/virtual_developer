/** Thin HTTP helpers — display layer only. */

import type { DashboardPayload, JobsPayload, SettingsPayload, TaskDetail } from './types'

export async function fetchDashboard(): Promise<DashboardPayload> {
  const res = await fetch('/api/dashboard')
  if (!res.ok) {
    throw new Error(`Dashboard API error: ${res.status}`)
  }
  return res.json()
}

export async function fetchJobs(issueKey?: string): Promise<JobsPayload> {
  const q = issueKey?.trim()
    ? `?issue_key=${encodeURIComponent(issueKey.trim())}`
    : ''
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

export async function patchSettings(
  body: Partial<SettingsPayload>,
): Promise<SettingsPayload> {
  const res = await fetch('/api/settings', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    throw new Error(`Settings update failed: ${res.status}`)
  }
  return res.json()
}

export function dashboardWsUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}/ws`
}
