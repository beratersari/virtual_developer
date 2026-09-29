export type NamedProject = {
  label?: string
  url?: string
}

export type SavedProjectRow<T> = {
  project: T
  index: number
  key: string
  name: string
}

/** Visible name: the label, or the URL when the label is blank. */
export function savedProjectName(project: NamedProject): string {
  const label = (project.label || '').trim()
  if (label) return label
  const url = (project.url || '').trim()
  return url || 'Untitled project'
}

export function savedProjectMatchesName(project: NamedProject, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return savedProjectName(project).toLowerCase().includes(needle)
}

export function savedProjectKey(project: NamedProject, index: number): string {
  const url = (project.url || '').trim()
  return url || `#${index}`
}

export function filterSavedProjects<T extends NamedProject>(
  projects: readonly T[],
  query: string,
): SavedProjectRow<T>[] {
  const rows: SavedProjectRow<T>[] = []
  projects.forEach((project, index) => {
    if (!savedProjectMatchesName(project, query)) return
    rows.push({
      project,
      index,
      key: savedProjectKey(project, index),
      name: savedProjectName(project),
    })
  })
  return rows
}

export function visibleSelectionState(
  visibleKeys: readonly string[],
  selected: ReadonlySet<string>,
): 'none' | 'some' | 'all' {
  if (visibleKeys.length === 0) return 'none'
  let hits = 0
  for (const key of visibleKeys) {
    if (selected.has(key)) hits += 1
  }
  if (hits === 0) return 'none'
  if (hits === visibleKeys.length) return 'all'
  return 'some'
}

export function toggleVisibleSelection(
  visibleKeys: readonly string[],
  selected: ReadonlySet<string>,
  select: boolean,
): Set<string> {
  const next = new Set(selected)
  for (const key of visibleKeys) {
    if (select) next.add(key)
    else next.delete(key)
  }
  return next
}

export function withoutSelectedProjects<T extends NamedProject>(
  projects: readonly T[],
  selected: ReadonlySet<string>,
): T[] {
  return projects.filter((project, index) => !selected.has(savedProjectKey(project, index)))
}

export function editorIndexAfterRemoval(
  editorIndex: number | null,
  projects: readonly NamedProject[],
  selected: ReadonlySet<string>,
): number | null {
  if (editorIndex == null) return null
  if (editorIndex < 0 || editorIndex >= projects.length) return null
  if (selected.has(savedProjectKey(projects[editorIndex], editorIndex))) return null
  let shift = 0
  for (let index = 0; index < editorIndex; index += 1) {
    if (selected.has(savedProjectKey(projects[index], index))) shift += 1
  }
  return editorIndex - shift
}
