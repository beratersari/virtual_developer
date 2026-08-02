import { useEffect, useMemo, useState } from 'react'
import { fetchIssueTypes } from '../api'
import type {
  JiraIssueType,
  ScheduleCreateBody,
  ScheduleItem,
} from '../types'
import { StatusBadge } from './StatusBadge'

function defaultLocalDatetimeValue(): string {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  d.setSeconds(0, 0)
  // +5 minutes default
  d.setMinutes(d.getMinutes() + 5)
  return d.toISOString().slice(0, 16)
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
  const [scheduledAt, setScheduledAt] = useState(defaultLocalDatetimeValue)
  const [formError, setFormError] = useState<string | null>(null)
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
    void loadIssueTypes()
  }, [])

  const selectableTypes = useMemo(
    () => issueTypes.filter((t) => !t.subtask),
    [issueTypes],
  )

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setFormError(null)
    if (!title.trim()) {
      setFormError('Title is required')
      return
    }
    if (!repositoryUrl.trim()) {
      setFormError('Git repository URL is required')
      return
    }
    if (!sourceBranch.trim() || !targetBranch.trim()) {
      setFormError('Source and target branches are required')
      return
    }
    if (!issueType.trim()) {
      setFormError('Issue type is required')
      return
    }
    if (!scheduledAt) {
      setFormError('Schedule time is required')
      return
    }
    // datetime-local is local wall time without offset; send as-is for server
    const iso = scheduledAt.length === 16 ? `${scheduledAt}:00` : scheduledAt
    try {
      await onCreate({
        title: title.trim(),
        description: description.trim(),
        repository_url: repositoryUrl.trim(),
        source_branch: sourceBranch.trim(),
        target_branch: targetBranch.trim(),
        mode,
        issue_type: issueType.trim(),
        scheduled_at: iso,
      })
      setTitle('')
      setDescription('')
      setScheduledAt(defaultLocalDatetimeValue())
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Create failed')
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

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">Scheduled jobs</h2>
          <p className="text-xs text-text-muted">
            Creates a Jira issue with label{' '}
            <span className="font-mono text-text-secondary">SCHEDULED_AI_JOB</span>
            , moves it to In Progress, and starts agent work at the scheduled time.
            Also available via{' '}
            <span className="font-mono text-text-secondary">
              python cli.py schedule create
            </span>
            .
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

      <form
        onSubmit={(e) => void submit(e)}
        className="ops-card space-y-4 p-5"
      >
        <div className="text-sm font-semibold text-text">Create schedule</div>
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
            <span className="mt-1 block text-[10px] text-text-muted">
              Loaded from Jira project create-meta (Cloud + on-prem). Custom types
              like ExtBug appear when the project allows them.
              {typesError ? ` · ${typesError}` : ''}
            </span>
          </label>
          <label className="block text-sm">
            <span className="text-text-secondary">Run at (local time)</span>
            <input
              type="datetime-local"
              className="ops-input mt-1"
              value={scheduledAt}
              onChange={(e) => setScheduledAt(e.target.value)}
              required
            />
          </label>
        </div>
        {(formError || error) && (
          <div role="alert" className="ops-alert ops-alert-danger text-xs">
            {formError || error}
          </div>
        )}
        <div className="flex justify-end">
          <button
            type="submit"
            className="ops-btn ops-btn-primary text-sm"
            disabled={creating}
          >
            {creating ? 'Creating…' : 'Create scheduled job'}
          </button>
        </div>
      </form>

      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>Schedule</th>
              <th>Issue</th>
              <th>Title</th>
              <th>Type</th>
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
                  <td className="font-mono text-xs text-text-secondary">
                    {s.issue_type || '—'}
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
                        {cancellingId === s.schedule_id
                          ? '…'
                          : 'Cancel'}
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
