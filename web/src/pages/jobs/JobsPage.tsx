import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { cancelQueueItem, deleteJobs, fetchJobs, fetchQueue } from '../../api/client'
import type { JobsPayload, QueueItem } from '../../api/types'
import { useLive } from '../../app/live'
import { sortJobsByCreatedAt } from '../../util/jobs'
import {
  jobIsDeletable,
  jobMatchesFilter,
  type JobStatusFilter,
} from '../../util/status'
import { Alert } from '../../ui/Alert'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { LiveDot } from '../../ui/LiveDot'
import { PageHeader } from '../../ui/PageHeader'
import { StatusBadge } from '../../ui/StatusBadge'
import { peekJobsPayload, rememberJobsPayload } from '../../app/entityCache'
import { JobsTable } from './JobsTable'

const FILTERS: { id: JobStatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'active', label: 'Active' },
  { id: 'queue', label: 'Queue' },
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
  const [queueItems, setQueueItems] = useState<QueueItem[]>([])
  const [queueQueued, setQueueQueued] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [cancelQueueId, setCancelQueueId] = useState<string | null>(null)
  const reqId = useRef(0)
  const lastGenReload = useRef(0)

  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebouncedFilter(issueFilter.trim())
      setPage(1)
    }, 250)
    return () => window.clearTimeout(t)
  }, [issueFilter])

  const loadQueue = useCallback(async () => {
    try {
      const q = await fetchQueue({ status: 'queued', limit: 200 })
      const rows = (q.items || []).filter((r) => r.status === 'queued')
      rows.sort((a, b) =>
        String(b.created_at || '').localeCompare(String(a.created_at || '')),
      )
      setQueueItems(rows)
      setQueueQueued(
        typeof q.queued_count === 'number' ? q.queued_count : rows.length,
      )
    } catch {
      setQueueItems([])
      setQueueQueued(0)
    }
  }, [])

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
      void loadQueue()
    },
    [debouncedFilter, page, loadQueue],
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

  const showQueue = statusFilter === 'queue'

  const filteredJobs = useMemo(
    () =>
      showQueue
        ? []
        : sortJobsByCreatedAt(
            (payload?.jobs ?? []).filter((j) =>
              jobMatchesFilter(j.status, Boolean(j.live), statusFilter),
            ),
          ),
    [payload, statusFilter, showQueue],
  )

  const visibleQueue = useMemo(() => {
    if (!showQueue) return []
    const needle = debouncedFilter.trim().toUpperCase()
    if (!needle) return queueItems
    return queueItems.filter((q) =>
      (q.issue_key || '').toUpperCase().includes(needle),
    )
  }, [queueItems, debouncedFilter, showQueue])

  const visibleIdKey = filteredJobs.map((j) => j.job_id).join('|')
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev
      const visible = new Set(filteredJobs.map((j) => j.job_id))
      const next = new Set([...prev].filter((id) => visible.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [visibleIdKey, filteredJobs])

  // Leaving queue tab clears bulk selection (jobs only)
  useEffect(() => {
    if (showQueue) setSelectedIds(new Set())
  }, [showQueue])

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
  const liveJobs = sortJobsByCreatedAt((payload?.jobs ?? []).filter((j) => j.live))
  const badgeQueued = live.queueQueued ?? queueQueued

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Workbench"
        title="Jobs"
        description={
          live.connected
            ? 'Each card is one agent run. Use the Queue tab for messages waiting for a free slot.'
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

      {liveJobs.length > 0 && statusFilter !== 'live' && statusFilter !== 'queue' && (
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
              {f.id === 'queue' && badgeQueued > 0 ? (
                <span className="ml-1.5 font-mono tabular-nums opacity-90">
                  {badgeQueued}
                </span>
              ) : null}
            </button>
          ))}
        </div>
        {!showQueue && (
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
        )}
        {showQueue && (
          <span className="text-xs text-text-muted">
            {visibleQueue.length} waiting
            {debouncedFilter ? ` · ${debouncedFilter.toUpperCase()}` : ''}
          </span>
        )}
      </div>

      {statusFilter !== 'all' && statusFilter !== 'queue' && (
        <p className="text-xs text-text-muted">
          Status filter is this page only ({filteredJobs.length} of {payload?.jobs.length ?? 0}).
          Issue search hits the server.
        </p>
      )}
      {showQueue && (
        <p className="text-xs text-text-muted">
          Only messages waiting for a free issue/workspace slot. Live runs stay under Live / All.
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

      {showQueue ? (
        visibleQueue.length === 0 ? (
          <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
            Nothing waiting in the queue.
          </div>
        ) : (
          <div className="space-y-2.5">
            {visibleQueue.map((q) => (
              <div key={q.queue_id} className="vd-job">
                <div className="vd-job-bar tone-info" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-text">
                      {q.issue_key || q.queue_id}
                    </span>
                    <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
                      {q.source === 'gitlab' ? 'GitLab' : 'Jira'}
                    </span>
                    <StatusBadge status="queued" size="sm" />
                  </div>
                  <div className="mt-1 text-[15px] text-text">{q.summary || '(no title)'}</div>
                  {q.message?.trim() && (
                    <p className="mt-1 line-clamp-2 whitespace-pre-wrap text-sm text-text-secondary">
                      {q.message}
                    </p>
                  )}
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-text-muted">
                    <span>{q.queue_id}</span>
                    {q.work_branch && (
                      <span>
                        {q.work_branch}
                        {q.target_branch ? ` → ${q.target_branch}` : ''}
                      </span>
                    )}
                    <span>{q.created_at ?? ''}</span>
                    {q.merge_request_url && (
                      <a
                        href={q.merge_request_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-accent-text hover:underline"
                      >
                        Merge request
                      </a>
                    )}
                    {q.job_id && (
                      <Link
                        to={`/jobs/${encodeURIComponent(q.job_id)}`}
                        className="text-accent-text hover:underline"
                      >
                        {q.job_id}
                      </Link>
                    )}
                  </div>
                  {q.error_message && (
                    <div className="mt-1.5 text-xs text-danger-text">{q.error_message}</div>
                  )}
                </div>
                <button
                  type="button"
                  className="shrink-0 text-xs text-danger-text hover:underline"
                  onClick={() => setCancelQueueId(q.queue_id)}
                >
                  Cancel
                </button>
              </div>
            ))}
          </div>
        )
      ) : (
        <JobsTable
          jobs={filteredJobs}
          selectable
          selectedIds={selectedIds}
          onToggleSelect={toggleSelect}
          onOpenJob={(_key, jobId) => navigate(`/jobs/${encodeURIComponent(jobId)}`)}
        />
      )}

      {selectedCount > 0 && !showQueue && (
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

      <ConfirmDialog
        open={Boolean(cancelQueueId)}
        title="Cancel queued message?"
        body="It will not run. Live work is cancelled from the job’s Stop control."
        confirmLabel="Cancel item"
        danger
        onCancel={() => setCancelQueueId(null)}
        onConfirm={() => {
          const id = cancelQueueId
          setCancelQueueId(null)
          if (!id) return
          void cancelQueueItem(id)
            .then(() => loadQueue())
            .catch((e) => {
              setError(e instanceof Error ? e.message : 'Cancel queue item failed')
            })
        }}
      />
    </section>
  )
}
