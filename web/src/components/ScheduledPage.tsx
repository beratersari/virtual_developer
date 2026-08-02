import { useEffect, useMemo, useState } from 'react'
import {
  fetchIssueTypes,
  previewScheduleIssue,
  scheduleExistingIssue,
} from '../api'
import type {
  JiraIssueType,
  ScheduleCreateBody,
  ScheduleItem,
  SchedulePreview,
} from '../types'
import { StatusBadge } from './StatusBadge'

function defaultLocalDatetimeValue(): string {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  d.setSeconds(0, 0)
  d.setMinutes(d.getMinutes() + 5)
  return d.toISOString().slice(0, 16)
}

function toIsoLocal(datetimeLocal: string): string {
  return datetimeLocal.length === 16 ? `${datetimeLocal}:00` : datetimeLocal
}

/** Prefer Task / Görev as default when present. */
function pickDefaultIssueType(types: JiraIssueType[]): string {
  const nonSub = types.filter((t) => !t.subtask)
  const pool = nonSub.length ? nonSub : types
  const preferred = ['Task', 'Görev', 'Gorev', 'Story', 'Hikaye']
  for (const name of preferred) {
    const hit = pool.find((t) => t.name.toLowerCase() === name.toLowerCase())
    if (hit) return hit.name
  }
  return pool[0]?.name || 'Task'
}

type CreateMode = 'new' | 'existing'

export function ScheduledPage({
  schedules,
  loading,
  error,
  creating,
  onCreate,
  onCancel,
  onRefresh,
  onOpenIssue,
}: {
  schedules: ScheduleItem[]
  loading: boolean
  error: string | null
  creating: boolean
  onCreate: (body: ScheduleCreateBody) => Promise<void>
  onCancel: (scheduleId: string) => Promise<void>
  onRefresh: () => void
  onOpenIssue?: (issueKey: string) => void
}) {
  const [createMode, setCreateMode] = useState<CreateMode>('existing')

  // --- New issue form ---
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [repositoryUrl, setRepositoryUrl] = useState('')
  const [sourceBranch, setSourceBranch] = useState('develop')
  const [targetBranch, setTargetBranch] = useState('develop')
  const [mode, setMode] = useState<'plan' | 'build'>('build')
  const [issueType, setIssueType] = useState('Task')
  const [issueTypes, setIssueTypes] = useState<JiraIssueType[]>([])
  const [typesLoading, setTypesLoading] = useState(false)
  const [typesError, setTypesError] = useState<string | null>(null)
  const [newScheduledAt, setNewScheduledAt] = useState(defaultLocalDatetimeValue)
  const [newFormError, setNewFormError] = useState<string | null>(null)

  // --- Existing issue flow ---
  const [lookupKey, setLookupKey] = useState('')
  const [preview, setPreview] = useState<SchedulePreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [existingScheduledAt, setExistingScheduledAt] = useState(
    defaultLocalDatetimeValue,
  )
  const [existingSubmitting, setExistingSubmitting] = useState(false)
  const [existingFormError, setExistingFormError] = useState<string | null>(null)

  const [cancellingId, setCancellingId] = useState<string | null>(null)

  const loadIssueTypes = async () => {
    setTypesLoading(true)
    setTypesError(null)
    try {
      const payload = await fetchIssueTypes()
      const list = payload.issue_types || []
      setIssueTypes(list)
      if (payload.error && list.length === 0) {
        setTypesError(payload.error)
      }
      if (list.length) {
        setIssueType((prev) => {
          const stillValid = list.some((t) => t.name === prev)
          return stillValid ? prev : pickDefaultIssueType(list)
        })
      }
    } catch (e) {
      setTypesError(
        e instanceof Error ? e.message : 'Could not load issue types',
      )
    } finally {
      setTypesLoading(false)
    }
  }

  useEffect(() => {
    if (createMode === 'new') {
      void loadIssueTypes()
    }
  }, [createMode])

  const selectableTypes = useMemo(
    () => issueTypes.filter((t) => !t.subtask),
    [issueTypes],
  )

  const switchMode = (m: CreateMode) => {
    setCreateMode(m)
    setNewFormError(null)
    setPreviewError(null)
    setExistingFormError(null)
  }

  const onGetIssue = async () => {
    setPreviewError(null)
    setExistingFormError(null)
    setPreview(null)
    const key = lookupKey.trim().toUpperCase()
    if (!key) {
      setPreviewError('Enter a Jira issue key (e.g. KAN-12)')
      return
    }
    setPreviewLoading(true)
    try {
      const data = await previewScheduleIssue(key)
      setPreview(data)
      setLookupKey(data.issue_key || key)
      setExistingScheduledAt(defaultLocalDatetimeValue())
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : 'Could not load issue')
    } finally {
      setPreviewLoading(false)
    }
  }

  const onScheduleExisting = async (e: React.FormEvent) => {
    e.preventDefault()
    setExistingFormError(null)
    if (!preview?.ok || !preview.template_valid) {
      setExistingFormError('Load a valid issue first with Get')
      return
    }
    if (!existingScheduledAt) {
      setExistingFormError('Choose a run time')
      return
    }
    setExistingSubmitting(true)
    try {
      await scheduleExistingIssue({
        issue_key: preview.issue_key,
        scheduled_at: toIsoLocal(existingScheduledAt),
      })
      setPreview(null)
      setLookupKey('')
      setExistingScheduledAt(defaultLocalDatetimeValue())
      onRefresh()
    } catch (err) {
      setExistingFormError(
        err instanceof Error ? err.message : 'Schedule failed',
      )
    } finally {
      setExistingSubmitting(false)
    }
  }

  const onCreateNew = async (e: React.FormEvent) => {
    e.preventDefault()
    setNewFormError(null)
    if (!title.trim()) {
      setNewFormError('Title is required')
      return
    }
    if (!repositoryUrl.trim()) {
      setNewFormError('Git repository URL is required')
      return
    }
    if (!sourceBranch.trim() || !targetBranch.trim()) {
      setNewFormError('Source and target branches are required')
      return
    }
    if (!issueType.trim()) {
      setNewFormError('Issue type is required')
      return
    }
    if (!newScheduledAt) {
      setNewFormError('Schedule time is required')
      return
    }
    try {
      await onCreate({
        title: title.trim(),
        description: description.trim(),
        repository_url: repositoryUrl.trim(),
        source_branch: sourceBranch.trim(),
        target_branch: targetBranch.trim(),
        mode,
        issue_type: issueType.trim(),
        scheduled_at: toIsoLocal(newScheduledAt),
      })
      setTitle('')
      setDescription('')
      setNewScheduledAt(defaultLocalDatetimeValue())
    } catch (err) {
      setNewFormError(err instanceof Error ? err.message : 'Create failed')
    }
  }

  const cancelOne = async (id: string) => {
    if (
      !window.confirm(
        `Cancel scheduled job ${id}?\n\nDoes not delete the Jira issue.`,
      )
    ) {
      return
    }
    setCancellingId(id)
    try {
      await onCancel(id)
    } finally {
      setCancellingId(null)
    }
  }

  const busy = creating || existingSubmitting

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">Scheduled jobs</h2>
          <p className="text-xs text-text-muted">
            Fire agent work at a chosen time. Create a new Jira issue, or schedule
            an existing one that already has a valid{' '}
            <span className="font-mono text-text-secondary">{'{params}'}</span>{' '}
            template.
          </p>
        </div>
        <button
          type="button"
          className="ops-btn ops-btn-secondary px-2.5 py-1 text-xs"
          onClick={() => onRefresh()}
          disabled={loading}
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {/* Mode switcher */}
      <div className="flex flex-wrap gap-1 rounded border border-border bg-surface p-1 w-fit">
        <button
          type="button"
          onClick={() => switchMode('existing')}
          className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            createMode === 'existing'
              ? 'bg-accent text-white'
              : 'text-text-secondary hover:bg-surface-hover hover:text-text'
          }`}
        >
          Existing issue
        </button>
        <button
          type="button"
          onClick={() => switchMode('new')}
          className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            createMode === 'new'
              ? 'bg-accent text-white'
              : 'text-text-secondary hover:bg-surface-hover hover:text-text'
          }`}
        >
          Create new issue
        </button>
      </div>

      {/* ===== Existing issue ===== */}
      {createMode === 'existing' && (
        <div className="ops-card space-y-4 p-5">
          <div>
            <div className="text-sm font-semibold text-text">
              Schedule existing issue
            </div>
            <p className="mt-1 text-xs text-text-muted">
              Enter the Jira key and press <strong>Get</strong>. If the issue
              exists and the git template is valid, choose a run time and schedule.
            </p>
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <label className="block min-w-[12rem] flex-1 text-sm">
              <span className="text-text-secondary">Issue key</span>
              <input
                className="ops-input mt-1 font-mono uppercase"
                value={lookupKey}
                onChange={(e) => {
                  setLookupKey(e.target.value.toUpperCase())
                  // Clear validated preview when key changes
                  if (preview) setPreview(null)
                  setPreviewError(null)
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    void onGetIssue()
                  }
                }}
                placeholder="e.g. KAN-12"
                disabled={previewLoading}
              />
            </label>
            <button
              type="button"
              className="ops-btn ops-btn-primary px-4 py-2 text-sm"
              onClick={() => void onGetIssue()}
              disabled={previewLoading || !lookupKey.trim()}
            >
              {previewLoading ? 'Loading…' : 'Get'}
            </button>
          </div>

          {previewError && (
            <div role="alert" className="ops-alert ops-alert-danger text-xs">
              {previewError}
            </div>
          )}

          {preview?.ok && preview.template_valid && (
            <form
              onSubmit={(e) => void onScheduleExisting(e)}
              className="space-y-4 border-t border-border pt-4"
            >
              <div className="rounded border border-border bg-bg p-4 space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-accent-text">
                    {preview.issue_key}
                  </span>
                  {preview.jira_status && (
                    <span className="text-[10px] uppercase tracking-wide text-text-muted">
                      {preview.jira_status}
                    </span>
                  )}
                  {preview.issue_type && (
                    <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-secondary">
                      {preview.issue_type}
                    </span>
                  )}
                  <span className="rounded bg-success-muted px-1.5 py-0.5 text-[10px] font-medium text-success-text">
                    Template valid
                  </span>
                </div>
                <div className="text-sm text-text">{preview.title || '—'}</div>
                <dl className="grid gap-1 text-xs sm:grid-cols-2">
                  <div>
                    <dt className="text-text-muted">Mode</dt>
                    <dd className="font-mono text-text-secondary">
                      {preview.mode || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-text-muted">Repository</dt>
                    <dd
                      className="truncate font-mono text-text-secondary"
                      title={preview.repository_url}
                    >
                      {preview.repository_url || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-text-muted">Source branch</dt>
                    <dd className="font-mono text-text-secondary">
                      {preview.source_branch || '—'}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-text-muted">Target branch</dt>
                    <dd className="font-mono text-text-secondary">
                      {preview.target_branch || '—'}
                    </dd>
                  </div>
                </dl>
              </div>

              <label className="block max-w-xs text-sm">
                <span className="text-text-secondary">Run at (local time)</span>
                <input
                  type="datetime-local"
                  className="ops-input mt-1"
                  value={existingScheduledAt}
                  onChange={(e) => setExistingScheduledAt(e.target.value)}
                  required
                />
              </label>

              {existingFormError && (
                <div role="alert" className="ops-alert ops-alert-danger text-xs">
                  {existingFormError}
                </div>
              )}

              <div className="flex flex-wrap items-center justify-end gap-2">
                <button
                  type="button"
                  className="ops-btn ops-btn-secondary text-sm"
                  onClick={() => {
                    setPreview(null)
                    setExistingFormError(null)
                  }}
                  disabled={existingSubmitting}
                >
                  Clear
                </button>
                <button
                  type="submit"
                  className="ops-btn ops-btn-primary text-sm"
                  disabled={existingSubmitting}
                >
                  {existingSubmitting
                    ? 'Scheduling…'
                    : `Schedule ${preview.issue_key}`}
                </button>
              </div>
            </form>
          )}
        </div>
      )}

      {/* ===== Create new issue ===== */}
      {createMode === 'new' && (
        <form
          onSubmit={(e) => void onCreateNew(e)}
          className="ops-card space-y-4 p-5"
        >
          <div>
            <div className="text-sm font-semibold text-text">
              Create new scheduled issue
            </div>
            <p className="mt-1 text-xs text-text-muted">
              Creates a Jira issue with label{' '}
              <span className="font-mono text-text-secondary">
                SCHEDULED_AI_JOB
              </span>
              , moves it to In Progress, and runs at the chosen time.
            </p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-sm sm:col-span-2">
              <span className="text-text-secondary">Title</span>
              <input
                className="ops-input mt-1"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="Short Jira summary"
                required
              />
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="text-text-secondary">Description</span>
              <textarea
                className="ops-input mt-1 min-h-[4.5rem] font-mono text-xs"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Task details (Repository / Mode params are added automatically)"
              />
            </label>
            <label className="block text-sm sm:col-span-2">
              <span className="text-text-secondary">Git repository</span>
              <input
                className="ops-input mt-1 font-mono text-xs"
                value={repositoryUrl}
                onChange={(e) => setRepositoryUrl(e.target.value)}
                placeholder="https://gitlab.com/org/repo.git"
                required
              />
            </label>
            <label className="block text-sm">
              <span className="text-text-secondary">Source branch</span>
              <input
                className="ops-input mt-1 font-mono text-xs"
                value={sourceBranch}
                onChange={(e) => setSourceBranch(e.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              <span className="text-text-secondary">Target branch</span>
              <input
                className="ops-input mt-1 font-mono text-xs"
                value={targetBranch}
                onChange={(e) => setTargetBranch(e.target.value)}
                required
              />
            </label>
            <label className="block text-sm">
              <span className="text-text-secondary">Mode</span>
              <select
                className="ops-input mt-1"
                value={mode}
                onChange={(e) =>
                  setMode(e.target.value === 'plan' ? 'plan' : 'build')
                }
              >
                <option value="build">build — implement / push + MR</option>
                <option value="plan">plan — generate plan only</option>
              </select>
            </label>
            <label className="block text-sm">
              <span className="flex items-center justify-between gap-2 text-text-secondary">
                <span>Issue type</span>
                <button
                  type="button"
                  className="ops-btn-ghost text-[10px]"
                  onClick={() => void loadIssueTypes()}
                  disabled={typesLoading}
                >
                  {typesLoading ? 'Loading…' : 'Reload types'}
                </button>
              </span>
              {selectableTypes.length > 0 ? (
                <select
                  className="ops-input mt-1"
                  value={issueType}
                  onChange={(e) => setIssueType(e.target.value)}
                >
                  {selectableTypes.map((t) => (
                    <option key={t.id || t.name} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className="ops-input mt-1 font-mono text-xs"
                  value={issueType}
                  onChange={(e) => setIssueType(e.target.value)}
                  placeholder="Task, Story, ExtBug, …"
                  required
                />
              )}
              {typesError && (
                <span className="mt-1 block text-[10px] text-warning-text">
                  {typesError}
                </span>
              )}
            </label>
            <label className="block text-sm">
              <span className="text-text-secondary">Run at (local time)</span>
              <input
                type="datetime-local"
                className="ops-input mt-1"
                value={newScheduledAt}
                onChange={(e) => setNewScheduledAt(e.target.value)}
                required
              />
            </label>
          </div>
          {(newFormError || error) && (
            <div role="alert" className="ops-alert ops-alert-danger text-xs">
              {newFormError || error}
            </div>
          )}
          <div className="flex justify-end">
            <button
              type="submit"
              className="ops-btn ops-btn-primary text-sm"
              disabled={busy}
            >
              {creating ? 'Creating…' : 'Create scheduled job'}
            </button>
          </div>
        </form>
      )}

      {error && createMode === 'existing' && (
        <div role="alert" className="ops-alert ops-alert-danger text-xs">
          {error}
        </div>
      )}

      {/* List */}
      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>Schedule</th>
              <th>Issue</th>
              <th>Title</th>
              <th>Source</th>
              <th>Mode</th>
              <th>Run at</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {schedules.length === 0 && (
              <tr>
                <td
                  colSpan={8}
                  className="px-4 py-8 text-center text-text-muted"
                >
                  {loading ? 'Loading…' : 'No scheduled jobs yet.'}
                </td>
              </tr>
            )}
            {schedules.map((s) => {
              const canCancel =
                s.status === 'scheduled' || s.status === 'error'
              const src = (s.source || 'new').toLowerCase()
              return (
                <tr key={s.schedule_id}>
                  <td className="font-mono text-[11px] text-text-secondary">
                    {s.schedule_id.length > 18
                      ? `${s.schedule_id.slice(0, 16)}…`
                      : s.schedule_id}
                  </td>
                  <td>
                    {s.issue_key ? (
                      <button
                        type="button"
                        className="font-mono text-sm text-accent-text hover:underline"
                        onClick={() => onOpenIssue?.(s.issue_key)}
                      >
                        {s.issue_key}
                      </button>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>
                    <div className="max-w-xs truncate text-sm" title={s.title}>
                      {s.title || '—'}
                    </div>
                    <div
                      className="mt-0.5 max-w-xs truncate font-mono text-[10px] text-text-muted"
                      title={s.repository_url}
                    >
                      {s.repository_url || ''}
                    </div>
                  </td>
                  <td className="text-xs text-text-secondary">
                    {src === 'existing' ? 'existing' : 'new'}
                    {s.issue_type ? (
                      <div className="font-mono text-[10px] text-text-muted">
                        {s.issue_type}
                      </div>
                    ) : null}
                  </td>
                  <td className="font-mono text-xs text-text-secondary">
                    {s.mode}
                  </td>
                  <td className="font-mono text-[11px] text-text-muted">
                    {s.scheduled_at || '—'}
                  </td>
                  <td>
                    <StatusBadge status={s.status} size="sm" />
                    {s.error_message && (
                      <div className="mt-1 max-w-xs truncate text-xs text-danger-text">
                        {s.error_message}
                      </div>
                    )}
                  </td>
                  <td className="text-right">
                    {canCancel && (
                      <button
                        type="button"
                        className="ops-btn ops-btn-secondary px-2 py-1 text-xs"
                        disabled={cancellingId === s.schedule_id}
                        onClick={() => void cancelOne(s.schedule_id)}
                      >
                        {cancellingId === s.schedule_id ? '…' : 'Cancel'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
