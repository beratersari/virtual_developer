export type ScheduleMode = 'existing' | 'new' | 'mr' | 'pr'
export type ScheduleTracker = 'jira' | 'azure'

const MODES: Record<string, ScheduleMode> = {
  existing: 'existing',
  new: 'new',
  mr: 'mr',
  pr: 'pr',
}

export function parseSchedulePath(
  mode: string | undefined,
  tracker: string | undefined,
): { mode: ScheduleMode; tracker: ScheduleTracker } | null {
  const a = (mode || '').trim().toLowerCase()
  const b = (tracker || '').trim().toLowerCase()
  if (!a && !b) return { mode: 'existing', tracker: 'jira' }
  if ((a === 'azure' || a === 'jira') && !b) {
    return { mode: 'existing', tracker: a }
  }
  const parsed = MODES[a]
  if (!parsed) return null
  if (!b || b === 'jira') return { mode: parsed, tracker: 'jira' }
  if (b === 'azure' && (parsed === 'existing' || parsed === 'new')) {
    return { mode: parsed, tracker: 'azure' }
  }
  return null
}

export function schedulePath(mode: ScheduleMode, tracker: ScheduleTracker = 'jira'): string {
  if (mode === 'mr' || mode === 'pr') return `/scheduled/${mode}`
  if (mode === 'existing') return `/scheduled/${tracker}`
  return `/scheduled/${mode}/${tracker}`
}

/** Address the router is on, before a Jira suffix is filled in. */
export function scheduleHere(mode: string | undefined, tracker: string | undefined): string {
  const a = (mode || '').trim()
  const b = (tracker || '').trim()
  if (!a) return '/scheduled'
  if (!b) return `/scheduled/${a}`
  return `/scheduled/${a}/${b}`
}

/** URL the page should show. Missing or invalid Jira forms become /jira. */
export function canonicalSchedulePath(
  mode: string | undefined,
  tracker: string | undefined,
): string {
  const parsed = parseSchedulePath(mode, tracker)
  if (!parsed) return schedulePath('existing', 'jira')
  return schedulePath(parsed.mode, parsed.tracker)
}
