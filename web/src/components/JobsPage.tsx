import { useMemo } from 'react'
import {
  jobMatchesFilter,
  type JobStatusFilter,
} from '../lib/status'
import type { JobItem, JobsPayload, TaskItem } from '../types'
import { JobsTable } from './JobsTable'
import { StatusBadge } from './StatusBadge'

const FILTERS: { id: JobStatusFilter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'live', label: 'Live' },
  { id: 'active', label: 'Planned' },
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
  problemTasks,
  connected,
  onOpenJob,
  onOpenIssue,
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
  problemTasks: TaskItem[]
  connected: boolean
  onOpenJob: (issueKey: string, jobId: string) => void
  onOpenIssue: (issueKey: string) => void
}) {
  const rawJobs: JobItem[] = jobsView?.jobs ?? []
  const filteredJobs = useMemo(
    () =>
      rawJobs.filter((j) =>
        jobMatchesFilter(j.status, Boolean(j.live), statusFilter),
      ),
    [rawJobs, statusFilter],
  )

  const total = jobsView?.total ?? 0
  const page = jobsView?.page ?? jobsPage
  const size = jobsView?.page_size ?? jobsPageSize
  const totalPages = Math.max(1, Math.ceil(total / size) || 1)
  const from = total === 0 ? 0 : (page - 1) * size + 1
  const to = Math.min(page * size, total)

  return (
    <section className="space-y-6">
      {problemTasks.length > 0 && (
        <div className="ops-alert ops-alert-danger">
          <h3 className="text-sm font-semibold text-danger-text">
            Attention ({problemTasks.length})
          </h3>
          <p className="mb-2 text-xs text-danger-text/80">
            Issue-level ERROR / in-flight / pending from the state store.
          </p>
          <ul className="space-y-1 text-sm">
            {problemTasks.slice(0, 12).map((t) => (
              <li key={t.issue_key} className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="font-mono text-accent-text hover:underline"
                  title="Open issue / task page"
                  onClick={() => onOpenIssue(t.issue_key)}
                >
                  {t.issue_key}
                </button>
                <StatusBadge status={t.status} size="sm" />
                {t.live && (
                  <span className="text-[10px] uppercase text-warning-text">live</span>
                )}
                {t.error_message && (
                  <span className="truncate text-xs text-text-muted">
                    {t.error_message.slice(0, 120)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-text">Jobs</h2>
          <p className="text-xs text-text-muted">
            Each agent run is a job. Click a job id for that run only; click the
            issue key for the issue page. Board issues live under Poll monitor.
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

      <JobsTable
        jobs={filteredJobs}
        density={density}
        onOpenJob={onOpenJob}
        onOpenIssue={onOpenIssue}
      />
    </section>
  )
}
