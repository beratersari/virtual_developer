import { useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

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

/** URLs the Select all action adds: current search matches that are not already chosen. */
export function selectAllMatching<T extends { label?: string; url: string }>(
  projects: T[],
  query: string,
  exclude: string[] = [],
): string[] {
  return searchSavedRepos(projects, query, exclude).pickable.map((project) => project.url)
}

const GIT_URL = /^(https?:\/\/|ssh:\/\/|git:\/\/|git@)/i

/** Saved URL when the text is that project. Otherwise a pasted git URL, or empty for a name. */
export function directRepoUrl(
  text: string,
  projects: { label?: string; url: string }[],
): string {
  const raw = text.trim()
  if (!raw) return ''
  const exact = projects.find((project) => project.url.trim() === raw)
  if (exact) return exact.url.trim()
  if (GIT_URL.test(raw)) return raw
  return ''
}

/** Search needle for the open list. A click that has not typed yet matches every saved repo. */
export function repoSearchQuery(typed: string, editing: boolean): string {
  return editing ? typed : ''
}

/** A blur clears the chosen repository only after the field was edited empty. */
export function repoSearchClearsOnBlur(typed: string, editing: boolean): boolean {
  return editing && !(typed || '').trim()
}

/** One field: the typed text while open, the saved name when one is chosen, otherwise the URL. */
export function repoSearchValue(
  query: string,
  open: boolean,
  selected: { label?: string; url: string } | undefined,
  selectedUrl: string,
): string {
  if (open) return query
  const label = (selected?.label || '').trim()
  if (label) return label
  return (selected?.url || selectedUrl || '').trim()
}

export type RepoSearchListPlacement = {
  top: number
  left: number
  width: number
  maxHeight: number
}

/** Keep the saved-repo menu inside the viewport, above or below the field. */
export function repoSearchListPlacement(
  anchor: { top: number; bottom: number; left: number; width: number },
  viewport: { width: number; height: number },
): RepoSearchListPlacement {
  const margin = 8
  const gap = 4
  const preferred = 240
  const spaceBelow = Math.max(0, viewport.height - anchor.bottom - margin - gap)
  const spaceAbove = Math.max(0, anchor.top - margin - gap)
  const openBelow = spaceBelow >= spaceAbove
  const available = openBelow ? spaceBelow : spaceAbove
  const maxHeight = Math.min(preferred, available)
  const width = Math.min(anchor.width, Math.max(0, viewport.width - margin * 2))
  const left = Math.min(
    Math.max(margin, anchor.left),
    Math.max(margin, viewport.width - margin - width),
  )
  const top = openBelow
    ? anchor.bottom + gap
    : Math.max(margin, anchor.top - gap - maxHeight)
  return { top, left, width, maxHeight }
}

export function SavedRepoSearch({
  label,
  projects,
  exclude = [],
  selectedUrl = '',
  onPick,
  onSelectAll,
  onDirectUrl,
  trailing = [],
}: {
  label: string
  projects: { label?: string; url: string }[]
  exclude?: string[]
  selectedUrl?: string
  onPick: (url: string) => void
  onSelectAll?: (urls: string[]) => void
  onDirectUrl?: (url: string) => void
  trailing?: { value: string; label: string }[]
}) {
  const id = useId()
  const listId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const [query, setQuery] = useState('')
  const [editing, setEditingState] = useState(false)
  const editingRef = useRef(false)
  const setEditing = (value: boolean) => {
    editingRef.current = value
    setEditingState(value)
  }
  const [open, setOpen] = useState(false)
  const [box, setBox] = useState<RepoSearchListPlacement | null>(null)
  useLayoutEffect(() => {
    if (!open || editing) return
    inputRef.current?.select()
  }, [open, editing])
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const node = inputRef.current
      if (!node) return
      const rect = node.getBoundingClientRect()
      setBox(
        repoSearchListPlacement(
          { top: rect.top, bottom: rect.bottom, left: rect.left, width: rect.width },
          { width: window.innerWidth, height: window.innerHeight },
        ),
      )
    }
    place()
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])
  const selected = projects.find((project) => project.url === selectedUrl)
  const filterQuery = repoSearchQuery(query, editing)
  const { pickable, taken } = searchSavedRepos(projects, filterQuery, exclude)
  const pick = (url: string) => {
    onPick(url)
    setQuery('')
    setEditing(false)
    setOpen(false)
  }
  const chooseAll = () => {
    if (!onSelectAll) return
    const urls = selectAllMatching(projects, filterQuery, exclude)
    if (urls.length === 0) return
    onSelectAll(urls)
    setQuery('')
    setEditing(false)
    setOpen(false)
  }
  const commitTypedUrl = (text: string) => {
    if (!onDirectUrl) return false
    const direct = directRepoUrl(text, projects)
    if (!direct) return false
    onDirectUrl(direct)
    setQuery('')
    setEditing(false)
    setOpen(false)
    return true
  }
  return (
    <div ref={rootRef}>
      <label className="field" htmlFor={id}>
        <span>{label}</span>
        <input
          ref={inputRef}
          id={id}
          type="search"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          placeholder="Name or URL"
          autoComplete="off"
          value={editing ? query : repoSearchValue('', false, selected, selectedUrl)}
          onClick={() => setOpen(true)}
          onChange={(event) => {
            setEditing(true)
            setQuery(event.target.value)
            setOpen(true)
          }}
          onPaste={(event) => {
            const text = event.clipboardData.getData('text')
            if (!commitTypedUrl(text)) return
            event.preventDefault()
          }}
          onBlur={(event) => {
            const next = event.relatedTarget
            if (
              next instanceof Node &&
              (rootRef.current?.contains(next) || listRef.current?.contains(next))
            ) {
              return
            }
            const typed = event.currentTarget.value
            if (editingRef.current && commitTypedUrl(typed)) return
            if (repoSearchClearsOnBlur(typed, editingRef.current)) {
              if (onDirectUrl) onDirectUrl('')
              else onPick('')
            }
            setQuery('')
            setEditing(false)
            setOpen(false)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              setQuery('')
              setEditing(false)
              setOpen(false)
              return
            }
            if (event.key !== 'Enter') return
            event.preventDefault()
            const typed = event.currentTarget.value
            if (editingRef.current && commitTypedUrl(typed)) return
            if (pickable.length === 1) pick(pickable[0].url)
          }}
        />
      </label>
      {open && box && box.maxHeight > 0
        ? createPortal(
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={label}
          className="overflow-y-auto rounded-xl border border-border bg-bg-elevated shadow-lg"
          style={{
            position: 'fixed',
            top: box.top,
            left: box.left,
            width: box.width,
            maxHeight: box.maxHeight,
            zIndex: 80,
            overscrollBehavior: 'contain',
          }}
        >
          {onSelectAll && pickable.length > 0 ? (
            <li role="presentation" className="sticky top-0 z-10 border-b border-border bg-bg-elevated">
              <button
                type="button"
                className="block w-full border-0 bg-transparent px-3 py-2 text-left text-sm text-accent-text hover:bg-surface"
                aria-label="Select all matching repositories"
                onMouseDown={(event) => event.preventDefault()}
                onClick={chooseAll}
              >
                Select all
              </button>
            </li>
          ) : null}
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
                  <span className="block truncate text-sm text-text">{project.label || project.url}</span>
                </button>
              </li>
            ))
          )}
          {taken.map((project) => (
            <li key={project.url} role="presentation">
              <div className="px-3 py-2" role="option" aria-disabled="true">
                <span className="block truncate text-sm text-text-muted">{project.label || project.url}</span>
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
        </ul>,
        document.body,
        )
        : null}
    </div>
  )
}
