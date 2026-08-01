/** Thin HTTP helpers — display layer only. */

import type { DashboardPayload, SettingsPayload } from './types'

export async function fetchDashboard(): Promise<DashboardPayload> {
  const res = await fetch('/api/dashboard')
  if (!res.ok) {
    throw new Error(`Dashboard API error: ${res.status}`)
  }
  return res.json()
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
