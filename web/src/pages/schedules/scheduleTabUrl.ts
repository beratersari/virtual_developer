import { listPageFromSegment, withListPage } from '../../util/listPageUrl'

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
  pageRaw?: string,
): { mode: ScheduleMode; tracker: ScheduleTracker; page: number } | null {
  const bits = [mode, tracker, pageRaw].map((part) => (part || '').trim()).filter(Boolean)
  let page = 1
  const tail = listPageFromSegment(bits[bits.length - 1])
  if (tail) {
    page = tail
    bits.pop()
  }
  const a = (bits[0] || '').toLowerCase()
  const b = (bits[1] || '').toLowerCase()
  if (bits.length > 2) return null
  const parsedMode = ((): { mode: ScheduleMode; tracker: ScheduleTracker } | null => {
    if (!a && !b) return { mode: 'existing', tracker: 'jira' }
    if ((a === 'azure' || a === 'jira') && !b) return { mode: 'existing', tracker: a }
    const parsed = MODES[a]
    if (!parsed) return null
    if (!b || b === 'jira') return { mode: parsed, tracker: 'jira' }
    if (b === 'azure' && (parsed === 'existing' || parsed === 'new')) {
      return { mode: parsed, tracker: 'azure' }
    }
    return null
  })()
  if (!parsedMode) return null
  return { ...parsedMode, page }
}

export function schedulePath(mode: ScheduleMode, tracker: ScheduleTracker = 'jira'): string {
  if (mode === 'mr' || mode === 'pr') return `/scheduled/${mode}`
  if (mode === 'existing') return `/scheduled/${tracker}`
  return `/scheduled/${mode}/${tracker}`
}

/** Address the router is on, before a Jira suffix is filled in. */
export function scheduleHere(
  mode: string | undefined,
  tracker: string | undefined,
  pageRaw?: string,
): string {
  const parts = [mode, tracker, pageRaw].map((part) => (part || '').trim()).filter(Boolean)
  if (!parts.length) return '/scheduled'
  return `/scheduled/${parts.join('/')}`
}

/** URL the page should show. Missing or invalid Jira forms become /jira. */
export function canonicalSchedulePath(
  mode: string | undefined,
  tracker: string | undefined,
  pageRaw?: string,
): string {
  const parsed = parseSchedulePath(mode, tracker, pageRaw)
  if (!parsed) return schedulePath('existing', 'jira')
  return withListPage(schedulePath(parsed.mode, parsed.tracker), parsed.page)
}
