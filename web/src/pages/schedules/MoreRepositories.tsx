import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { ProjectRepository, RepositorySet } from '../../api/types'
import { SavedRepoSearch } from '../../ui/ProjectSelect'

export type RepoRow = {
  url: string
  source: string
  target: string
  sourceMode: 'issue_key' | 'custom'
}

const CUSTOM_REPO = '__custom__'

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

export function scheduleRepositoryFields(rows: RepoRow[]) {
  const clean = rows
    .map((row) => ({
      url: row.url.trim(),
      sourceMode: row.sourceMode,
      source_branch: row.sourceMode === 'custom' ? row.source.trim() : 'develop',
      target_branch: row.target.trim(),
    }))
    .filter((row) => row.url)
  const first = clean[0]
  const refs =
    clean.length > 1
      ? clean.map((row, index) => ({
          url: row.url,
          source_branch: index === 0 && row.sourceMode === 'issue_key' ? '' : row.source_branch,
          target_branch: row.target_branch,
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
  const [setName, setSetName] = useState('')
  const visible = rows.length > 0 ? rows : [emptyRepoRow()]

  const update = (idx: number, patch: Partial<RepoRow>) => {
    setRows((cur) => {
      const base = cur.length > 0 ? cur : [emptyRepoRow()]
      return base.map((row, i) => (i === idx ? { ...row, ...patch } : row))
    })
  }

  const addRow = (row: RepoRow) => {
    setRows((cur) => {
      const base = cur.length > 0 ? cur : [emptyRepoRow()]
      if (base.some((item) => item.url === row.url)) return base
      if (base.length === 1 && !base[0].url.trim()) return [row]
      return [...base, row]
    })
  }

  return (
    <div className="space-y-3 rounded-xl border border-border p-3">
      <div>
        <div className="text-sm font-semibold text-text">Repositories</div>
        <p className="mt-1 text-xs text-text-muted">
          One repository runs as a single repository. Two or more run together,
          and each one keeps its own source and target. develop or main becomes
          feature/KEY on that repository. The ticket names only the first one.
        </p>
      </div>
      {sets.length > 0 ? (
        <label className="field">
          <span>Repo set</span>
          <select
            value={setName}
            onChange={(e) => {
              const name = e.target.value
              setSetName(name)
              const picked = sets.find((row) => row.name === name)
              if (!picked) return
              const next = rowsFromRepositorySet(picked.repositories, projects)
              if (next.length > 0) setRows(next)
            }}
          >
            <option value="">Select a saved set</option>
            {sets.map((row) => (
              <option key={row.name} value={row.name}>
                {row.name}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {visible.map((row, idx) => (
        <RepositoryRow
          key={idx}
          row={row}
          projects={projects}
          taken={visible.map((item) => item.url).filter((url) => url && url !== row.url)}
          canRemove={visible.length > 1}
          showRemember={showRemember && idx === 0}
          rememberRepo={rememberRepo}
          setRememberRepo={setRememberRepo}
          onChange={(patch) => update(idx, patch)}
          onRemove={() => setRows((cur) => cur.filter((_, i) => i !== idx))}
          add={
            idx === visible.length - 1 ? (
              <AddRepository
                projects={projects}
                used={visible.map((item) => item.url)}
                onAdd={(url) => {
                  const saved = projects.find((p) => p.url === url)
                  addRow(saved ? rowFromProject(saved) : { ...emptyRepoRow(), url })
                }}
              />
            ) : null
          }
        />
      ))}
      <p className="quiet text-xs">
        Saved remotes live in{' '}
        <Link to="/settings/jira" className="text-accent-text hover:underline">
          Settings → Projects
        </Link>
        .
      </p>
    </div>
  )
}

function RepositoryRow({
  row,
  projects,
  taken,
  canRemove,
  showRemember,
  rememberRepo,
  setRememberRepo,
  onChange,
  onRemove,
  add,
}: {
  row: RepoRow
  projects: ProjectRepository[]
  taken: string[]
  canRemove: boolean
  showRemember: boolean
  rememberRepo: boolean
  setRememberRepo?: (value: boolean) => void
  onChange: (patch: Partial<RepoRow>) => void
  onRemove: () => void
  add?: ReactNode
}) {
  const known = projects.some((p) => p.url === row.url)
  const [otherUrl, setOtherUrl] = useState(false)
  useEffect(() => {
    if (known) setOtherUrl(false)
  }, [known])
  const showUrl = projects.length === 0 || otherUrl || (!known && Boolean(row.url))
  return (
    <div className="space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          {projects.length > 0 ? (
            <SavedRepoSearch
              label="Repository"
              projects={projects}
              exclude={taken}
              selectedUrl={known ? row.url : ''}
              trailing={[{ value: CUSTOM_REPO, label: 'Other URL…' }]}
              onPick={(url) => {
                if (url === CUSTOM_REPO) {
                  setOtherUrl(true)
                  onChange({ url: '' })
                  return
                }
                setOtherUrl(false)
                const saved = projects.find((p) => p.url === url)
                if (saved) onChange(rowFromProject(saved))
              }}
            />
          ) : null}
          {showUrl ? (
            <label className="field">
              {projects.length === 0 ? <span>Repository</span> : <span>Git URL</span>}
              <input
                value={row.url}
                onChange={(e) => onChange({ url: e.target.value })}
                placeholder="https://gitlab.com/group/repo.git"
                required
              />
            </label>
          ) : null}
        </div>
        {canRemove ? (
          <button type="button" className="vd-btn-ghost mt-6 text-danger-text" onClick={onRemove}>
            Remove
          </button>
        ) : null}
      </div>
      {showRemember && setRememberRepo && row.url && !known ? (
        <label className="field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <input
            type="checkbox"
            checked={rememberRepo}
            onChange={(e) => setRememberRepo(e.target.checked)}
          />
          <span style={{ margin: 0 }}>Remember this project</span>
        </label>
      ) : null}
      <label className="field">
        <span>Source</span>
        <select
          value={row.sourceMode}
          onChange={(e) =>
            onChange({ sourceMode: e.target.value === 'custom' ? 'custom' : 'issue_key' })
          }
        >
          <option value="issue_key">feature/&lt;issue key&gt;</option>
          <option value="custom">custom branch</option>
        </select>
      </label>
      {row.sourceMode === 'custom' ? (
        <label className="field">
          <span>Branch</span>
          <input
            value={row.source}
            onChange={(e) => onChange({ source: e.target.value })}
            required
          />
        </label>
      ) : null}
      <label className="field">
        <span>Target</span>
        <input value={row.target} onChange={(e) => onChange({ target: e.target.value })} required />
      </label>
      {add}
    </div>
  )
}

function AddRepository({
  projects,
  used,
  onAdd,
}: {
  projects: ProjectRepository[]
  used: string[]
  onAdd: (url: string) => void
}) {
  const [addRepoUrl, setAddRepoUrl] = useState('')
  return (
    <div className="mt-3 space-y-2">
      {projects.some((project) => project.url) ? (
        <SavedRepoSearch
          label="Add a repository"
          projects={projects}
          exclude={used}
          onPick={onAdd}
        />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={addRepoUrl}
          onChange={(e) => setAddRepoUrl(e.target.value)}
          placeholder="https://gitlab.example.com/group/repo.git"
          className="min-w-[16rem] flex-1"
        />
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={!addRepoUrl.trim()}
          onClick={() => {
            onAdd(addRepoUrl.trim())
            setAddRepoUrl('')
          }}
        >
          Add URL
        </button>
      </div>
    </div>
  )
}
