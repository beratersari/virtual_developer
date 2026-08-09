import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { deleteJobs, fetchJobs } from '../../api/client'
import type { JobsPayload } from '../../api/types'
import { useLive } from '../../app/live'
import {
  jobIsDeletable,
  jobMatchesFilter,
  type JobStatusFilter,
} from '../../util/status'
import { Alert } from '../../ui/Alert'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { LiveDot } from '../../ui/LiveDot'
import { PageHeader } from '../../ui/PageHeader'
import { peekJobsPayload, rememberJobsPayload } from '../../app/entityCache'
import { JobsTable } from './JobsTable'

const FILTERS: { id: JobStatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'active', label: 'Active' },
  { id: 'error', label: 'Error' },
  { id: 'completed', label: 'Completed' },
  { id: 'cancelled', label: 'Cancelled' },
]

const PAGE_SIZE = 25

export function JobsPage() {
  const navigate = useNavigate()
  const live = useLive()
  const [issueFilter, setIssueFilter] = useState('')
  const [debouncedFilter, setDebouncedFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState<JobStatusFilter>('all')
  const [page, setPage] = useState(1)
  const [payload, setPayload] = useState<JobsPayload | null>(() => peekJobsPayload())
  const [error, setError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const reqId = useRef(0)
  const lastGenReload = useRef(0)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebouncedFilter(issueFilter.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(t)
  }, [issueFilter])

  const load = useCallback(
    async (opts?: { filter?: string; page?: number }) => {
      const req = ++reqId.current
      try {
        const data = await fetchJobs({
          issueKey: (opts?.filter ?? debouncedFilter) || undefined,
          page: opts?.page ?? page,
          pageSize: PAGE_SIZE,
        })
        if (req !== reqId.current) return
        rememberJobsPayload(data)
        setPayload(data)
        setError(null)
      } catch (e) {
        if (req !== reqId.current) return
        setError(e instanceof Error ? e.message : 'Failed to load jobs')
      }
    },
    [debouncedFilter, page],
  )

  useEffect(() => {
    void load({ filter: debouncedFilter, page })
  }, [debouncedFilter, page, load])

  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void load()
  }, [live.generation, load])

  const filteredJobs = useMemo(
    () =>
      (payload?.jobs ?? []).filter((j) =>
        jobMatchesFilter(j.status, Boolean(j.live), statusFilter),
      ),
    [payload, statusFilter],
  )

  const visibleIdKey = filteredJobs.map((j) => j.job_id).join('|')
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev
      const visible = new Set(filteredJobs.map((j) => j.job_id))
      const next = new Set([...prev].filter((id) => visible.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [visibleIdKey, filteredJobs])

  const deletableOnPage = useMemo(
    () => filteredJobs.filter((j) => jobIsDeletable(j.status, Boolean(j.live))),
    [filteredJobs],
  )

  const toggleSelect = (jobId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  const onConfirmDelete = async () => {
    const ids = [...selectedIds]
    if (ids.length === 0) return
    setBulkDeleting(true)
    setError(null)
    try {
      const result = await deleteJobs(ids, { deleteArtifacts: true })
      if (result.failed_count > 0) {
        const sample = (result.failed || [])
          .slice(0, 3)
          .map((f) => `${f.job_id}: ${f.error}`)
          .join('; ')
        const more = result.failed_count > 3 ? ` (+${result.failed_count - 3} more)` : ''
        if (result.deleted_count === 0) {
          throw new Error(result.message || `Could not delete jobs. ${sample}${more}`)
        }
        setError(`Deleted ${result.deleted_count}; ${result.failed_count} failed. ${sample}${more}`)
      }
      setSelectedIds(new Set())
      setConfirmOpen(false)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    } finally {
      setBulkDeleting(false)
    }
  }

  const total = payload?.total ?? 0
  const currentPage = payload?.page ?? page
  const size = payload?.page_size ?? PAGE_SIZE
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (currentPage - 1) * size + 1
  const to = Math.min(currentPage * size, total)
  const selectedCount = selectedIds.size
  const liveJobs = (payload?.jobs ?? []).filter((j) => j.live)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Workbench"
        title="Jobs"
        description={
          live.connected
            ? 'Each card is one agent run. Open a card for logs and prompts.'
            : 'Disconnected — list may be stale.'
        }
        actions={
          <label className="block text-xs text-text-muted">
            Find issue
            <input
              className="vd-input mt-1 w-52 font-mono"
              placeholder="KAN-12"
              value={issueFilter}
              onChange={(e) => setIssueFilter(e.target.value)}
            />
          </label>
        }
      />

      {liveJobs.length > 0 && statusFilter !== 'live' && (
        <div className="vd-panel flex flex-wrap items-center gap-3 px-4 py-3">
          <LiveDot label={`${liveJobs.length} running`} />
          {liveJobs.slice(0, 4).map((j) => (
            <button
              key={j.job_id}
              type="button"
              className="font-mono text-sm text-accent-text hover:underline"
              onClick={() => navigate(`/jobs/${encodeURIComponent(j.job_id)}`)}
            >
              {j.issue_key}
            </button>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setStatusFilter(f.id)}
              className={`rounded-full px-3 py-1 text-xs font-semibold transition-transform duration-150 active:scale-95 ${
                statusFilter === f.id
                  ? 'bg-accent text-[#1a0d08]'
                  : 'text-text-muted hover:text-text'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <span>
            {from}–{to} of {total}
            {debouncedFilter ? ` · ${debouncedFilter.toUpperCase()}` : ''}
          </span>
          <button
            type="button"
            disabled={currentPage <= 1}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
          >
            Prev
          </button>
          <button
            type="button"
            disabled={currentPage >= totalPages}
            onClick={() => setPage((p) => p + 1)}
            className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
          >
            Next
          </button>
        </div>
      </div>

      {statusFilter !== 'all' && (
        <p className="text-xs text-text-muted">
          Status filter is this page only ({filteredJobs.length} of {payload?.jobs.length ?? 0}).
          Issue search hits the server.
        </p>
      )}

      {error && (
        <Alert
          action={
            <button
              type="button"
              className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
              onClick={() => {
                setError(null)
                void load()
              }}
            >
              Retry
            </button>
          }
        >
          {error}
        </Alert>
      )}

      <JobsTable
        jobs={filteredJobs}
        selectable
        selectedIds={selectedIds}
        onToggleSelect={toggleSelect}
        onOpenJob={(_key, jobId) => navigate(`/jobs/${encodeURIComponent(jobId)}`)}
      />

      {selectedCount > 0 && (
        <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border-strong bg-bg-elevated px-4 py-3 shadow-lg">
          <span className="text-sm text-text-secondary">
            {selectedCount} selected · {deletableOnPage.length} deletable on this page
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              onClick={() => setSelectedIds(new Set())}
            >
              Clear
            </button>
            <button
              type="button"
              className="vd-btn vd-btn-danger"
              disabled={bulkDeleting}
              onClick={() => setConfirmOpen(true)}
            >
              {bulkDeleting ? 'Deleting…' : 'Delete selected'}
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmOpen}
        title={`Delete ${selectedCount} job(s)?`}
        body={
          'Removes job history records and linked session/prompt files under .jira-agent.\n' +
          'Does not change Jira issues. Live / in-flight jobs are skipped.'
        }
        confirmLabel="Delete"
        danger
        busy={bulkDeleting}
        onConfirm={() => void onConfirmDelete()}
        onCancel={() => setConfirmOpen(false)}
      />
    </section>
  )
}
