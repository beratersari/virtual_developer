/**
 * Browser tab title. Copying the address bar pastes this name as the link preview.
 * Keep the names in step with src/dashboard/page_title.py.
 */
import {
  ANALYTICS_PERIOD_LABELS,
  analyticsPeriodFromParam,
} from '../pages/analytics/analyticsPeriodUrl'
import { listPageFromSegment } from './listPageUrl'
import { parseSchedulePath } from '../pages/schedules/scheduleTabUrl'
import { settingsSectionFromParam } from '../pages/settings/settingsSectionUrl'

const JOB_LIST_NAMES: Record<string, string> = {
  'in-flight': 'Jobs - In flight',
  active: 'Jobs - In flight',
  queue: 'Jobs - Queue',
  error: 'Jobs - Error',
  completed: 'Jobs - Completed',
  cancelled: 'Jobs - Cancelled',
  'plan-ready': 'Jobs - Plan ready',
}

const JOB_TAB_LABEL: Record<string, string> = {
  plan: 'Plan',
  prompt: 'Prompt',
  transcript: 'Transcript',
  output: 'Output',
  daemon: 'Daemon',
}

const SETTINGS_NAMES = {
  jira: 'Settings - Jira',
  gitlab: 'Settings - GitLab',
  azure: 'Settings - Azure',
  projects: 'Settings - Projects',
  model: 'Settings - Agent',
  runtime: 'Settings - Runtime',
} as const

function collapse(value: string | null | undefined): string {
  return (value || '').replace(/\s+/g, ' ').trim()
}

function partsOf(pathname: string): string[] {
  return pathname
    .split('/')
    .filter(Boolean)
    .map((part) => {
      try {
        return decodeURIComponent(part)
      } catch {
        return part
      }
    })
}

function pageNumber(raw: string | undefined): boolean {
  return listPageFromSegment(raw) != null
}

export function formatPageTitle(name: string): string {
  const clean = collapse(name)
  if (!clean || clean === 'Yaver') return 'Yaver'
  if (clean.startsWith('Yaver - ')) return clean
  return `Yaver - ${clean}`
}

/** Generic name used until a job, issue, or workspace record has loaded. */
export function isRecordPlaceholder(name: string): boolean {
  return /^(Job|Issue|Workspace)( - .+)?$/.test(collapse(name))
}

/**
 * On a fresh load the HTML title already names the record. Keep it until the
 * page has a real name, so the tab does not drop to "Job" while the request
 * is in flight. Later navigations still update immediately.
 */
export function resolveDocumentTitle(
  currentTitle: string,
  name: string,
  seenRealTitle: boolean,
): { title: string; seenRealTitle: boolean } {
  const next = formatPageTitle(name)
  const currentName = currentTitle.startsWith('Yaver - ')
    ? currentTitle.slice('Yaver - '.length)
    : ''
  if (
    !seenRealTitle &&
    isRecordPlaceholder(name) &&
    currentName &&
    !isRecordPlaceholder(currentName)
  ) {
    return { title: currentTitle, seenRealTitle: false }
  }
  return { title: next, seenRealTitle: true }
}

let seenRealTitle = false

/** Writes the tab title and the og:title tag link previews read. */
export function applyDocumentTitle(name: string): void {
  if (typeof document === 'undefined') return
  const resolved = resolveDocumentTitle(document.title, name, seenRealTitle)
  seenRealTitle = resolved.seenRealTitle
  document.title = resolved.title
  let meta = document.querySelector('meta[property="og:title"]')
  if (!meta) {
    meta = document.createElement('meta')
    meta.setAttribute('property', 'og:title')
    document.head.appendChild(meta)
  }
  meta.setAttribute('content', resolved.title)
}

export function jobPageName(
  summary: string | null | undefined,
  section?: string | null,
  issueKey?: string | null,
): string {
  const base = collapse(summary) || collapse(issueKey) || 'Job'
  const tab = JOB_TAB_LABEL[(section || '').trim().toLowerCase()] || ''
  return tab ? `${base} - ${tab}` : base
}

export function issuePageName(
  summary: string | null | undefined,
  issueKey?: string | null,
  section?: string | null,
): string {
  const base = collapse(summary) || collapse(issueKey) || 'Issue'
  const tab = (section || '').trim().toLowerCase() === 'logs' ? 'System logs' : ''
  return tab ? `${base} - ${tab}` : base
}

export function workspacePageName(
  branch?: string | null,
  targetBranch?: string | null,
): string {
  const source = collapse(branch)
  if (!source) return 'Workspace'
  const target = collapse(targetBranch)
  return target ? `${source} → ${target}` : source
}

export function reviewsPageName(origin: string | null | undefined, state: string | null | undefined): string {
  const who =
    origin === 'ours'
      ? 'Opened by us'
      : origin === 'contributed'
        ? 'Contributed'
        : 'Merge requests'
  if (state === 'opened') return `${who} · open`
  if (state === 'merged') return `${who} · merged`
  if (state === 'closed') return `${who} · closed`
  return who
}

function queryValue(search: string, key: string): string {
  const raw = search.startsWith('?') ? search.slice(1) : search
  return new URLSearchParams(raw).get(key) || ''
}

function scheduleName(parts: string[]): string {
  const parsed = parseSchedulePath(parts[1], parts[2], parts[3])
  if (!parsed) return 'Existing issue'
  if (parsed.mode === 'mr') return 'Existing MR'
  if (parsed.mode === 'pr') return 'Existing PR'
  const base = parsed.mode === 'new' ? 'New issue' : 'Existing issue'
  return parsed.tracker === 'azure' ? `${base} - Azure work item` : base
}

/**
 * Name for the record bar. List pages keep their heading, so this stays empty.
 * A placeholder such as "Job" stays empty until the issue name is known.
 */
export function recordTitle(visible: string, fallback: string): string | null {
  if (!isRecordPlaceholder(fallback)) return null
  if (isRecordPlaceholder(visible)) return null
  return visible
}

/** The open record's name wins. A leftover name from another address does not. */
export function shownPageName(
  fallback: string,
  override: { path: string; name: string } | null,
  pathname: string,
): string {
  return override && override.path === pathname ? override.name : fallback
}

/** Tab text without the "Yaver - " prefix. A placeholder yields to a real title already on the document. */
export function visiblePageNameFrom(
  name: string,
  currentTitle: string,
  seenReal: boolean,
): string {
  const title = resolveDocumentTitle(currentTitle, name, seenReal).title
  const prefix = 'Yaver - '
  if (title.startsWith(prefix)) return title.slice(prefix.length)
  return collapse(name) || name
}

/** Same name the browser tab is showing for this page. */
export function visiblePageName(name: string): string {
  if (typeof document === 'undefined') return name
  return visiblePageNameFrom(name, document.title, seenRealTitle)
}

/** Name after "Yaver - ". Record pages stay generic until the page supplies the title. */
export function pageNameFromLocation(pathname: string, search = ''): string {
  const parts = partsOf(pathname)
  const head = (parts[0] || '').toLowerCase()

  if (!head || head === 'queue') return 'Jobs'

  if (head === 'jobs') {
    if (parts.length === 1) return 'Jobs'
    const section = parts[1].toLowerCase()
    if (JOB_LIST_NAMES[section]) {
      return parts.length > 3 ? 'Jobs' : JOB_LIST_NAMES[section]
    }
    if (parts.length === 2 && pageNumber(parts[1])) return 'Jobs'
    if (parts.length > 3) return 'Jobs'
    return jobPageName(null, parts[2])
  }

  if (head === 'tasks') {
    if (parts.length < 2 || parts.length > 3) return 'Jobs'
    // The key lives in the address. Putting it in the title would replace
    // the issue summary the server already wrote into the page.
    return issuePageName(null, null, parts[2])
  }

  if (head === 'analytics') {
    if ((parts[1] || '').toLowerCase() === 'reviews') {
      if (parts.length > 3) return 'Jobs'
      return reviewsPageName(queryValue(search, 'origin'), queryValue(search, 'state'))
    }
    if (parts.length > 2) return 'Jobs'
    const period = parts[1] ? analyticsPeriodFromParam(parts[1]) : '30d'
    if (!period || period === '30d') return 'Analytics'
    return `Analytics - ${ANALYTICS_PERIOD_LABELS[period]}`
  }

  if (head === 'scheduled') {
    if (parts.length > 4) return 'Jobs'
    return scheduleName(parts)
  }

  if (head === 'schedules') return 'Existing issue'

  if (head === 'sessions') {
    if (parts.length === 1) return 'Sessions'
    if (parts.length === 2 && pageNumber(parts[1])) return 'Sessions'
    if (parts.length === 2) return 'Workspace'
    return 'Jobs'
  }

  if (head === 'storage' && parts.length === 1) return 'Storage'
  if (head === 'poll' && parts.length === 1) return 'Board'

  if (head === 'settings') {
    if (parts.length > 2) return 'Jobs'
    const section = settingsSectionFromParam(parts[1]) ?? 'jira'
    return SETTINGS_NAMES[section]
  }

  return 'Jobs'
}
