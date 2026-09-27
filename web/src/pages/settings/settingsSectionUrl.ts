export type SettingsSection = 'jira' | 'gitlab' | 'azure' | 'projects' | 'model' | 'runtime'

const SECTION_FOR_TAB: Record<SettingsSection, string> = {
  jira: 'jira',
  gitlab: 'gitlab',
  azure: 'azure',
  projects: 'projects',
  model: 'agent',
  runtime: 'runtime',
}

const TAB_FOR_SECTION: Record<string, SettingsSection> = {
  jira: 'jira',
  gitlab: 'gitlab',
  azure: 'azure',
  projects: 'projects',
  agent: 'model',
  model: 'model',
  runtime: 'runtime',
}

export function settingsSectionFromParam(section: string | undefined): SettingsSection | null {
  const raw = (section || '').trim().toLowerCase()
  if (!raw) return 'jira'
  return TAB_FOR_SECTION[raw] ?? null
}

export function settingsSectionPath(section: SettingsSection): string {
  return `/settings/${SECTION_FOR_TAB[section]}`
}

export function settingsHere(section: string | undefined): string {
  const raw = (section || '').trim()
  return raw ? `/settings/${raw}` : '/settings'
}

export function canonicalSettingsPath(section: string | undefined): string {
  return settingsSectionPath(settingsSectionFromParam(section) ?? 'jira')
}
