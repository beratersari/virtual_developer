/** Settings number fields. A blank input is not zero. */

export const SETTINGS_NUMBER_FIELDS = {
  poll_interval_seconds: { label: 'Poll interval', min: 5, max: 3600 },
  max_concurrent_jobs: { label: 'Max concurrent jobs', min: 1, max: 64 },
  temp_clone_max_age_days: { label: 'Clone age', min: 0, max: 3650 },
  agent_task_timeout_seconds: { label: 'Agent timeout', min: 30, max: 86400 },
  agent_task_max_retries: { label: 'Error retries', min: 0, max: 64 },
  agent_task_max_incomplete_retries: {
    label: 'Incomplete retries',
    min: 0,
    max: 256,
  },
} as const

export type SettingsNumberKey = keyof typeof SETTINGS_NUMBER_FIELDS

export function parseSettingsNumber(raw: string): number | null {
  const text = String(raw ?? '').trim()
  if (!text) return null
  const value = Number(text)
  if (!Number.isFinite(value)) return null
  return value
}

export function settingsNumberProblem(
  key: SettingsNumberKey,
  value: number | null,
): string | null {
  const field = SETTINGS_NUMBER_FIELDS[key]
  if (value == null || !Number.isFinite(value)) {
    return `${field.label} needs a number.`
  }
  if (value < field.min || value > field.max) {
    return `${field.label} must be between ${field.min} and ${field.max}.`
  }
  return null
}

export function requireSettingsNumber(
  key: SettingsNumberKey,
  value: number | null,
): number {
  const problem = settingsNumberProblem(key, value)
  if (problem || value == null) {
    throw new Error(problem || 'Enter a number.')
  }
  return value
}
