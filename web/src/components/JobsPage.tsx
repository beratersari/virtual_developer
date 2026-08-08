import { useEffect, useMemo, useState } from 'react'
import {
  jobIsDeletable,
  jobMatchesFilter,
  type JobStatusFilter,
} from '../util/status'
import type { JobItem, JobsPayload } from '../types'
import { JobsTable } from './JobsTable'

const FILTERS: { id: JobStatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'active', label: 'Active' },
  { id: 'error', label: 'Error' },
  { id: 'completed', label: 'Completed' },
  { id: 'cancelled', label: 'Cancelled' },
]

export function JobsPage({
  jobsView,
  jobsPage,
  jobsPageSize,
  issueFilter,
  setIssueFilter,
  statusFilter,
  setStatusFilter,
  density,
  setDensity,
  setJobsPage,
  connected,
  onOpenJob,
  onOpenIssue,
  onBulkDelete,
  bulkDeleting = false,
}: {
  jobsView: JobsPayload | null
  jobsPage: number
  jobsPageSize: number
  issueFilter: string
  setIssueFilter: (v: string) => void
  statusFilter: JobStatusFilter
  setStatusFilter: (v: JobStatusFilter) => void
  density: 'comfortable' | 'compact'
  setDensity: (v: 'comfortable' | 'compact') => void
  setJobsPage: (fn: (p: number) => number) => void
  connected: boolean
  onOpenJob: (issueKey: string, jobId: string) => void
  onOpenIssue: (issueKey: string) => void
  /** Delete selected historical jobs; returns when done so selection can clear. */
  onBulkDelete?: (jobIds: string[]) => Promise<void>
  bulkDeleting?: boolean
}) {
  const rawJobs: JobItem[] = jobsView?.jobs ?? []
  const filteredJobs = useMemo(
    () =>
      rawJobs.filter((j) =>
        jobMatchesFilter(j.status, Boolean(j.live), statusFilter),
      ),
    [rawJobs, statusFilter],
  )

  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set())
  const [actionError, setActionError] = useState<string | null>(null)

  // Drop selection when the page of rows changes (filter / page / reload).
  const visibleIdKey = filteredJobs.map((j) => j.job_id).join('|')
  useEffect(() => {
    setSelectedIds((prev) => {
      if (prev.size === 0) return prev
      const visible = new Set(filteredJobs.map((j) => j.job_id))
      const next = new Set([...prev].filter((id) => visible.has(id)))
      return next.size === prev.size ? prev : next
    })
    // Only re-prune when the visible id set changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleIdKey])

  const deletableOnPage = useMemo(
    () =>
      filteredJobs.filter((j) => jobIsDeletable(j.status, Boolean(j.live))),
    [filteredJobs],
  )

  const selectedCount = selectedIds.size

  const toggleSelect = (jobId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })
  }

  const toggleSelectAll = () => {
    setSelectedIds((prev) => {
      const deletableIds = deletableOnPage.map((j) => j.job_id)
      const allSelected =
        deletableIds.length > 0 && deletableIds.every((id) => prev.has(id))
      if (allSelected) {
        const next = new Set(prev)
        for (const id of deletableIds) next.delete(id)
        return next
      }
      const next = new Set(prev)
      for (const id of deletableIds) next.add(id)
      return next
    })
  }

  const onDeleteSelected = async () => {
    if (!onBulkDelete || selectedCount === 0 || bulkDeleting) return
    const ids = [...selectedIds]
    if (
      !window.confirm(
        `Permanently delete ${ids.length} selected job(s)?\n\n` +
          'Removes job history records and linked session/prompt files under .jira-agent. ' +
          'Does not change Jira issues. Live / in-flight jobs are skipped.',
      )
    ) {
      return
    }
    setActionError(null)
    try {
      await onBulkDelete(ids)
      setSelectedIds(new Set())
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  const total = jobsView?.total ?? 0
  const page = jobsView?.page ?? jobsPage
  const size = jobsView?.page_size ?? jobsPageSize
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (page - 1) * size + 1
  const to = Math.min(page * size, total)

  return (
    <section className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">Jobs</h2>
          <p className="text-xs text-text-muted">
            Each agent run is a job. Click a job id for that run only; click the
            issue key for the issue page. Select rows to delete finished runs.
            {!connected && ' · WebSocket disconnected (list may be stale)'}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-text-muted">
            Filter by Jira issue
            <input
              className="ops-input ml-2 w-40 font-mono"
              placeholder="e.g. KAN-1"
              value={issueFilter}
              onChange={(e) => setIssueFilter(e.target.value)}
            />
          </label>
          {issueFilter.trim() && (
            <button
              type="button"
              className="ops-btn-ghost text-xs"
              onClick={() => setIssueFilter('')}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {/* Status chips + density */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setStatusFilter(f.id)}
              className={`rounded px-2.5 py-1 text-xs font-medium transition-colors ${
                statusFilter === f.id
                  ? 'bg-accent text-white'
                  : 'border border-border bg-surface text-text-secondary hover:bg-surface-hover hover:text-text'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-xs text-text-muted">
          <span>Density</span>
          <button
            type="button"
            className={`rounded px-2 py-1 ${
              density === 'comfortable'
                ? 'bg-accent-muted text-accent-text'
                : 'border border-border text-text-muted'
            }`}
            onClick={() => setDensity('comfortable')}
          >
            Comfortable
          </button>
          <button
            type="button"
            className={`rounded px-2 py-1 ${
              density === 'compact'
                ? 'bg-accent-muted text-accent-text'
                : 'border border-border text-text-muted'
            }`}
            onClick={() => setDensity('compact')}
          >
            Compact
          </button>
        </div>
      </div>

      {statusFilter !== 'all' && (
        <p className="text-xs text-text-muted">
          Status filter applies to the current page ({filteredJobs.length} of{' '}
          {rawJobs.length} rows shown). Use issue filter for server-side narrowing.
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-text-muted">
        <span>
          Showing {from}–{to} of {total} job(s)
          {issueFilter.trim() ? ` for ${issueFilter.trim().toUpperCase()}` : ''}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={page <= 1}
            onClick={() => setJobsPage((p) => Math.max(1, p - 1))}
            className="ops-btn ops-btn-secondary px-2 py-1 text-xs"
          >
            Previous
          </button>
          <span className="font-mono text-text-secondary">
            Page {page} / {totalPages}
          </span>
          <button
            type="button"
            disabled={page >= totalPages}
            onClick={() => setJobsPage((p) => p + 1)}
            className="ops-btn ops-btn-secondary px-2 py-1 text-xs"
          >
            Next
          </button>
        </div>
      </div>

      {onBulkDelete && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded border border-border bg-surface px-3 py-2">
          <span className="text-xs text-text-muted">
            {selectedCount === 0
              ? `Select finished jobs to delete (${deletableOnPage.length} deletable on this page)`
              : `${selectedCount} job(s) selected`}
          </span>
          <div className="flex flex-wrap items-center gap-2">
            {selectedCount > 0 && (
              <button
                type="button"
                className="ops-btn ops-btn-secondary px-2 py-1 text-xs"
                disabled={bulkDeleting}
                onClick={() => setSelectedIds(new Set())}
              >
                Clear selection
              </button>
            )}
            <button
              type="button"
              className="ops-btn ops-btn-danger px-2.5 py-1 text-xs"
              disabled={selectedCount === 0 || bulkDeleting}
              onClick={() => void onDeleteSelected()}
              title={
                selectedCount === 0
                  ? 'Select one or more finished jobs'
                  : 'Permanently delete selected job records'
              }
            >
              {bulkDeleting
                ? 'Deleting…'
                : selectedCount > 0
                  ? `Delete selected (${selectedCount})`
                  : 'Delete selected'}
            </button>
          </div>
        </div>
      )}

      {actionError && (
        <div role="alert" className="ops-alert ops-alert-danger text-xs">
          {actionError}
        </div>
      )}

      <JobsTable
        jobs={filteredJobs}
        density={density}
        onOpenJob={onOpenJob}
        onOpenIssue={onOpenIssue}
        selectable={Boolean(onBulkDelete)}
        selectedIds={selectedIds}
        onToggleSelect={toggleSelect}
        onToggleSelectAll={toggleSelectAll}
      />
    </section>
  )
}
