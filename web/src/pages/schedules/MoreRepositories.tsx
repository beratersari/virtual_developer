import { useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ProjectRepository, RepositorySet } from '../../api/types'
import { SavedRepoSearch } from '../../ui/ProjectSelect'

export type RepoRow = {
  url: string
  source: string
  target: string
  sourceMode: 'issue_key' | 'custom'
}

export function emptyRepoRow(): RepoRow {
  return { url: '', source: 'develop', target: 'develop', sourceMode: 'issue_key' }
}

export function rowFromProject(project: ProjectRepository): RepoRow {
  const source = (project.source_branch || '').trim()
  return {
    url: project.url,
    source: source || 'develop',
    target: (project.target_branch || '').trim() || 'develop',
    sourceMode: source ? 'custom' : 'issue_key',
  }
}

export function rowsFromRepositorySet(
  repositories: string[],
  projects: ProjectRepository[],
): RepoRow[] {
  return repositories.filter(Boolean).map((url) => {
    const saved = projects.find((project) => project.url === url)
    return saved ? rowFromProject(saved) : { ...emptyRepoRow(), url }
  })
}

export function filledRepoRows(rows: RepoRow[]): RepoRow[] {
  return rows.filter((row) => row.url.trim())
}

export function appendRepoRows(
  current: RepoRow[],
  incoming: RepoRow[],
): { rows: RepoRow[]; added: number } {
  const next = filledRepoRows(current)
  const seen = new Set(next.map((row) => row.url.trim()))
  let added = 0
  for (const row of incoming) {
    const url = row.url.trim()
    if (!url || seen.has(url)) continue
    seen.add(url)
    next.push({ ...row, url })
    added += 1
  }
  return { rows: next, added }
}

export function repoRowTitle(row: RepoRow, projects: ProjectRepository[]): string {
  const saved = projects.find((project) => project.url === row.url.trim())
  return (saved?.label || '').trim() || row.url.trim()
}

export function repoRowBranches(row: RepoRow): string {
  const source =
    row.sourceMode === 'custom'
      ? row.source.trim() || 'custom branch'
      : 'feature/<issue key>'
  return `${source} → ${row.target.trim() || 'develop'}`
}

export function repoDraftProblem(draft: RepoRow, taken: string[]): string | null {
  const url = draft.url.trim()
  if (!url) return 'Enter a repository URL'
  if (taken.includes(url)) return 'That repository is already in the list'
  if (draft.sourceMode === 'custom' && !draft.source.trim()) return 'Enter a source branch'
  if (!draft.target.trim()) return 'Enter a target branch'
  return null
}

export function scheduleRepositoryFields(rows: RepoRow[]) {
  const clean = rows
    .map((row) => ({
      url: row.url.trim(),
      sourceMode: row.sourceMode,
      source_branch: row.sourceMode === 'custom' ? row.source.trim() : '',
      target_branch: row.target.trim(),
    }))
    .filter((row) => row.url)
  const first = clean[0]
  const refs =
    clean.length > 1
      ? clean.map((row) => ({
          url: row.url,
          source_branch: row.source_branch,
          target_branch: row.target_branch,
          source_branch_mode: row.sourceMode,
        }))
      : undefined
  return {
    repository_url: first?.url || '',
    source_branch: first?.sourceMode === 'custom' ? first.source_branch : undefined,
    source_branch_mode: first?.sourceMode || 'issue_key',
    target_branch: first?.target_branch || '',
    repository_refs: refs,
  }
}

type RepoDialogState =
  | { mode: 'add'; draft: RepoRow }
  | { mode: 'edit'; index: number; draft: RepoRow }

export function RepositoryList({
  rows,
  setRows,
  sets,
  projects,
  showRemember = false,
  rememberRepo = false,
  setRememberRepo,
}: {
  rows: RepoRow[]
  setRows: (next: RepoRow[] | ((cur: RepoRow[]) => RepoRow[])) => void
  sets: RepositorySet[]
  projects: ProjectRepository[]
  showRemember?: boolean
  rememberRepo?: boolean
  setRememberRepo?: (value: boolean) => void
}) {
  const [notice, setNotice] = useState('')
  const [dialog, setDialog] = useState<RepoDialogState | null>(null)
  const addRef = useRef<HTMLButtonElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const wasOpen = useRef(false)
  const listed = filledRepoRows(rows)
  const titleId = useId()

  const closeDialog = () => setDialog(null)

  useEffect(() => {
    if (!dialog) {
      if (!wasOpen.current) return
      wasOpen.current = false
      const back = returnFocus.current
      const timer = window.setTimeout(() => back?.focus(), 0)
      return () => window.clearTimeout(timer)
    }
    wasOpen.current = true
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDialog()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [dialog])

  const openAdd = () => {
    returnFocus.current = addRef.current
    setNotice('')
    setDialog({ mode: 'add', draft: emptyRepoRow() })
  }

  const addFromSet = (name: string) => {
    setNotice('')
    const picked = sets.find((row) => row.name === name)
    if (!picked) return
    const result = appendRepoRows(rows, rowsFromRepositorySet(picked.repositories, projects))
    setRows(result.rows)
    if (result.added === 0) setNotice('Those repositories are already in the list.')
  }

  return (
    <div className="space-y-3 rounded-xl border border-border p-3">
      <div>
        <div className="text-sm font-semibold text-text">Repositories</div>
        <p className="mt-1 text-xs text-text-muted">
          One repository runs as a single repository. Two or more run together,
          and each one keeps its own source and target. develop or main becomes
          feature/KEY on that repository. The ticket records each repository's source branch.
        </p>
      </div>
      <label className="field">
        <span>Add projects from a set</span>
        <select
          value=""
          disabled={sets.length === 0}
          onChange={(event) => addFromSet(event.target.value)}
        >
          <option value="">{sets.length === 0 ? 'No repo sets yet' : 'Select a set'}</option>
          {sets.map((row) => (
            <option key={row.name} value={row.name}>
              {row.name}
            </option>
          ))}
        </select>
      </label>
      <button
        ref={addRef}
        type="button"
        className="vd-btn vd-btn-secondary"
        onClick={openAdd}
      >
        Add repository
      </button>
      {notice ? (
        <p className="text-xs text-text-muted" role="status">
          {notice}
        </p>
      ) : null}
      {listed.length === 0 ? (
        <p className="text-xs text-text-muted">No repositories yet.</p>
      ) : (
        <ul className="divide-y divide-border" aria-label="Repositories">
          {listed.map((row, index) => {
            const title = repoRowTitle(row, projects)
            return (
              <li key={row.url} className="flex items-center justify-between gap-3 py-2">
                <span className="min-w-0">
                  <span className="block truncate text-sm text-text">{title}</span>
                  <span className="block truncate text-xs text-text-muted">
                    {repoRowBranches(row)}
                  </span>
                </span>
                <span className="flex shrink-0 items-center gap-3">
                  <button
                    type="button"
                    className="vd-btn-ghost inline-flex h-6 w-6 items-center justify-center"
                    aria-label={`Edit ${title}`}
                    onClick={(event) => {
                      returnFocus.current = event.currentTarget
                      setNotice('')
                      setDialog({ mode: 'edit', index, draft: { ...row } })
                    }}
                  >
                    <PencilIcon />
                  </button>
                  <button
                    type="button"
                    className="vd-btn-ghost bad inline-flex h-6 w-6 items-center justify-center"
                    aria-label={`Remove ${title}`}
                    onClick={() => {
                      setNotice('')
                      setRows(listed.filter((_, item) => item !== index))
                      window.setTimeout(() => addRef.current?.focus(), 0)
                    }}
                  >
                    <TrashIcon />
                  </button>
                </span>
              </li>
            )
          })}
        </ul>
      )}
      <p className="quiet text-xs">
        Saved remotes live in{' '}
        <Link to="/settings/jira" className="text-accent-text hover:underline">
          Settings → Projects
        </Link>
        .
      </p>
      {dialog ? (
        <RepositoryDialog
          titleId={titleId}
          state={dialog}
          projects={projects}
          taken={listed
            .filter((_, index) => dialog.mode === 'add' || index !== dialog.index)
            .map((row) => row.url.trim())}
          showRemember={
            showRemember && (dialog.mode === 'add' ? listed.length === 0 : dialog.index === 0)
          }
          rememberRepo={rememberRepo}
          onClose={closeDialog}
          onSave={(draft, remember) => {
            const committed = {
              ...draft,
              url: draft.url.trim(),
              source: draft.source.trim(),
              target: draft.target.trim(),
            }
            if (dialog.mode === 'add') {
              setRows(appendRepoRows(rows, [committed]).rows)
            } else {
              setRows(listed.map((row, index) => (index === dialog.index ? committed : row)))
            }
            if (showRemember && setRememberRepo && (dialog.mode === 'add' ? listed.length === 0 : dialog.index === 0)) {
              setRememberRepo(remember)
            }
            closeDialog()
          }}
        />
      ) : null}
    </div>
  )
}

function RepositoryDialog({
  titleId,
  state,
  projects,
  taken,
  showRemember,
  rememberRepo,
  onClose,
  onSave,
}: {
  titleId: string
  state: RepoDialogState
  projects: ProjectRepository[]
  taken: string[]
  showRemember: boolean
  rememberRepo: boolean
  onClose: () => void
  onSave: (draft: RepoRow, remember: boolean) => void
}) {
  const [draft, setDraft] = useState(state.draft)
  const [remember, setRemember] = useState(rememberRepo)
  const urlRef = useRef<HTMLInputElement>(null)
  const sourceRef = useRef<HTMLSelectElement>(null)
  const problem = repoDraftProblem(draft, taken)
  const known = projects.some((project) => project.url === draft.url.trim())
  const title = state.mode === 'add' ? 'Add repository' : 'Edit repository'

  useEffect(() => {
    if (state.mode === 'add' && projects.length > 0) return
    const node = state.mode === 'edit' ? sourceRef.current : urlRef.current
    node?.focus()
  }, [projects.length, state.mode])

  const save = () => {
    if (problem) return
    onSave(draft, remember)
  }

  return (
    <div
      className="vd-modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="vd-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(event) => {
          if (event.key !== 'Enter') return
          event.preventDefault()
          event.stopPropagation()
          const target = event.target
          if (target instanceof HTMLInputElement && target.getAttribute('role') === 'combobox') {
            return
          }
          save()
        }}
      >
        <h3 id={titleId} className="vd-modal-title">
          {title}
        </h3>
        {projects.length > 0 ? (
          <SavedRepoSearch
            label="Saved repository"
            projects={projects}
            exclude={taken}
            selectedUrl={known ? draft.url.trim() : ''}
            autoFocus={state.mode === 'add'}
            onPick={(url) => {
              const saved = projects.find((project) => project.url === url)
              if (!saved) return
              setDraft(rowFromProject(saved))
            }}
          />
        ) : null}
        <label className="field">
          <span>Repository URL</span>
          <input
            ref={urlRef}
            value={draft.url}
            spellCheck={false}
            placeholder="https://gitlab.com/group/repo.git"
            aria-invalid={problem === 'That repository is already in the list'}
            onChange={(event) => setDraft({ ...draft, url: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Source branch</span>
          <select
            ref={sourceRef}
            value={draft.sourceMode}
            onChange={(event) =>
              setDraft({
                ...draft,
                sourceMode: event.target.value === 'custom' ? 'custom' : 'issue_key',
              })
            }
          >
            <option value="issue_key">feature/&lt;issue key&gt;</option>
            <option value="custom">Custom branch</option>
          </select>
        </label>
        {draft.sourceMode === 'custom' ? (
          <label className="field">
            <span>Branch name</span>
            <input
              value={draft.source}
              spellCheck={false}
              onChange={(event) => setDraft({ ...draft, source: event.target.value })}
              required
            />
          </label>
        ) : null}
        <label className="field">
          <span>Target branch</span>
          <input
            value={draft.target}
            spellCheck={false}
            onChange={(event) => setDraft({ ...draft, target: event.target.value })}
            required
          />
        </label>
        {showRemember && draft.url.trim() && !known ? (
          <label className="field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <input
              type="checkbox"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            <span style={{ margin: 0 }}>Remember this project</span>
          </label>
        ) : null}
        {problem && draft.url.trim() ? (
          <p className="text-xs text-danger-text" role="alert">
            {problem}
          </p>
        ) : null}
        <div className="vd-modal-actions">
          <button type="button" className="vd-btn vd-btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="vd-btn vd-btn-primary" disabled={Boolean(problem)} onClick={save}>
            {state.mode === 'add' ? 'Add' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

function PencilIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none">
      <path
        d="M9.2 2.8l4 4M2.5 13.5l.7-3.2 7.2-7.2a1.2 1.2 0 0 1 1.7 0l.8.8a1.2 1.2 0 0 1 0 1.7l-7.2 7.2-3.2.7z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function TrashIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" fill="none">
      <path
        d="M3.2 4.5h9.6M6.4 4.5V3.4a.8.8 0 0 1 .8-.8h1.6a.8.8 0 0 1 .8.8v1.1M4.6 4.5l.55 8a1 1 0 0 0 1 .9h3.7a1 1 0 0 0 1-.9l.55-8"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
