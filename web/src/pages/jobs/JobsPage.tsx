import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { cancelQueueItem, deleteJobs, fetchJobs, fetchQueue, isAbortError } from '../../api/client'
import { noteLiveGeneration, usePageLoad } from '../../api/pageLoad'
import {
  queuePlaceholder,
  shouldLoadQueueForFilter,
  shouldRefreshQueueList,
  waitingQueueRows,
} from './queueRefresh'
import type { JobsPayload, QueueItem } from '../../api/types'
import { useLive } from '../../app/live'
import { jobsFilterEcho, sortJobsByCreatedAt } from '../../util/jobs'
import {
  jobIsDeletable,
  jobMatchesFilter,
  type JobStatusFilter,
} from '../../util/status'
import { Alert } from '../../ui/Alert'
import { Spinner } from '../../ui/Spinner'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { LiveDot } from '../../ui/LiveDot'
import { PageHeader } from '../../ui/PageHeader'
import { StatusBadge } from '../../ui/StatusBadge'
import {
  peekJobsPayload,
  peekQueuePayload,
  rememberJobsPayload,
  rememberQueuePayload,
} from '../../app/entityCache'
import { JobsTable } from './JobsTable'
import { jobsFilterFromPath, jobsFilterPath, jobsPageFromPath } from './jobsFilterUrl'
import { listPageFromSegment, withListPage } from '../../util/listPageUrl'
import { JobDetailPage } from './JobDetailPage'

const FILTERS: { id: JobStatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'In flight' },
  { id: 'plan_ready', label: 'Plan ready' },
  { id: 'queue', label: 'Queue' },
  { id: 'error', label: 'Error' },
  { id: 'completed', label: 'Completed' },
  { id: 'cancelled', label: 'Cancelled' },
]

const PAGE_SIZE = 25

/** `/jobs/2` is a list page. `/jobs/job_…` is a job. */
export function JobsAtJobOrPage() {
  const { jobId = '', section = '' } = useParams()
  if (!section && listPageFromSegment(jobId)) return <JobsPage />
  return <JobDetailPage />
}

export function JobsPage() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const live = useLive()
  const statusFilter = jobsFilterFromPath(pathname)
  const page = jobsPageFromPath(pathname)
  const [issueFilter, setIssueFilter] = useState('')
  const [debouncedFilter, setDebouncedFilter] = useState('')
  const [payload, setPayload] = useState<JobsPayload | null>(() => peekJobsPayload())
  const [queueItems, setQueueItems] = useState<QueueItem[]>(() =>
    waitingQueueRows(peekQueuePayload()?.items),
  )
  const [queueQueued, setQueueQueued] = useState(
    () => peekQueuePayload()?.queued_count ?? 0,
  )
  const [queueReady, setQueueReady] = useState(() => peekQueuePayload() != null)
  const [queueError, setQueueError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [bulkDeleting, setBulkDeleting] = useState(false)
  const [cancelQueueId, setCancelQueueId] = useState<string | null>(null)
  const reqId = useRef(0)
  const queueReq = useRef(0)
  const lastFetchedQueued = useRef(-1)
  const lastGenReload = useRef(0)
  const genSeen = useRef<number | null>(null)
  const jobsFlight = usePageLoad()
  const queueFlight = usePageLoad()
  const viewKey = `${statusFilter}|${page}|${debouncedFilter}`
  const [shownFor, setShownFor] = useState<string | null>(null)

  const filterRef = useRef(issueFilter)
  useEffect(() => {
    const t = window.setTimeout(() => {
      const changed = filterRef.current !== issueFilter
      filterRef.current = issueFilter
      setDebouncedFilter(issueFilter.trim())
      if (changed && jobsPageFromPath(pathname) > 1) navigate(jobsFilterPath(statusFilter))
    }, 250)
    return () => window.clearTimeout(t)
  }, [issueFilter, navigate, pathname, statusFilter])

  const loadQueue = useCallback(async (mode: 'query' | 'tick' = 'tick') => {
    const signal = mode === 'query' ? queueFlight.query('queue') : queueFlight.tick('queue')
    if (!signal) return
    const req = ++queueReq.current
    try {
      const q = await fetchQueue({ status: 'queued', limit: 200, signal })
      if (signal.aborted || req !== queueReq.current) return
      const rows = waitingQueueRows(q.items)
      const count =
        typeof q.queued_count === 'number' ? q.queued_count : rows.length
      rememberQueuePayload({ ...q, items: rows })
      lastFetchedQueued.current = count
      setQueueItems(rows)
      setQueueQueued(count)
      setQueueReady(true)
      setQueueError(null)
    } catch (e) {
      if (signal.aborted || isAbortError(e)) return
      // A failed refresh must not wipe rows that are already on screen.
      // The first failure has no rows, so it must not look like an empty queue.
      if (req === queueReq.current) {
        setQueueError(e instanceof Error ? e.message : 'Could not load the queue')
      }
    } finally {
      if (queueFlight.settle(signal)) void loadQueue('tick')
    }
  }, [queueFlight])

  const load = useCallback(
    async (opts?: { filter?: string; page?: number; tick?: boolean }) => {
      const filter = opts?.filter ?? debouncedFilter
      const nextPage = opts?.page ?? page
      const key = `${statusFilter}|${nextPage}|${filter}`
      const signal = opts?.tick ? jobsFlight.tick(key) : jobsFlight.query(key)
      if (!signal) return
      const req = ++reqId.current
      if (!opts?.tick) setError(null)
      try {
        const data = await fetchJobs({
          issueKey: filter || undefined,
          status: statusFilter,
          page: nextPage,
          pageSize: PAGE_SIZE,
          signal,
        })
        if (signal.aborted || req !== reqId.current) return
        rememberJobsPayload(data)
        setPayload(data)
        setShownFor(key)
        setError(null)
        const total = data.total ?? 0
        const size = data.page_size ?? PAGE_SIZE
        const pages = Math.max(1, Math.ceil(total / size) || 1)
        const landed = data.page ?? nextPage
        if (landed > pages) {
          navigate(withListPage(jobsFilterPath(statusFilter), pages), { replace: true })
        }
      } catch (e) {
        if (signal.aborted || isAbortError(e) || req !== reqId.current) return
        // A failed load must not replace the list with an empty filter.
        // Rows already on screen stay. The first failure has no rows, so
        // the alert is the result.
        setShownFor(key)
        setError(e instanceof Error ? e.message : 'Failed to load jobs')
      } finally {
        const follow = jobsFlight.settle(signal)
        if (!signal.aborted && req === reqId.current) void loadQueue('tick')
        if (follow) void load({ filter, page: nextPage, tick: true })
      }
    },
    [debouncedFilter, jobsFlight, loadQueue, navigate, page, statusFilter],
  )

  useEffect(() => {
    // The queue list is its own request. Waiting for the jobs page made the
    // tab look empty until that slower call returned.
    if (shouldLoadQueueForFilter(statusFilter)) return
    void load({ filter: debouncedFilter, page })
  }, [debouncedFilter, page, load, statusFilter])

  useEffect(() => {
    if (!noteLiveGeneration(genSeen, live.generation)) return
    if (shouldLoadQueueForFilter(statusFilter)) return
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void load({ tick: true })
  }, [live.generation, load, statusFilter])

  useEffect(() => {
    if (shouldLoadQueueForFilter(statusFilter)) {
      void loadQueue('query')
      return
    }
    if (!shouldRefreshQueueList(statusFilter, live.queueQueued, lastFetchedQueued.current)) {
      return
    }
    void loadQueue('tick')
  }, [statusFilter, live.queueQueued, live.generation, loadQueue])

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
    const needle = debouncedFilter.trim().toLocaleLowerCase()
    if (!needle) return queueItems
    return queueItems.filter((q) => {
      const key = (q.issue_key || '').toLocaleLowerCase()
      const title = (q.summary || '').toLocaleLowerCase()
      const body = (q.message || '').toLocaleLowerCase()
      return key.includes(needle) || title.includes(needle) || body.includes(needle)
    })
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
  const badgeQueued = Math.max(live.queueQueued, queueQueued, queueItems.length)

  return (
    <section className="space-y-5">
      <PageHeader
        title="Jobs"
        description={
          live.connected
            ? 'Queue holds messages waiting for a free slot.'
            : 'Disconnected. This list may be stale.'
        }
        actions={
          <label className="block text-xs text-text-muted">
            Search
            <input
              className="vd-input mt-1 w-64"
              placeholder="Key, title, or description"
              value={issueFilter}
              onChange={(e) => setIssueFilter(e.target.value)}
            />
          </label>
        }
      />

      {liveJobs.length > 0 && statusFilter !== 'active' && statusFilter !== 'queue' && (
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
        <div className="vd-seg">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              aria-pressed={statusFilter === f.id}
              onClick={() => navigate(jobsFilterPath(f.id))}
              className={`vd-seg-btn ${statusFilter === f.id ? 'is-on' : ''}`}
            >
              {f.id === 'queue' ? `Queue (${badgeQueued})` : f.label}
            </button>
          ))}
        </div>
        {!showQueue && (
          <div className="flex items-center gap-2 text-xs text-text-muted">
            <span>
              {from}–{to} of {total}
              {debouncedFilter ? ` · ${jobsFilterEcho(debouncedFilter)}` : ''}
            </span>
            <button
              type="button"
              disabled={currentPage <= 1}
              onClick={() => navigate(withListPage(jobsFilterPath(statusFilter), currentPage - 1))}
              className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
            >
              Prev
            </button>
            <button
              type="button"
              disabled={currentPage >= totalPages}
              onClick={() => navigate(withListPage(jobsFilterPath(statusFilter), currentPage + 1))}
              className="vd-btn vd-btn-secondary px-3 py-1 text-xs"
            >
              Next
            </button>
          </div>
        )}
        {showQueue && (
          <span className="text-xs text-text-muted">
            {visibleQueue.length} waiting
            {debouncedFilter ? ` · ${jobsFilterEcho(debouncedFilter)}` : ''}
          </span>
        )}
      </div>

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

      {showQueue && queueError && visibleQueue.length > 0 && (
        <Alert>{queueError}</Alert>
      )}

      {showQueue ? (
        visibleQueue.length === 0 && queueError ? (
          <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
            {queueError}
          </div>
        ) : queuePlaceholder(queueReady, visibleQueue.length) === 'loading' ? (
          <div
            className="vd-panel px-5 py-10 text-center text-sm text-text-muted"
            aria-busy="true"
          >
            Loading queue…
          </div>
        ) : queuePlaceholder(queueReady, visibleQueue.length) === 'empty' ? (
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
                      {q.source === 'gitlab'
                        ? 'GitLab'
                        : q.source === 'azure' || q.source === 'azure_workitem'
                          ? 'Azure'
                          : 'Jira'}
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
      ) : shownFor !== viewKey && !error ? (
        <div
          className="vd-panel px-5 py-10 text-center text-sm text-text-muted"
          aria-busy="true"
        >
          <span className="inline-flex items-center gap-2">
            <Spinner /> Loading jobs…
          </span>
        </div>
      ) : error && filteredJobs.length === 0 ? null : (
        <JobsTable
          jobs={filteredJobs}
          selectable
          selectedIds={selectedIds}
          onToggleSelect={toggleSelect}
          fallbackWorker={live.settings?.agent_backend || ''}
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
          'Removes job history records and linked session/prompt files under YAVER_DATA_DIR.\n' +
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
