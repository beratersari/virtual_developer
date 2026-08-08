import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  cancelSchedule,
  createSchedule,
  fetchIssueTypes,
  fetchSchedules,
  previewScheduleIssue,
  scheduleExistingIssue,
} from '../../api/client'
import type { JiraIssueType, ScheduleItem, SchedulePreview } from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'
import { StatusBadge } from '../../ui/StatusBadge'

function defaultWhen(): string {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset() + 5)
  d.setSeconds(0, 0)
  return d.toISOString().slice(0, 16)
}

function toIso(local: string): string {
  const raw = local.length === 16 ? `${local}:00` : local
  const d = new Date(raw)
  return Number.isNaN(d.getTime()) ? raw : d.toISOString()
}

export function SchedulesPage() {
  const live = useLive()
  const [rows, setRows] = useState<ScheduleItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mode, setMode] = useState<'existing' | 'new'>('existing')

  const reload = async () => {
    try {
      const p = await fetchSchedules()
      setRows(p.schedules || [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [])
  useEffect(() => {
    void reload()
  }, [live.generation])

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Later"
        title="Scheduled"
        description={
          <>
            Queue a run for a chosen time. Existing issues need a valid{' '}
            <span className="font-mono">{'{params}'}</span> template.
          </>
        }
      />
      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'existing' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('existing')}
        >
          Existing issue
        </button>
        <button
          type="button"
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium ${
            mode === 'new' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          }`}
          onClick={() => setMode('new')}
        >
          New issue
        </button>
      </div>
      {mode === 'existing' ? <Existing onDone={() => void reload()} /> : <CreateNew onDone={() => void reload()} />}
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {rows.map((s) => (
          <li key={s.schedule_id} className="py-3 text-sm">
            {s.issue_key ? (
              <Link className="font-mono text-accent-text hover:underline" to={`/tasks/${encodeURIComponent(s.issue_key)}`}>
                {s.issue_key}
              </Link>
            ) : (
              '—'
            )}{' '}
            {s.title} · {s.mode} · {s.scheduled_at} · <StatusBadge status={s.status} size="sm" />
            {(s.status === 'scheduled' || s.status === 'error' || s.status === 'dispatching') && (
              <>
                {' '}
                <button
                  type="button"
                  className="vd-btn-ghost text-danger-text"
                  onClick={() => setCancelId(s.schedule_id)}
                >
                  cancel
                </button>
              </>
            )}
          </li>
        ))}
        {rows.length === 0 && <li className="py-6 text-text-muted">Nothing scheduled.</li>}
      </ul>
      <ConfirmDialog
        open={Boolean(cancelId)}
        title="Cancel this schedule?"
        body="Does not delete the Jira issue."
        confirmLabel="Cancel it"
        danger
        busy={busy}
        onConfirm={async () => {
          if (!cancelId) return
          setBusy(true)
          try {
            await cancelSchedule(cancelId)
            setCancelId(null)
            await reload()
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setCancelId(null)}
      />
    </section>
  )
}

function Existing({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState('')
  const [preview, setPreview] = useState<SchedulePreview | null>(null)
  const [when, setWhen] = useState(defaultWhen)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [looking, setLooking] = useState(false)

  const get = async () => {
    setErr(null)
    setLooking(true)
    try {
      const p = await previewScheduleIssue(key.trim().toUpperCase())
      setPreview(p)
      setKey(p.issue_key || key)
    } catch (e) {
      setPreview(null)
      setErr(e instanceof Error ? e.message : 'Preview failed')
    } finally {
      setLooking(false)
    }
  }

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!preview?.ok) return
    setBusy(true)
    setErr(null)
    try {
      await scheduleExistingIssue({ issue_key: preview.issue_key, scheduled_at: toIso(when) })
      setPreview(null)
      setKey('')
      onDone()
    } catch (err2) {
      setErr(err2 instanceof Error ? err2.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <label className="field">
        <span>Issue key</span>
        <input value={key} onChange={(e) => setKey(e.target.value.toUpperCase())} />
      </label>
      <p className="actions">
        <button type="button" disabled={looking || !key.trim()} onClick={() => void get()}>
          {looking ? 'Looking up…' : 'Look up'}
        </button>
      </p>
      {preview?.ok && preview.template_valid && (
        <>
          <p className="quiet">
            {preview.issue_key} — {preview.title} · {preview.mode} · {preview.repository_url}
          </p>
          <label className="field">
            <span>Run at</span>
            <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
          </label>
          <button type="submit" className="go" disabled={busy}>
            {busy ? (
              <>
                <Spinner /> Scheduling…
              </>
            ) : (
              'Schedule'
            )}
          </button>
        </>
      )}
      {err && <p className="err">{err}</p>}
    </form>
  )
}

function CreateNew({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [repo, setRepo] = useState('')
  const [srcMode, setSrcMode] = useState<'issue_key' | 'custom'>('issue_key')
  const [source, setSource] = useState('develop')
  const [target, setTarget] = useState('develop')
  const [mode, setMode] = useState<'plan' | 'build'>('build')
  const [issueType, setIssueType] = useState('Task')
  const [types, setTypes] = useState<JiraIssueType[]>([])
  const [when, setWhen] = useState(defaultWhen)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void fetchIssueTypes()
      .then((p) => setTypes(p.issue_types || []))
      .catch(() => undefined)
  }, [])

  const selectable = useMemo(() => types.filter((t) => !t.subtask), [types])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await createSchedule({
        title: title.trim(),
        description: description.trim(),
        repository_url: repo.trim(),
        source_branch: srcMode === 'custom' ? source.trim() : undefined,
        source_branch_mode: srcMode,
        target_branch: target.trim(),
        mode,
        issue_type: issueType.trim(),
        scheduled_at: toIso(when),
      })
      setTitle('')
      setDescription('')
      setRepo('')
      onDone()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'Create failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)}>
      <label className="field">
        <span>Title</span>
        <input value={title} onChange={(e) => setTitle(e.target.value)} required />
      </label>
      <label className="field">
        <span>Description</span>
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="field">
        <span>Repository</span>
        <input value={repo} onChange={(e) => setRepo(e.target.value)} required />
      </label>
      <label className="field">
        <span>Source</span>
        <select value={srcMode} onChange={(e) => setSrcMode(e.target.value === 'custom' ? 'custom' : 'issue_key')}>
          <option value="issue_key">feature/&lt;new issue key&gt;</option>
          <option value="custom">named branch</option>
        </select>
      </label>
      {srcMode === 'custom' && (
        <label className="field">
          <span>Branch</span>
          <input value={source} onChange={(e) => setSource(e.target.value)} required />
        </label>
      )}
      <label className="field">
        <span>Target</span>
        <input value={target} onChange={(e) => setTarget(e.target.value)} required />
      </label>
      <label className="field">
        <span>Mode</span>
        <select value={mode} onChange={(e) => setMode(e.target.value === 'plan' ? 'plan' : 'build')}>
          <option value="build">build</option>
          <option value="plan">plan</option>
        </select>
      </label>
      <label className="field">
        <span>Issue type</span>
        {selectable.length ? (
          <select value={issueType} onChange={(e) => setIssueType(e.target.value)}>
            {selectable.map((t) => (
              <option key={t.id || t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
        ) : (
          <input value={issueType} onChange={(e) => setIssueType(e.target.value)} />
        )}
      </label>
      <label className="field">
        <span>Run at</span>
        <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} />
      </label>
      {err && <p className="err">{err}</p>}
      <button type="submit" className="go" disabled={busy}>
        {busy ? (
          <>
            <Spinner /> Creating…
          </>
        ) : (
          'Create schedule'
        )}
      </button>
    </form>
  )
}
