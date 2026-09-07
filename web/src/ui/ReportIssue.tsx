import { useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { ApiError, downloadIssueReport, fetchJobs } from '../api/client'
import type { JobItem } from '../api/types'
import { Spinner } from './Spinner'

type Target = { kind: 'general' } | { kind: 'job'; job: JobItem }

function jobLabel(job: JobItem): string {
  const title = (job.summary || '').trim() || '(no title)'
  const key = (job.issue_key || '').trim()
  return key ? `${job.job_id} — ${key}: ${title}` : `${job.job_id} — ${title}`
}

function jobIdFromPath(pathname: string): string | null {
  const m = pathname.match(/^\/jobs\/([^/]+)/)
  return m ? decodeURIComponent(m[1]) : null
}

export function ReportIssue() {
  const location = useLocation()
  const rootRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [jobs, setJobs] = useState<JobItem[]>([])
  const [loadingJobs, setLoadingJobs] = useState(false)
  const [query, setQuery] = useState('')
  const [target, setTarget] = useState<Target | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, busy])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setLoadingJobs(true)
    setError(null)
    setDone(null)
    fetchJobs({ page: 1, pageSize: 100 })
      .then((payload) => {
        if (cancelled) return
        const list = payload.jobs || []
        setJobs(list)
        const fromPath = jobIdFromPath(location.pathname)
        const match = fromPath ? list.find((j) => j.job_id === fromPath) : null
        setTarget(match ? { kind: 'job', job: match } : { kind: 'general' })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setJobs([])
        setTarget({ kind: 'general' })
        setError(err instanceof Error ? err.message : 'Could not load jobs')
      })
      .finally(() => {
        if (!cancelled) setLoadingJobs(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, location.pathname])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return jobs
    return jobs.filter((j) => jobLabel(j).toLowerCase().includes(q))
  }, [jobs, query])

  const canSubmit = Boolean(target && note.trim() && !busy)

  async function submit() {
    if (!target || !note.trim()) return
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const filename = await downloadIssueReport({
        kind: target.kind,
        note: note.trim(),
        job_id: target.kind === 'job' ? target.job.job_id : undefined,
      })
      setDone(filename)
      setNote('')
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Download failed'
      setError(msg)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="relative" ref={rootRef}>
      <button
        type="button"
        className="vd-btn vd-btn-secondary w-full justify-start px-3 py-1.5 text-xs"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
      >
        <IconFlag />
        Report issue
      </button>

      {open && (
        <div
          className="vd-report-panel"
          role="dialog"
          aria-label="Report issue"
        >
          <div className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
            What is this about?
          </div>
          <input
            className="vd-input mb-2 py-1.5 text-xs"
            type="search"
            placeholder="Filter jobs…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            disabled={loadingJobs}
          />
          <div className="vd-report-list" role="listbox" aria-label="Issue target">
            <button
              type="button"
              role="option"
              aria-selected={target?.kind === 'general'}
              className={
                target?.kind === 'general'
                  ? 'vd-report-option is-selected'
                  : 'vd-report-option'
              }
              onClick={() => setTarget({ kind: 'general' })}
            >
              <span className="font-medium text-text">General issue</span>
              <span className="block text-[11px] text-text-muted">
                Settings, poll, queue, logs, and your note
              </span>
            </button>
            {loadingJobs && (
              <div className="flex items-center gap-2 px-2 py-2 text-xs text-text-muted">
                <Spinner /> Loading jobs…
              </div>
            )}
            {!loadingJobs &&
              filtered.map((job) => {
                const selected =
                  target?.kind === 'job' && target.job.job_id === job.job_id
                return (
                  <button
                    key={job.job_id}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    className={selected ? 'vd-report-option is-selected' : 'vd-report-option'}
                    onClick={() => setTarget({ kind: 'job', job })}
                    title={jobLabel(job)}
                  >
                    <span className="font-mono text-[11px] text-text-secondary">
                      {job.job_id}
                    </span>
                    <span className="block truncate text-xs text-text">
                      {(job.issue_key || '—') + ': ' + (job.summary || '(no title)')}
                    </span>
                  </button>
                )
              })}
            {!loadingJobs && jobs.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-text-muted">No jobs yet.</div>
            )}
            {!loadingJobs && jobs.length > 0 && filtered.length === 0 && (
              <div className="px-2 py-2 text-[11px] text-text-muted">No matching jobs.</div>
            )}
          </div>

          <label className="mt-3 block text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
            Note
            <textarea
              className="vd-input mt-1 min-h-[5.5rem] resize-y py-1.5 text-xs leading-relaxed"
              value={note}
              maxLength={8000}
              placeholder="What went wrong? What did you expect?"
              onChange={(e) => setNote(e.target.value)}
            />
          </label>

          {error && <div className="mt-2 text-xs text-danger-text">{error}</div>}
          {done && (
            <div className="mt-2 text-xs text-success-text">Downloaded {done}</div>
          )}

          <div className="mt-3 flex justify-end gap-2">
            <button
              type="button"
              className="vd-btn vd-btn-secondary px-3 py-1.5 text-xs"
              disabled={busy}
              onClick={() => setOpen(false)}
            >
              Close
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-primary px-3 py-1.5 text-xs"
              disabled={!canSubmit}
              onClick={() => void submit()}
            >
              {busy ? (
                <>
                  <Spinner /> Building…
                </>
              ) : (
                'Download zip'
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function IconFlag() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M3.5 13.5V3.5h7.2l-.8 2.4 1.2.6H12.5v4.5H8.8l.7-2.1-1.3-.6H3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  )
}
