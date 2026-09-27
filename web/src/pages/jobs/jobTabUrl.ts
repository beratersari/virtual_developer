export type JobTab = 'overview' | 'plan' | 'prompt' | 'chat' | 'output' | 'logs'

/** Path segment after /jobs/:jobId. Details stays on the bare job URL. */
const SECTION_FOR_TAB: Record<JobTab, string> = {
  overview: '',
  plan: 'plan',
  prompt: 'prompt',
  chat: 'transcript',
  output: 'output',
  logs: 'daemon',
}

const TAB_FOR_SECTION: Record<string, JobTab> = {
  plan: 'plan',
  prompt: 'prompt',
  transcript: 'chat',
  output: 'output',
  daemon: 'logs',
}

export function jobTabFromSection(section: string | undefined): JobTab | null {
  const raw = (section || '').trim().toLowerCase()
  if (!raw) return 'overview'
  return TAB_FOR_SECTION[raw] ?? null
}

export function jobTabPath(jobId: string, tab: JobTab): string {
  const base = `/jobs/${encodeURIComponent(jobId)}`
  const section = SECTION_FOR_TAB[tab]
  return section ? `${base}/${section}` : base
}
