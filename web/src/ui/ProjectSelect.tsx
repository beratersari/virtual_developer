import { useId, useState } from 'react'
import type { ProjectRepository } from '../api/types'

export function projectMatchesQuery(
  project: { label?: string; url: string },
  query: string,
): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  const label = (project.label || '').toLowerCase()
  return label.includes(needle) || project.url.toLowerCase().includes(needle)
}

export function ProjectSearchField({
  value,
  onChange,
}: {
  value: string
  onChange: (next: string) => void
}) {
  const id = useId()
  return (
    <label className="field" htmlFor={id}>
      <span>Search</span>
      <input
        id={id}
        type="search"
        value={value}
        placeholder="Name or URL"
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') e.preventDefault()
        }}
      />
    </label>
  )
}

export function ProjectSelect({
  label,
  projects,
  value,
  onChange,
  emptyOption,
  trailingOptions = [],
}: {
  label: string
  projects: ProjectRepository[]
  value: string
  onChange: (value: string) => void
  emptyOption?: string
  trailingOptions?: { value: string; label: string }[]
}) {
  const [query, setQuery] = useState('')
  const matched = projects.filter((project) => project.url && projectMatchesQuery(project, query))
  const pinned =
    value && !matched.some((project) => project.url === value)
      ? projects.find((project) => project.url === value)
      : undefined
  const shown = pinned ? [pinned, ...matched] : matched
  const searching = query.trim().length > 0
  return (
    <>
      <ProjectSearchField value={query} onChange={setQuery} />
      {searching && shown.length === 0 ? (
        <p className="-mt-2 mb-3 text-xs text-text-muted">No matching projects.</p>
      ) : null}
      <label className="field">
        <span>{label}</span>
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          {emptyOption != null ? <option value="">{emptyOption}</option> : null}
          {shown.map((project) => (
            <option key={project.url} value={project.url}>
              {project.label || project.url}
            </option>
          ))}
          {trailingOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    </>
  )
}
