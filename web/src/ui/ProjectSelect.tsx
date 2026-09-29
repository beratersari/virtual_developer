import { useId, useRef, useState } from 'react'

export function projectMatchesQuery(
  project: { label?: string; url: string },
  query: string,
): boolean {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  const label = (project.label || '').toLowerCase()
  return label.includes(needle) || project.url.toLowerCase().includes(needle)
}

export function searchSavedRepos<T extends { label?: string; url: string }>(
  projects: T[],
  query: string,
  exclude: string[] = [],
): { pickable: T[]; taken: T[] } {
  const matches = projects.filter(
    (project) => Boolean(project.url) && projectMatchesQuery(project, query),
  )
  return {
    pickable: matches.filter((project) => !exclude.includes(project.url)),
    taken: matches.filter((project) => exclude.includes(project.url)),
  }
}

export function SavedRepoSearch({
  label,
  projects,
  exclude = [],
  selectedUrl = '',
  onPick,
  trailing = [],
  autoFocus = false,
}: {
  label: string
  projects: { label?: string; url: string }[]
  exclude?: string[]
  selectedUrl?: string
  onPick: (url: string) => void
  trailing?: { value: string; label: string }[]
  autoFocus?: boolean
}) {
  const id = useId()
  const listId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const selected = projects.find((project) => project.url === selectedUrl)
  const { pickable, taken } = searchSavedRepos(projects, query, exclude)
  const pick = (url: string) => {
    onPick(url)
    setQuery('')
    setOpen(false)
  }
  return (
    <div ref={rootRef}>
      <label className="field" htmlFor={id}>
        <span>{label}</span>
        <input
          id={id}
          type="search"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          placeholder="Name or URL"
          autoComplete="off"
          autoFocus={autoFocus}
          value={open ? query : selected?.label || selected?.url || ''}
          onFocus={() => {
            setQuery('')
            setOpen(true)
          }}
          onChange={(event) => {
            setQuery(event.target.value)
            setOpen(true)
          }}
          onBlur={(event) => {
            const next = event.relatedTarget
            if (next instanceof Node && rootRef.current?.contains(next)) return
            setOpen(false)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setOpen(false)
              return
            }
            if (event.key !== 'Enter') return
            event.preventDefault()
            if (pickable.length === 1) pick(pickable[0].url)
          }}
        />
      </label>
      {open ? (
        <ul
          id={listId}
          role="listbox"
          aria-label={label}
          className="-mt-1 mb-3 max-h-60 overflow-y-auto rounded-xl border border-border"
        >
          {pickable.length === 0 && taken.length === 0 ? (
            <li className="px-3 py-2 text-xs text-text-muted">No matching projects.</li>
          ) : (
            pickable.map((project) => (
              <li key={project.url} role="presentation">
                <button
                  type="button"
                  role="option"
                  className="block w-full border-0 bg-transparent px-3 py-2 text-left hover:bg-surface"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => pick(project.url)}
                >
                  <span className="block text-sm text-text">{project.label || project.url}</span>
                  {project.label ? (
                    <span className="block truncate text-xs text-text-muted">{project.url}</span>
                  ) : null}
                </button>
              </li>
            ))
          )}
          {taken.map((project) => (
            <li key={project.url} role="presentation">
              <div className="px-3 py-2" role="option" aria-disabled="true">
                <span className="block text-sm text-text-muted">{project.label || project.url}</span>
                {project.label ? (
                  <span className="block truncate text-xs text-text-muted">{project.url}</span>
                ) : null}
                <span className="block text-xs text-text-muted">Already added</span>
              </div>
            </li>
          ))}
          {trailing.map((option) => (
            <li key={option.value} role="presentation">
              <button
                type="button"
                role="option"
                className="vd-btn-ghost block w-full px-3 py-2 text-left"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => pick(option.value)}
              >
                {option.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
