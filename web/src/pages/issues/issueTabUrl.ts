export type IssueTab = 'overview' | 'logs'

const SECTION_FOR_TAB: Record<IssueTab, string> = {
  overview: '',
  logs: 'logs',
}

const TAB_FOR_SECTION: Record<string, IssueTab> = {
  logs: 'logs',
}

export function issueTabFromSection(section: string | undefined): IssueTab | null {
  const raw = (section || '').trim().toLowerCase()
  if (!raw) return 'overview'
  return TAB_FOR_SECTION[raw] ?? null
}

export function issueTabPath(issueKey: string, tab: IssueTab): string {
  const base = `/tasks/${encodeURIComponent(issueKey)}`
  const section = SECTION_FOR_TAB[tab]
  return section ? `${base}/${section}` : base
}
