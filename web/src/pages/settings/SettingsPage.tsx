import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  fetchSettings,
  importAccessibleProjects,
  patchSettings,
  testAzureConnection,
  testGitlabConnection,
  testJiraConnection,
} from '../../api/client'
import type {
  GitlabConnectionTestResult,
  GitlabHostCredentialDraft,
  JiraConnectionTestResult,
  ProjectRepository,
  RepositorySet,
  SettingsPayload,
  WorkMode,
} from '../../api/types'
import { useLive } from '../../app/live'
import { azureCollectionProblem } from './azureCollection'
import { ModesPanel } from './ModesPanel'
import {
  editorIndexAfterRemoval,
  filterSavedProjects,
  savedProjectKey,
  toggleVisibleSelection,
  visibleSelectionState,
  withoutSelectedProjects,
} from './savedProjects'
import {
  canonicalSettingsPath,
  settingsHere,
  settingsSectionFromParam,
  settingsSectionPath,
  type SettingsSection,
} from './settingsSectionUrl'
import { ModelField } from '../../ui/ModelField'
import { SavedRepoSearch } from '../../ui/ProjectSelect'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

type Draft = {
  jira_enabled: boolean
  jira_host: string
  jira_api_token: string
  jira_board_id: string
  jira_projects: string
  poll_interval_seconds: number
  jira_trigger_user: string
  jira_trigger_label: string
  gitlab_trigger_user: string
  azure_trigger_user: string
  gitlab_webhook_enabled: boolean
  gitlab_webhook_secret: string
  azure_webhook_enabled: boolean
  max_concurrent_jobs: number
  temp_clone_max_age_days: number
  agent_task_timeout_seconds: number
  agent_task_max_retries: number
  agent_task_max_incomplete_retries: number
  default_model: string
  default_review_model: string
  agent_backend: string
  gitlab_cred_rows: GitlabHostCredentialDraft[]
  azure_cred_rows: GitlabHostCredentialDraft[]
  project_repositories: ProjectRepository[]
  repository_sets: RepositorySet[]
  work_modes: WorkMode[]
}

function SettingsGroup({
  title,
  children,
}: {
  title?: string
  children: ReactNode
}) {
  return (
    <div className="vd-panel space-y-3 p-5">
      {title ? <div className="text-sm font-semibold text-text">{title}</div> : null}
      {children}
    </div>
  )
}

function fromSettings(s: SettingsPayload): Draft {
  return {
    jira_enabled: s.jira_enabled !== false,
    jira_host: s.jira_host,
    jira_api_token: '',
    jira_board_id: s.jira_board_id,
    jira_projects: s.jira_projects ?? '',
    poll_interval_seconds: s.poll_interval_seconds,
    jira_trigger_user: s.jira_trigger_user ?? s.trigger_assignee_names ?? '',
    jira_trigger_label: s.jira_trigger_label ?? s.trigger_labels ?? '',
    gitlab_trigger_user: s.gitlab_trigger_user ?? s.gitlab_bot_mentions ?? '',
    azure_trigger_user: s.azure_trigger_user ?? s.azure_bot_mentions ?? '',
    gitlab_webhook_enabled: s.gitlab_webhook_enabled !== false,
    gitlab_webhook_secret: '',
    azure_webhook_enabled: s.azure_webhook_enabled === true,
    max_concurrent_jobs: s.max_concurrent_jobs,
    temp_clone_max_age_days: s.temp_clone_max_age_days ?? 7,
    agent_task_timeout_seconds: s.agent_task_timeout_seconds,
    agent_task_max_retries: s.agent_task_max_retries ?? 3,
    agent_task_max_incomplete_retries: s.agent_task_max_incomplete_retries ?? 256,
    default_model: s.default_model,
    default_review_model: s.default_review_model || '',
    agent_backend: s.agent_backend || 'opencode',
    gitlab_cred_rows: (s.gitlab_credentials ?? []).map((c) => ({
      host: c.host,
      pat: '',
      pat_configured: Boolean(c.pat_configured),
      original_host: c.host,
    })),
    azure_cred_rows: (s.azure_credentials ?? []).map((c) => ({
      host: c.host,
      pat: '',
      pat_configured: Boolean(c.pat_configured),
      original_host: c.host,
    })),
    project_repositories: (s.project_repositories ?? []).map((p) => ({
      label: p.label || '',
      url: p.url || '',
      target_branch: p.target_branch || '',
      source_branch: p.source_branch || '',
    })),
    repository_sets: (s.repository_sets ?? []).map((row) => ({
      name: row.name || '',
      repositories: [...(row.repositories || [])],
    })),
    work_modes: (s.work_modes ?? []).map((row) => ({
      name: row.name,
      behavior: row.behavior,
      agent: row.agent,
      builtin: Boolean(row.builtin),
    })),
  }
}

function savedShape(d: Draft) {
  return {
    jira_enabled: d.jira_enabled,
    jira_host: d.jira_host.trim(),
    jira_api_token: d.jira_api_token.trim(),
    jira_board_id: d.jira_board_id.trim(),
    jira_projects: d.jira_projects.trim(),
    poll_interval_seconds: Number(d.poll_interval_seconds),
    jira_trigger_user: d.jira_trigger_user,
    jira_trigger_label: d.jira_trigger_label,
    gitlab_trigger_user: d.gitlab_trigger_user,
    azure_trigger_user: d.azure_trigger_user,
    gitlab_webhook_enabled: d.gitlab_webhook_enabled,
    gitlab_webhook_secret: d.gitlab_webhook_secret.trim(),
    azure_webhook_enabled: d.azure_webhook_enabled,
    max_concurrent_jobs: Number(d.max_concurrent_jobs),
    temp_clone_max_age_days: Number(d.temp_clone_max_age_days),
    agent_task_timeout_seconds: Number(d.agent_task_timeout_seconds),
    agent_task_max_retries: Number(d.agent_task_max_retries),
    agent_task_max_incomplete_retries: Number(d.agent_task_max_incomplete_retries),
    default_model: d.default_model.trim(),
    default_review_model: d.default_review_model.trim(),
    agent_backend: d.agent_backend,
    gitlab_cred_rows: d.gitlab_cred_rows
      .map((r) => ({ host: r.host.trim(), pat: r.pat.trim() }))
      .filter((r) => r.host || r.pat),
    azure_cred_rows: d.azure_cred_rows
      .map((r) => ({ host: r.host.trim(), pat: r.pat.trim() }))
      .filter((r) => r.host || r.pat),
    project_repositories: d.project_repositories
      .map((p) => ({
        label: p.label.trim(),
        url: p.url.trim(),
        target_branch: (p.target_branch || '').trim(),
        source_branch: (p.source_branch || '').trim(),
      }))
      .filter((p) => p.url || p.label || p.target_branch || p.source_branch),
    repository_sets: d.repository_sets
      .map((row) => ({
        name: row.name.trim(),
        repositories: row.repositories.map((url) => url.trim()).filter(Boolean),
      }))
      .filter((row) => row.name && row.repositories.length >= 2),
    work_modes: d.work_modes
      .map((row) => ({
        name: row.name.trim().toLowerCase(),
        behavior: row.behavior || 'build',
        agent: row.agent.trim(),
        builtin: Boolean(row.builtin),
      }))
      .filter((row) => row.name || row.agent),
  }
}

function draftChanged(draft: Draft, saved: SettingsPayload): boolean {
  return JSON.stringify(savedShape(draft)) !== JSON.stringify(savedShape(fromSettings(saved)))
}

function RepoSetList({
  sets,
  projects,
  editor,
  titleId,
  onOpenNew,
  onOpenEdit,
  onClose,
  onChangeEditor,
  onSave,
  onRemoveAt,
}: {
  sets: RepositorySet[]
  projects: ProjectRepository[]
  editor: { index: number | null; name: string; repositories: string[] } | null
  titleId: string
  onOpenNew: () => void
  onOpenEdit: (index: number) => void
  onClose: () => void
  onChangeEditor: (
    next: { index: number | null; name: string; repositories: string[] } | null,
  ) => void
  onSave: (repositories: string[]) => void
  onRemoveAt: (index: number) => void
}) {
  const addRef = useRef<HTMLButtonElement>(null)
  const editorToken = editor == null ? '' : editor.index == null ? 'new' : String(editor.index)
  const saved = projects
    .map((row) => ({ url: row.url.trim(), label: (row.label || '').trim() }))
    .filter((row) => row.url)
  const known = new Set(saved.map((row) => row.url))
  const choices = [
    ...saved,
    ...(editor?.repositories || [])
      .map((url) => url.trim())
      .filter((url) => url && !known.has(url))
      .map((url) => ({ url, label: '' })),
  ]
  const canSave = Boolean(
    editor && editor.name.trim() && editor.repositories.filter(Boolean).length >= 2,
  )
  const addRepository = (url: string) => {
    const clean = url.trim()
    if (!clean || !editor || editor.repositories.includes(clean)) return
    onChangeEditor({ ...editor, repositories: [...editor.repositories, clean] })
  }
  return (
    <div className="rounded-xl border border-border p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text">Repo sets</div>
          <p className="mt-1 text-xs text-text-muted">
            A named group of projects you already saved. A scheduled job can
            select the set and work in every repository.
          </p>
        </div>
        <button
          ref={addRef}
          type="button"
          className="vd-btn vd-btn-secondary shrink-0"
          aria-label="Add repo set"
          onClick={onOpenNew}
        >
          +
        </button>
      </div>
      {sets.length === 0 ? (
        <p className="mt-3 text-xs text-text-muted">No repo sets yet.</p>
      ) : (
        <ul className="mt-3 divide-y divide-border" aria-label="Repo sets">
          {sets.map((row, idx) => {
            const title = row.name || 'Untitled set'
            return (
              <li key={`${row.name}-${idx}`} className="flex items-center justify-between gap-3 py-2">
                <span className="min-w-0 truncate text-sm text-text">{title}</span>
                <RowActions
                  editLabel={`Edit ${title}`}
                  removeLabel={`Remove ${title}`}
                  onEdit={() => onOpenEdit(idx)}
                  onRemove={() => {
                    onRemoveAt(idx)
                    window.setTimeout(() => addRef.current?.focus(), 0)
                  }}
                />
              </li>
            )
          })}
        </ul>
      )}
      {editor ? (
        <div
          className="vd-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) onClose()
          }}
        >
          <form
            className="vd-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            onSubmit={(e) => {
              e.preventDefault()
              if (!editor || !canSave) return
              onSave(editor.repositories)
            }}
          >
            <h3 id={titleId} className="vd-modal-title">
              {editor.index == null ? 'Add repo set' : 'Edit repo set'}
            </h3>
            <label className="field">
              <span>Name</span>
              <input
                value={editor.name}
                onChange={(e) =>
                  onChangeEditor({ ...editor, name: e.target.value })
                }
                required
              />
            </label>
            {editor.repositories.filter(Boolean).length === 0 ? (
              <p className="text-xs text-text-muted">No repositories in this set yet.</p>
            ) : (
              <ul className="max-h-40 divide-y divide-border overflow-y-auto" aria-label="Repositories in this set">
                {editor.repositories.filter(Boolean).map((url) => {
                  const row = choices.find((choice) => choice.url === url)
                  return (
                    <li key={url} className="flex items-center justify-between gap-3 py-2">
                      <span className="min-w-0 truncate text-sm text-text">{row?.label || url}</span>
                      <button
                        type="button"
                        className="vd-btn-ghost bad"
                        aria-label={`Remove ${row?.label || url} from this set`}
                        onClick={() =>
                          onChangeEditor({
                            ...editor,
                            repositories: editor.repositories.filter((item) => item !== url),
                          })
                        }
                      >
                        <TrashIcon />
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
            <SavedRepoSearch
              key={editorToken}
              label="Repository"
              projects={saved}
              exclude={editor.repositories}
              onPick={addRepository}
              onDirectUrl={addRepository}
              onSelectAll={(urls) => {
                const next = editor.repositories.slice()
                for (const url of urls) {
                  if (!next.includes(url)) next.push(url)
                }
                onChangeEditor({ ...editor, repositories: next })
              }}
            />
            <p className="mt-2 text-xs text-text-muted">Select at least two projects.</p>
            <div className="vd-modal-actions">
              <button type="button" className="vd-btn vd-btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="vd-btn vd-btn-primary" disabled={!canSave}>
                Save
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  )
}

function RowActions({
  editLabel,
  removeLabel,
  onEdit,
  onRemove,
}: {
  editLabel: string
  removeLabel: string
  onEdit: (current: HTMLButtonElement) => void
  onRemove: () => void
}) {
  return (
    <span className="flex shrink-0 items-center gap-3">
      <button
        type="button"
        className="vd-btn-ghost"
        aria-label={editLabel}
        onClick={(event) => onEdit(event.currentTarget)}
      >
        <PencilIcon />
      </button>
      <button
        type="button"
        className="vd-btn-ghost bad"
        aria-label={removeLabel}
        onClick={onRemove}
      >
        <TrashIcon />
      </button>
    </span>
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

type ProjectEditorState = {
  index: number | null
  label: string
  url: string
  target_branch: string
  source_branch: string
}

function ProjectRepoList({
  projects,
  editor,
  titleId,
  status,
  reloadDisabled,
  onReload,
  onOpenNew,
  onOpenEdit,
  onClose,
  onChangeEditor,
  onSave,
  onRemoveAt,
  onRemoveSelected,
  emptyText = 'No saved projects yet.',
}: {
  projects: ProjectRepository[]
  emptyText?: string
  editor: ProjectEditorState | null
  titleId: string
  status: ReactNode
  reloadDisabled: boolean
  onReload: () => void
  onOpenNew: () => void
  onOpenEdit: (index: number) => void
  onClose: () => void
  onChangeEditor: (next: ProjectEditorState) => void
  onSave: () => void
  onRemoveAt: (index: number) => void
  onRemoveSelected: (selected: ReadonlySet<string>) => void
}) {
  const addRef = useRef<HTMLButtonElement>(null)
  const labelRef = useRef<HTMLInputElement>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const selectAllRef = useRef<HTMLInputElement>(null)
  const deleteRef = useRef<HTMLButtonElement>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const wasOpen = useRef(false)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const searchId = useId()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [confirmDelete, setConfirmDelete] = useState(false)
  const visible = filterSavedProjects(projects, query)
  const visibleKeys = visible.map((row) => row.key)
  const selection = visibleSelectionState(visibleKeys, selected)
  const open = editor != null
  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = selection === 'some'
  }, [selection])
  useEffect(() => {
    const live = new Set(projects.map((project, index) => savedProjectKey(project, index)))
    setSelected((prev) => {
      let changed = false
      const next = new Set<string>()
      prev.forEach((key) => {
        if (live.has(key)) next.add(key)
        else changed = true
      })
      return changed ? next : prev
    })
  }, [projects])
  useEffect(() => {
    if (!open) {
      if (!wasOpen.current) return
      wasOpen.current = false
      const back = returnFocus.current
      const timer = window.setTimeout(() => back?.focus(), 0)
      return () => window.clearTimeout(timer)
    }
    wasOpen.current = true
    labelRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCloseRef.current()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])
  const canSave = Boolean(editor && editor.url.trim())
  return (
    <div className="rounded-xl border border-border p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-text">Saved projects</div>
          <p className="mt-1 text-xs text-text-muted">
            Named remotes for Scheduled → New issue.
          </p>
        </div>
        <button
          ref={addRef}
          type="button"
          className="vd-btn vd-btn-secondary shrink-0"
          aria-label="Add project"
          onClick={() => {
            returnFocus.current = addRef.current
            onOpenNew()
          }}
        >
          +
        </button>
      </div>
      {status}
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={reloadDisabled}
          onClick={onReload}
        >
          Reload from tokens
        </button>
      </p>
      {projects.length === 0 ? (
        <p className="mt-3 text-xs text-text-muted">{emptyText}</p>
      ) : (
        <>
          <label className="field mt-3" htmlFor={searchId}>
            <span>Search</span>
            <input
              ref={searchRef}
              id={searchId}
              type="search"
              placeholder="Name"
              value={query}
              autoComplete="off"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <label className="field-check text-sm text-text">
              <input
                ref={selectAllRef}
                type="checkbox"
                className="vd-checkbox"
                aria-label="Select all saved projects"
                checked={selection === 'all'}
                disabled={visible.length === 0}
                onChange={() => {
                  setSelected((prev) =>
                    toggleVisibleSelection(visibleKeys, prev, selection !== 'all'),
                  )
                }}
              />
              <span>Select all</span>
            </label>
            {selected.size > 0 ? (
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-xs text-text-muted">
                  {selected.size} selected
                </span>
                <button
                  type="button"
                  className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
                  onClick={() => setSelected(new Set())}
                >
                  Clear
                </button>
                <button
                  ref={deleteRef}
                  type="button"
                  className="vd-btn vd-btn-danger px-3 py-1 text-xs"
                  onClick={() => setConfirmDelete(true)}
                >
                  Delete selected
                </button>
              </span>
            ) : null}
          </div>
          {visible.length === 0 ? (
            <p className="mt-3 text-xs text-text-muted">No projects match that name.</p>
          ) : (
            <ul className="mt-3 divide-y divide-border" aria-label="Saved projects">
              {visible.map((row) => (
                <li
                  key={`${row.key}-${row.index}`}
                  className="flex items-center gap-3 py-2"
                >
                  <label className="flex min-w-0 flex-1 items-center gap-3 text-sm text-text">
                    <input
                      type="checkbox"
                      className="vd-checkbox shrink-0"
                      aria-label={`Select ${row.name}`}
                      checked={selected.has(row.key)}
                      onChange={() => {
                        setSelected((prev) => {
                          const next = new Set(prev)
                          if (next.has(row.key)) next.delete(row.key)
                          else next.add(row.key)
                          return next
                        })
                      }}
                    />
                    <span className="min-w-0 truncate">{row.name}</span>
                  </label>
                  <RowActions
                    editLabel={`Edit ${row.name}`}
                    removeLabel={`Remove ${row.name}`}
                    onEdit={(current) => {
                      returnFocus.current = current
                      onOpenEdit(row.index)
                    }}
                    onRemove={() => {
                      onRemoveAt(row.index)
                      window.setTimeout(() => addRef.current?.focus(), 0)
                    }}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete ${selected.size} saved ${selected.size === 1 ? 'project' : 'projects'}?`}
        body="They leave Saved projects. Press Save at the top of Settings to store this change."
        confirmLabel="Delete"
        danger
        onCancel={() => {
          setConfirmDelete(false)
          window.setTimeout(() => deleteRef.current?.focus(), 0)
        }}
        onConfirm={() => {
          onRemoveSelected(selected)
          setSelected(new Set())
          setConfirmDelete(false)
          window.setTimeout(() => {
            if (searchRef.current) searchRef.current.focus()
            else addRef.current?.focus()
          }, 0)
        }}
      />
      {editor ? (
        <div
          className="vd-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) onClose()
          }}
        >
          <form
            className="vd-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            onSubmit={(e) => {
              e.preventDefault()
              if (canSave) onSave()
            }}
          >
            <h3 id={titleId} className="vd-modal-title">
              {editor.index == null ? 'Add project' : 'Edit project'}
            </h3>
            <label className="field">
              <span>Label</span>
              <input
                ref={labelRef}
                value={editor.label}
                placeholder="demo"
                onChange={(e) => onChangeEditor({ ...editor, label: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Git URL</span>
              <input
                value={editor.url}
                placeholder="https://gitlab.com/group/repo.git"
                spellCheck={false}
                required
                onChange={(e) => onChangeEditor({ ...editor, url: e.target.value })}
              />
            </label>
            <label className="field">
              <span>Default target</span>
              <input
                value={editor.target_branch}
                placeholder="develop"
                spellCheck={false}
                onChange={(e) =>
                  onChangeEditor({ ...editor, target_branch: e.target.value })
                }
              />
            </label>
            <div className="vd-modal-actions">
              <button type="button" className="vd-btn vd-btn-secondary" onClick={onClose}>
                Cancel
              </button>
              <button type="submit" className="vd-btn vd-btn-primary" disabled={!canSave}>
                Save
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  )
}

export function SettingsPage() {
  const { section: sectionParam = '' } = useParams()
  const navigate = useNavigate()
  const section: SettingsSection = settingsSectionFromParam(sectionParam) ?? 'jira'
  const live = useLive()
  const pushSettings = live.setSettings
  const [settings, setSettings] = useState<SettingsPayload | null>(
    () => live.settings,
  )
  const [repoSetEditor, setRepoSetEditor] = useState<{
    index: number | null
    name: string
    repositories: string[]
  } | null>(null)
  const repoSetTitleId = useId()
  const [projectEditor, setProjectEditor] = useState<ProjectEditorState | null>(null)
  const projectTitleId = useId()
  const [draft, setDraft] = useState<Draft | null>(() =>
    live.settings ? fromSettings(live.settings) : null,
  )
  const settingsRef = useRef(settings)
  settingsRef.current = settings
  const draftRef = useRef(draft)
  draftRef.current = draft
  const projectsLoaded = useRef(Array.isArray(live.settings?.project_repositories))
  const [projectImport, setProjectImport] = useState<
    | { state: 'idle' | 'loading' }
    | {
        state: 'done'
        added: number
        gitlab: number
        azure: number
        errors: string[]
      }
    | { state: 'error'; message: string }
  >({ state: 'idle' })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jiraResult, setJiraResult] = useState<JiraConnectionTestResult | null>(null)
  const [gitlabResults, setGitlabResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [jiraTesting, setJiraTesting] = useState(false)
  const [gitlabTestingIdx, setGitlabTestingIdx] = useState<number | null>(null)
  const [azureResults, setAzureResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [azureTestingIdx, setAzureTestingIdx] = useState<number | null>(null)
  const [saved, setSaved] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [dirtyKeys, setDirtyKeys] = useState<Set<keyof Draft>>(new Set())
  const dirtyRef = useRef(false)

  const touch = (key: keyof Draft) => {
    setDirtyKeys((prev) => {
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }

  const mark = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    touch(key)
    setDraft((d) => (d ? { ...d, [key]: value } : d))
  }

  useEffect(() => {
    const want = canonicalSettingsPath(sectionParam)
    if (settingsHere(sectionParam) !== want) navigate(want, { replace: true })
  }, [navigate, sectionParam])

  useEffect(() => {
    if (settings || !live.settings) return
    if (Array.isArray(live.settings.project_repositories)) {
      projectsLoaded.current = true
    }
    setSettings(live.settings)
    setDraft(fromSettings(live.settings))
  }, [live.settings, settings])

  useEffect(() => {
    const ac = new AbortController()
    void fetchSettings(ac.signal)
      .then((s) => {
        if (ac.signal.aborted) return
        const serverHasList = Array.isArray(s.project_repositories)
        if (serverHasList) projectsLoaded.current = true
        setSettings((cur) => {
          if (!serverHasList && Array.isArray(cur?.project_repositories)) {
            return { ...s, project_repositories: cur.project_repositories }
          }
          return s
        })
        if (!dirtyRef.current) {
          setDraft((d) => {
            const next = fromSettings(s)
            if (!serverHasList && d?.project_repositories?.length) {
              next.project_repositories = d.project_repositories
            }
            return next
          })
        }
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return
        setError(e instanceof Error ? e.message : 'Could not load settings')
      })
    return () => ac.abort()
  }, [])

  const loadProjects = useCallback(async () => {
    const editor = projectsLoaded.current
      ? (draftRef.current?.project_repositories ?? [])
      : null
    setProjectImport({ state: 'loading' })
    try {
      const result = await importAccessibleProjects(editor)
      projectsLoaded.current = true
      const repositories = result.project_repositories ?? []
      setDraft((d) => (d ? { ...d, project_repositories: repositories } : d))
      setSettings((s) => (s ? { ...s, project_repositories: repositories } : s))
      const current = settingsRef.current
      if (current) {
        pushSettings({ ...current, project_repositories: repositories })
      }
      setProjectImport({
        state: 'done',
        added: result.added,
        gitlab: result.gitlab,
        azure: result.azure,
        errors: result.errors ?? [],
      })
    } catch (e) {
      setProjectImport({
        state: 'error',
        message: e instanceof Error ? e.message : 'Could not load repositories',
      })
    }
  }, [pushSettings])

  useEffect(() => {
    if (section === 'projects') return
    setProjectEditor(null)
    setRepoSetEditor(null)
  }, [section])

  const onSave = async () => {
    if (!draft || modelsLoading) return
    setSaving(true)
    setError(null)
    try {
      for (const r of draft.gitlab_cred_rows) {
        if (r.host.trim() && !r.pat_configured && !r.pat.trim()) {
          throw new Error(`GitLab host "${r.host}" needs a PAT`)
        }
      }
      for (const r of draft.azure_cred_rows) {
        const host = r.host.trim()
        if (!host) continue
        const problem = azureCollectionProblem(host)
        if (problem) throw new Error(problem)
        if (!r.pat_configured && !r.pat.trim()) {
          throw new Error(`Azure DevOps host "${host}" needs a PAT`)
        }
      }
      const body: Parameters<typeof patchSettings>[0] = {}
      if (dirtyKeys.has('jira_enabled')) body.jira_enabled = draft.jira_enabled
      if (dirtyKeys.has('jira_host')) body.jira_host = draft.jira_host.trim()
      if (dirtyKeys.has('jira_board_id')) body.jira_board_id = draft.jira_board_id.trim()
      if (dirtyKeys.has('jira_projects')) body.jira_projects = draft.jira_projects.trim()
      if (dirtyKeys.has('poll_interval_seconds')) {
        body.poll_interval_seconds = Number(draft.poll_interval_seconds)
      }
      if (dirtyKeys.has('jira_trigger_user')) {
        body.jira_trigger_user = draft.jira_trigger_user
      }
      if (dirtyKeys.has('jira_trigger_label')) {
        body.jira_trigger_label = draft.jira_trigger_label
      }
      if (dirtyKeys.has('gitlab_trigger_user')) {
        body.gitlab_trigger_user = draft.gitlab_trigger_user
      }
      if (dirtyKeys.has('azure_trigger_user')) {
        body.azure_trigger_user = draft.azure_trigger_user
      }
      if (dirtyKeys.has('gitlab_webhook_enabled')) {
        body.gitlab_webhook_enabled = draft.gitlab_webhook_enabled
      }
      if (dirtyKeys.has('azure_webhook_enabled')) {
        body.azure_webhook_enabled = draft.azure_webhook_enabled
      }
      if (dirtyKeys.has('gitlab_webhook_secret') && draft.gitlab_webhook_secret.trim()) {
        body.gitlab_webhook_secret = draft.gitlab_webhook_secret.trim()
      }

      if (dirtyKeys.has('max_concurrent_jobs')) {
        body.max_concurrent_jobs = Number(draft.max_concurrent_jobs)
      }
      if (dirtyKeys.has('temp_clone_max_age_days')) {
        body.temp_clone_max_age_days = Number(draft.temp_clone_max_age_days)
      }
      if (dirtyKeys.has('agent_task_timeout_seconds')) {
        body.agent_task_timeout_seconds = Number(draft.agent_task_timeout_seconds)
      }
      if (dirtyKeys.has('agent_task_max_retries')) {
        body.agent_task_max_retries = Number(draft.agent_task_max_retries)
      }
      if (dirtyKeys.has('agent_task_max_incomplete_retries')) {
        body.agent_task_max_incomplete_retries = Number(
          draft.agent_task_max_incomplete_retries,
        )
      }
      if (dirtyKeys.has('default_model')) body.default_model = draft.default_model.trim()
      if (dirtyKeys.has('default_review_model')) {
        body.default_review_model = draft.default_review_model.trim()
      }
      if (dirtyKeys.has('agent_backend')) body.agent_backend = draft.agent_backend
      if (dirtyKeys.has('gitlab_cred_rows')) {
        body.gitlab_credentials = draft.gitlab_cred_rows
          .map((r) => {
            const host = r.host.trim()
            const prev = (r.original_host || '').trim()
            const row: { host: string; pat?: string; previous_host?: string } = { host }
            if (r.pat.trim()) row.pat = r.pat.trim()
            if (prev && prev.toLowerCase() !== host.toLowerCase()) {
              row.previous_host = prev
            }
            return row
          })
          .filter((r) => r.host)
      }
      if (dirtyKeys.has('azure_cred_rows')) {
        body.azure_credentials = draft.azure_cred_rows
          .map((r) => {
            const host = r.host.trim()
            const prev = (r.original_host || '').trim()
            const row: { host: string; pat?: string; previous_host?: string } = { host }
            if (r.pat.trim()) row.pat = r.pat.trim()
            if (prev && prev.toLowerCase() !== host.toLowerCase()) {
              row.previous_host = prev
            }
            return row
          })
          .filter((r) => r.host)
      }
      if (dirtyKeys.has('work_modes')) {
        body.work_modes = draft.work_modes
          .map((row) => ({
            name: row.name.trim().toLowerCase(),
            behavior: row.behavior || 'build',
            agent: row.agent.trim(),
            builtin: Boolean(row.builtin),
          }))
          .filter((row) => row.name && row.agent)
      }
      if (dirtyKeys.has('repository_sets')) {
        body.repository_sets = draft.repository_sets
          .map((row) => ({
            name: row.name.trim(),
            repositories: row.repositories.map((url) => url.trim()).filter(Boolean),
          }))
          .filter((row) => row.name && row.repositories.length >= 2)
      }
      if (dirtyKeys.has('project_repositories')) {
        body.project_repositories = draft.project_repositories
          .map((p) => ({
            label: p.label.trim(),
            url: p.url.trim(),
            target_branch: (p.target_branch || '').trim(),
            source_branch: (p.source_branch || '').trim(),
          }))
          .filter((p) => p.url)
        if (!projectsLoaded.current) {
          body.project_repositories_append = body.project_repositories
          delete body.project_repositories
        }
      }
      if (dirtyKeys.has('jira_api_token') && draft.jira_api_token.trim()) {
        body.jira_api_token = draft.jira_api_token.trim()
      }
      const updated = await patchSettings(body)
      const merged: SettingsPayload = { ...updated }
      delete merged.project_repositories
      if (projectsLoaded.current) {
        merged.project_repositories =
          body.project_repositories ??
          settings?.project_repositories ??
          draft.project_repositories
      }
      setSettings(merged)
      pushSettings(merged)
      setDraft(
        fromSettings(
          projectsLoaded.current
            ? merged
            : { ...merged, project_repositories: draft.project_repositories },
        ),
      )
      setDirtyKeys(new Set())
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1800)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const dirty = settings != null && draft != null && draftChanged(draft, settings)
  dirtyRef.current = dirty

  if (!settings || !draft) {
    return <p className="text-sm text-text-muted">{error || 'Loading settings…'}</p>
  }

  const saveButton = (
    <button
      type="button"
      className="go"
      disabled={saving || modelsLoading || !dirty}
      onClick={() => void onSave()}
    >
      {saving ? (
        <>
          <Spinner /> Saving…
        </>
      ) : modelsLoading ? (
        <>
          <Spinner /> Loading models…
        </>
      ) : saved ? (
        'Saved'
      ) : (
        'Save'
      )}
    </button>
  )

  return (
    <section className="space-y-5">
      <PageHeader
        title="Settings"
        description="Leave a secret blank to keep the saved value."
        actions={saveButton}
      />

      <div className="vd-seg vd-seg-wide">
        {(
          [
            ['jira', 'Jira'],
            ['gitlab', 'GitLab'],
            ['azure', 'Azure'],
            ['projects', 'Projects'],
            ['model', 'Agent'],
            ['runtime', 'Runtime'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => {
              if (id === 'model' && section !== 'model') setModelsLoading(true)
              navigate(settingsSectionPath(id))
            }}
            aria-pressed={section === id}
            className={`vd-seg-btn ${section === id ? 'is-on' : ''}`}
          >
            {label}
          </button>
        ))}
      </div>

      {section === 'jira' && (
      <div key="jira" className="vd-fade space-y-5">
      <div className="vd-panel px-5 py-4">
      <label className="field field-check !mb-0">
        <input
          type="checkbox"
          checked={draft.jira_enabled}
          onChange={(e) => mark('jira_enabled', e.target.checked)}
        />
        <span>Enabled</span>
      </label>
      <p className="mt-2 text-xs text-text-muted">
        Off: the board poller stays idle. Comments still post when a
        host and API token are saved. GitLab and Azure jobs still run.
        Test Jira still works so you can check the token before turning
        this on.
      </p>
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-2">
      <SettingsGroup title="Connection">
      <label className="field">
        <span>Host</span>
        <input value={draft.jira_host} onChange={(e) => mark('jira_host', e.target.value)} />
      </label>
      <label className="field">
        <span>
          API token{settings.jira_token_configured ? '' : ' (missing)'}
        </span>
        <input
          type="password"
          value={draft.jira_api_token}
          autoComplete="new-password"
          onChange={(e) => mark('jira_api_token', e.target.value)}
        />
      </label>
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={jiraTesting}
          onClick={() => {
            setJiraTesting(true)
            const host = draft.jira_host.trim()
            const token = draft.jira_api_token.trim()
            void testJiraConnection({
              ...(host ? { host } : {}),
              ...(token ? { api_token: token } : {}),
            })
              .then(setJiraResult)
              .catch((e: Error) => setJiraResult({ ok: false, error: e.message }))
              .finally(() => setJiraTesting(false))
          }}
        >
          {jiraTesting ? 'Testing…' : 'Test Jira'}
        </button>
      </p>
      {jiraResult && (
        <p className={jiraResult.ok ? 'quiet' : 'err'}>
          {jiraResult.ok
            ? `${jiraResult.message || 'OK'} · ${jiraResult.auth_mode || ''}`
            : jiraResult.error}
        </p>
      )}
      {jiraResult?.ok && (jiraResult.projects || []).length > 0 && (
        <ul className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-text-secondary">
          {(jiraResult.projects || []).map((p) => (
            <li key={String(p.id ?? p.key)}>
              <span className="text-text">{p.key}</span>
              {p.name ? ` — ${p.name}` : ''}
            </li>
          ))}
        </ul>
      )}

      </SettingsGroup>
      <div className="grid gap-5">
      <SettingsGroup title="Board">
      <label className="field">
        <span>Board ID</span>
        <input
          value={draft.jira_board_id}
          onChange={(e) => mark('jira_board_id', e.target.value)}
          placeholder="2, 5"
        />
        <span className="text-xs text-text-muted">
          Agile board ids from each board URL, separated by commas. Each
          Scrum board still uses only its first active sprint.
        </span>
      </label>
      <label className="field">
        <span>Project keys</span>
        <input
          value={draft.jira_projects}
          onChange={(e) => mark('jira_projects', e.target.value)}
          placeholder="KAN, PLATFORM"
        />
        <span className="text-xs text-text-muted">
          Keys used to read a ticket id from a merge request title. Separate
          with commas.
        </span>
      </label>
      <label className="field">
        <span>Poll interval (seconds)</span>
        <input
          type="number"
          value={draft.poll_interval_seconds}
          onChange={(e) => mark('poll_interval_seconds', Number(e.target.value))}
        />
        <span className="text-xs text-text-muted">
          How often the poller reads the board.
        </span>
      </label>

      </SettingsGroup>
      <SettingsGroup title="Intake">
      <label className="field">
        <span>Trigger user</span>
        <input
          value={draft.jira_trigger_user}
          onChange={(e) => mark('jira_trigger_user', e.target.value)}
          placeholder="Beratersari, jira ai bot"
        />
        <span className="text-xs text-text-muted">
          Jira display name or username, no @. To Do issues assigned to this
          name are accepted. Comments that mention the same name tag the bot.
          Comma-separated if there is more than one.
        </span>
      </label>
      <label className="field">
        <span>Trigger label</span>
        <input
          value={draft.jira_trigger_label}
          onChange={(e) => mark('jira_trigger_label', e.target.value)}
          placeholder="bot, ai-assist"
        />
        <span className="text-xs text-text-muted">
          Optional. When set, To Do intake needs the trigger user AND one of
          these labels. Leave empty to accept any To Do ticket assigned to the
          bot. Comma-separated if there is more than one.
        </span>
      </label>
      </SettingsGroup>
      </div>
      </div>
      </div>
      )}

      {section === 'gitlab' && (
      <div key="gitlab" className="vd-fade space-y-5">
      <div className="vd-panel px-5 py-4">
        <label className="field field-check !mb-0">
          <input
            type="checkbox"
            checked={draft.gitlab_webhook_enabled}
            onChange={(e) => mark('gitlab_webhook_enabled', e.target.checked)}
          />
          <span>Enabled</span>
        </label>
        <p className="mt-2 text-xs text-text-muted">
          Project webhook. Off: comment and merge-request events are ignored.
        </p>
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-2">
      <SettingsGroup title="Credentials">
      <p className="text-xs text-text-muted">
        One personal access token per GitLab host.
      </p>
      {draft.gitlab_cred_rows.map((row, idx) => (
        <div key={idx}>
          <label className="field">
            <span>Host {row.pat_configured ? '(PAT stored)' : ''}</span>
            <input
              value={row.host}
              onChange={(e) => {
                touch('gitlab_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.gitlab_cred_rows]
                  rows[idx] = { ...rows[idx], host: e.target.value }
                  return { ...d, gitlab_cred_rows: rows }
                })
              }}
            />
          </label>
          <label className="field">
            <span>PAT</span>
            <input
              type="password"
              value={row.pat}
              autoComplete="new-password"
              onChange={(e) => {
                touch('gitlab_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.gitlab_cred_rows]
                  rows[idx] = { ...rows[idx], pat: e.target.value }
                  return { ...d, gitlab_cred_rows: rows }
                })
              }}
            />
          </label>
          <p className="actions">
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              disabled={gitlabTestingIdx === idx}
              onClick={() => {
                setGitlabTestingIdx(idx)
                void (async () => {
                  try {
                    const r = await testGitlabConnection({
                      host: row.host.trim(),
                      pat: row.pat.trim() || undefined,
                    })
                    setGitlabResults((m) => ({ ...m, [row.host.trim()]: r }))
                  } catch (e) {
                    setGitlabResults((m) => ({
                      ...m,
                      [row.host.trim()]: {
                        ok: false,
                        error: e instanceof Error ? e.message : 'Test failed',
                      },
                    }))
                  } finally {
                    setGitlabTestingIdx(null)
                  }
                })()
              }}
            >
              {gitlabTestingIdx === idx ? 'Testing…' : 'Test'}
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-danger"
              onClick={() => {
                touch('gitlab_cred_rows')
                setDraft((d) =>
                  d
                    ? {
                        ...d,
                        gitlab_cred_rows: d.gitlab_cred_rows.filter((_, i) => i !== idx),
                      }
                    : d,
                )
              }}
            >
              Remove host
            </button>
          </p>
          {gitlabResults[row.host.trim()] && (
            <p className={gitlabResults[row.host.trim()].ok ? 'quiet' : 'err'}>
              {gitlabResults[row.host.trim()].message ||
                gitlabResults[row.host.trim()].error ||
                ''}
            </p>
          )}
        </div>
      ))}
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          onClick={() => {
            touch('gitlab_cred_rows')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    gitlab_cred_rows: [
                      ...d.gitlab_cred_rows,
                      { host: '', pat: '', pat_configured: false, original_host: '' },
                    ],
                  }
                : d,
            )
          }}
        >
          Add GitLab host
        </button>
      </p>
      </SettingsGroup>

      <div className="grid gap-5">
      <SettingsGroup title="Trigger username">
      <label className="field">
        <span>Trigger user</span>
        <input
          value={draft.gitlab_trigger_user}
          onChange={(e) => mark('gitlab_trigger_user', e.target.value)}
          placeholder="berat_ai, yaver"
        />
        <span className="text-xs text-text-muted">
          GitLab username, no @. Start a job with @name /yaver on a
          merge-request comment. Mention without /yaver gets a usage note
          in the thread. Comments from this user are ignored. Comma-separated
          if there is more than one. /review and /ask stay silent unless
          Code review (below) is on.
        </span>
      </label>
      </SettingsGroup>

      <SettingsGroup title="Project webhook">
        <div className="text-sm">
        <p className="mt-1 text-xs text-text-muted">
          Register a project hook for comments and merge-request events.
        </p>
        <label className="field">
          <span>
            Secret{' '}
            {settings?.gitlab_webhook_secret_configured ? '(stored)' : ''}
          </span>
          <input
            type="password"
            value={draft.gitlab_webhook_secret}
            autoComplete="new-password"
            onChange={(e) => mark('gitlab_webhook_secret', e.target.value)}
            placeholder="leave blank to keep current"
          />
        </label>
        <span className="text-xs text-text-muted">
          @bot /review and /ask on merge and pull requests run a review.
          Assign the bot as reviewer, or open a request that already lists
          it, to start one. New commits do not re-review. No push or new
          request. Work-item /review and /ask stay silent.
        </span>
        <p className="mt-2 font-mono text-[11px] text-text-secondary">
          URL: http://&lt;host&gt;:{settings?.dashboard_port ?? 8080}
          {settings?.gitlab_webhook_path || '/yaver/webhook/gitlab'}
        </p>
        </div>
      </SettingsGroup>
      </div>
      </div>
      </div>
      )}

      {section === 'azure' && (
      <div key="azure" className="vd-fade space-y-5">
      <div className="vd-panel px-5 py-4">
        <label className="field field-check !mb-0">
          <input
            type="checkbox"
            checked={draft.azure_webhook_enabled}
            onChange={(e) => mark('azure_webhook_enabled', e.target.checked)}
          />
          <span>Enabled</span>
        </label>
        <p className="mt-2 text-xs text-text-muted">
          Service hook. Off: pull-request and work-item events are ignored.
        </p>
      </div>
      <div className="grid items-start gap-5 lg:grid-cols-2">
      <SettingsGroup title="Credentials">
      <p className="text-xs text-text-muted">
        Add the collection URL, not the hostname. Use
        https://tfs.example.com/tfs/DefaultCollection when the server
        has a /tfs virtual directory, or
        https://tfs.example.com/DefaultCollection when it does not. A
        host-only URL is rejected on save.
      </p>
      {draft.azure_cred_rows.map((row, idx) => (
        <div key={idx}>
          <label className="field">
            <span>
              Collection URL {row.pat_configured ? '(PAT stored)' : ''}
            </span>
            <input
              value={row.host}
              onChange={(e) => {
                touch('azure_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.azure_cred_rows]
                  rows[idx] = { ...rows[idx], host: e.target.value }
                  return { ...d, azure_cred_rows: rows }
                })
              }}
              placeholder="https://tfs.example.com/tfs/DefaultCollection"
            />
          </label>
          <label className="field">
            <span>PAT</span>
            <input
              type="password"
              value={row.pat}
              autoComplete="new-password"
              onChange={(e) => {
                touch('azure_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.azure_cred_rows]
                  rows[idx] = { ...rows[idx], pat: e.target.value }
                  return { ...d, azure_cred_rows: rows }
                })
              }}
            />
          </label>
          <p className="actions">
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              disabled={azureTestingIdx === idx}
              onClick={() => {
                setAzureTestingIdx(idx)
                void (async () => {
                  try {
                    const r = await testAzureConnection({
                      host: row.host.trim(),
                      pat: row.pat.trim() || undefined,
                    })
                    setAzureResults((m) => ({ ...m, [row.host.trim()]: r }))
                  } catch (e) {
                    setAzureResults((m) => ({
                      ...m,
                      [row.host.trim()]: {
                        ok: false,
                        error: e instanceof Error ? e.message : 'Test failed',
                      },
                    }))
                  } finally {
                    setAzureTestingIdx(null)
                  }
                })()
              }}
            >
              {azureTestingIdx === idx ? 'Testing…' : 'Test'}
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-danger"
              onClick={() => {
                touch('azure_cred_rows')
                setDraft((d) =>
                  d
                    ? {
                        ...d,
                        azure_cred_rows: d.azure_cred_rows.filter((_, i) => i !== idx),
                      }
                    : d,
                )
              }}
            >
              Remove collection
            </button>
          </p>
          {azureResults[row.host.trim()] && (
            <p className={azureResults[row.host.trim()].ok ? 'quiet' : 'err'}>
              {azureResults[row.host.trim()].message ||
                azureResults[row.host.trim()].error ||
                ''}
            </p>
          )}
        </div>
      ))}
      <p className="actions">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          onClick={() => {
            touch('azure_cred_rows')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    azure_cred_rows: [
                      ...d.azure_cred_rows,
                      { host: '', pat: '', pat_configured: false, original_host: '' },
                    ],
                  }
                : d,
            )
          }}
        >
          Add collection
        </button>
      </p>
      </SettingsGroup>

      <div className="grid gap-5">
      <SettingsGroup title="Trigger username">
      <label className="field">
        <span>Trigger user</span>
        <input
          value={draft.azure_trigger_user}
          onChange={(e) => mark('azure_trigger_user', e.target.value)}
          placeholder="yaver, Yaver Bot"
        />
        <span className="text-xs text-text-muted">
          Azure DevOps display name or unique name, no @. PR comments start
          a job with @name /yaver. Work items start when Assigned To matches
          one of these names. Comma-separated if there is more than one.
        </span>
      </label>
      </SettingsGroup>

      <SettingsGroup title="Service hook">
        <div className="text-sm">
        <p className="mt-1 text-xs text-text-muted">
          Register a project Web Hook for pull-request commented,
          pull-request updated / merged / abandoned, and work item
          created / updated / commented. New work starts when a To Do or
          In Progress item is assigned to the bot (or New, Active, Doing).
          Resolved and Done are ignored. Moving In Progress back to To Do
          does not re-queue. After a plan, mention the bot with
          /planRefactor or /planExecute — not tags. Mention without those
          commands gets a usage note on the work item.
        </p>
        <p className="mt-2 font-mono text-[11px] text-text-secondary">
          URL: http://&lt;host&gt;:{settings?.dashboard_port ?? 8080}
          {settings?.azure_webhook_path || '/yaver/webhook/azure'}
        </p>
        </div>
      </SettingsGroup>
      </div>
      </div>
      </div>
      )}

      {section === 'projects' && (
      <div key="projects" className="vd-fade space-y-5">
        <ProjectRepoList
          projects={draft.project_repositories}
          emptyText={
            Array.isArray(settings.project_repositories)
              ? 'No saved projects yet.'
              : 'Press Reload from tokens to load saved projects.'
          }
          editor={projectEditor}
          titleId={projectTitleId}
          reloadDisabled={projectImport.state === 'loading'}
          onReload={() => {
            setProjectEditor(null)
            void loadProjects()
          }}
          status={
            <>
              {projectImport.state === 'loading' ? (
                <p className="mt-2 flex items-center gap-2 text-xs text-text-muted">
                  <Spinner /> Loading repositories…
                </p>
              ) : null}
              {projectImport.state === 'done' ? (
                <p className="mt-2 text-xs text-text-muted">
                  {projectImport.gitlab} GitLab and {projectImport.azure} Azure
                  repositories. Added {projectImport.added} to saved projects.
                  {projectImport.errors.length
                    ? ` ${projectImport.errors.join(' ')}`
                    : ''}
                </p>
              ) : null}
              {projectImport.state === 'error' ? (
                <p className="mt-2 text-xs text-danger-text">{projectImport.message}</p>
              ) : null}
            </>
          }
          onOpenNew={() =>
            setProjectEditor({
              index: null,
              label: '',
              url: '',
              target_branch: 'develop',
              source_branch: '',
            })
          }
          onOpenEdit={(index) => {
            const row = draft.project_repositories[index]
            if (!row) return
            setProjectEditor({
              index,
              label: row.label,
              url: row.url,
              target_branch: row.target_branch || '',
              source_branch: row.source_branch || '',
            })
          }}
          onClose={() => setProjectEditor(null)}
          onChangeEditor={setProjectEditor}
          onSave={() => {
            if (!projectEditor || !projectEditor.url.trim()) return
            const row: ProjectRepository = {
              label: projectEditor.label.trim(),
              url: projectEditor.url.trim(),
              target_branch: projectEditor.target_branch.trim(),
              source_branch: (projectEditor.source_branch || '').trim(),
            }
            const index = projectEditor.index
            touch('project_repositories')
            setDraft((d) => {
              if (!d) return d
              const next = d.project_repositories.slice()
              if (index == null) next.push(row)
              else next[index] = row
              return { ...d, project_repositories: next }
            })
            setProjectEditor(null)
          }}
          onRemoveAt={(index) => {
            touch('project_repositories')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    project_repositories: d.project_repositories.filter((_, i) => i !== index),
                  }
                : d,
            )
            setProjectEditor((ed) => {
              if (!ed || ed.index == null) return ed
              if (ed.index === index) return null
              if (ed.index > index) return { ...ed, index: ed.index - 1 }
              return ed
            })
          }}
          onRemoveSelected={(keys) => {
            const current = draft.project_repositories
            touch('project_repositories')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    project_repositories: withoutSelectedProjects(
                      d.project_repositories,
                      keys,
                    ),
                  }
                : d,
            )
            setProjectEditor((ed) => {
              if (!ed || ed.index == null) return ed
              const next = editorIndexAfterRemoval(ed.index, current, keys)
              if (next == null) return null
              if (next === ed.index) return ed
              return { ...ed, index: next }
            })
          }}
        />
        <RepoSetList
          sets={draft.repository_sets}
          projects={draft.project_repositories}
          editor={repoSetEditor}
          titleId={repoSetTitleId}
          onOpenNew={() =>
            setRepoSetEditor({ index: null, name: '', repositories: [] })
          }
          onOpenEdit={(index) => {
            const row = draft.repository_sets[index]
            if (!row) return
            setRepoSetEditor({
              index,
              name: row.name,
              repositories: [...row.repositories],
            })
          }}
          onClose={() => setRepoSetEditor(null)}
          onChangeEditor={setRepoSetEditor}
          onSave={(urls) => {
            if (!repoSetEditor) return
            const name = repoSetEditor.name.trim()
            const repositories = urls.map((url) => url.trim()).filter(Boolean)
            if (!name || repositories.length < 2) return
            touch('repository_sets')
            setDraft((d) => {
              if (!d) return d
              const next = d.repository_sets.slice()
              const row: RepositorySet = { name, repositories }
              if (repoSetEditor.index == null) next.push(row)
              else next[repoSetEditor.index] = row
              return { ...d, repository_sets: next }
            })
            setRepoSetEditor(null)
          }}
          onRemoveAt={(index) => {
            touch('repository_sets')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    repository_sets: d.repository_sets.filter((_, i) => i !== index),
                  }
                : d,
            )
            setRepoSetEditor((ed) => {
              if (!ed || ed.index == null) return ed
              if (ed.index === index) return null
              if (ed.index > index) return { ...ed, index: ed.index - 1 }
              return ed
            })
          }}
        />
      </div>
      )}

      {section === 'model' && (
      <div key="model" className="vd-fade grid items-start gap-5 lg:grid-cols-2">
      <SettingsGroup>
      <label className="field">
        <span>Worker</span>
        <select
          value={draft.agent_backend}
          onChange={(e) => {
            setModelsLoading(true)
            mark('agent_backend', e.target.value)
          }}
        >
          <option value="opencode">OpenCode</option>
          <option value="codex">Codex</option>
          <option value="claude">Claude Code</option>
        </select>
        <span className="mt-1 block text-xs text-text-muted">
          Default worker for new jobs. A Backend field in the issue overrides
          this.
        </span>
      </label>
      <p className="text-xs text-text-muted">
        Default model for plan, build, test, and /yaver. The list
        follows the selected worker.
      </p>
      <ModelField
        label="Default model"
        value={draft.default_model}
        onChange={(v) => mark('default_model', v)}
        backend={draft.agent_backend}
        allowEmpty={false}
        showRefresh
        onLoadingChange={setModelsLoading}
      />
      <p className="mt-3 text-xs text-text-muted">
        Model for /review and /ask. Leave empty to use Default model.
      </p>
      <ModelField
        label="Review model"
        value={draft.default_review_model}
        onChange={(v) => mark('default_review_model', v)}
        backend={draft.agent_backend}
        allowEmpty
        showRefresh
        onLoadingChange={setModelsLoading}
      />
      </SettingsGroup>
      <SettingsGroup>
      <ModesPanel
        modes={draft.work_modes}
        onChange={(work_modes) => {
          touch('work_modes')
          setDraft((d) => (d ? { ...d, work_modes } : d))
        }}
      />
      </SettingsGroup>
      </div>
      )}

      {section === 'runtime' && (
      <div key="runtime" className="vd-fade grid items-start gap-5 lg:grid-cols-2">
      <SettingsGroup title="Jobs">
      <label className="field">
        <span>Max concurrent jobs</span>
        <input
          type="number"
          value={draft.max_concurrent_jobs}
          onChange={(e) => mark('max_concurrent_jobs', Number(e.target.value))}
        />
      </label>
      <label className="field">
        <span>Delete unused clones after (days)</span>
        <input
          type="number"
          min={0}
          max={3650}
          value={draft.temp_clone_max_age_days}
          onChange={(e) =>
            mark('temp_clone_max_age_days', Number(e.target.value))
          }
        />
        <span className="text-xs text-text-muted">
          Folders with no live job and last used longer than this are
          deleted hourly. 0 = never auto-delete. Live clones are never
          removed.
        </span>
      </label>
      <label className="field">
        <span>Agent timeout (seconds)</span>
        <input
          type="number"
          min={30}
          max={86400}
          value={draft.agent_task_timeout_seconds}
          onChange={(e) => mark('agent_task_timeout_seconds', Number(e.target.value))}
        />
        <span className="text-xs text-text-muted">
          Seconds allowed for one agent attempt. A saved change applies
          immediately, including a running job.
        </span>
      </label>

      </SettingsGroup>
      <div className="grid gap-5">
      <SettingsGroup title="Retries">
      <label className="field">
        <span>Error / timeout retries</span>
        <input
          type="number"
          min={0}
          max={64}
          value={draft.agent_task_max_retries}
          onChange={(e) => mark('agent_task_max_retries', Number(e.target.value))}
        />
        <span className="text-xs text-text-muted">
          Maximum extra attempts after a timeout or error. 0 means no retry.
        </span>
      </label>
      <label className="field">
        <span>Incomplete-session retries</span>
        <input
          type="number"
          min={0}
          max={256}
          value={draft.agent_task_max_incomplete_retries}
          onChange={(e) =>
            mark('agent_task_max_incomplete_retries', Number(e.target.value))
          }
        />
        <span className="text-xs text-text-muted">
          Extra attempts when a session ends incomplete. Compaction on the
          same session is not counted against this limit.
        </span>
      </label>

      </SettingsGroup>
      <SettingsGroup title="Data location">
      <p className="text-xs text-text-muted">
        Set <span className="font-mono">YAVER_BASE_DIR</span> in .env.
      </p>
      <dl className="space-y-1 font-mono text-[11px] text-text-secondary">
        <div>Base: {settings.base_dir || '(not one folder)'}</div>
        <div>Data: {settings.data_dir || '(default)'}</div>
        <div>Clones: {settings.temp_dir_base || '(default)'}</div>
      </dl>
      </SettingsGroup>
      </div>
      </div>
      )}

      <ConfirmDialog
        open={error != null}
        title="Could not save"
        body={error || ''}
        confirmLabel="OK"
        onConfirm={() => setError(null)}
        onCancel={() => setError(null)}
      />
    </section>
  )
}
