import type { DashboardPayload } from '../types'
import { StatusBadge } from './StatusBadge'

function formatCountdown(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  const s = Math.max(0, seconds)
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}m ${r}s` : `${r}s`
}

export function PollMonitor({
  data,
  tickLeft,
  onOpenIssue,
}: {
  data: DashboardPayload
  tickLeft: number | null
  onOpenIssue: (key: string) => void
}) {
  return (
    <section className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard label="Phase" value={data.poll.phase} capitalize />
        <div className="ops-card p-4">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Next poll in
          </div>
          <div className="mt-1 font-mono text-2xl font-semibold text-text">
            {formatCountdown(tickLeft ?? data.poll.seconds_until_next_poll)}
          </div>
          <div className="mt-1 text-xs text-text-muted">
            Interval {data.poll.poll_interval_seconds}s
          </div>
        </div>
        <div className="ops-card p-4">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Matched filter
          </div>
          <div className="mt-1 text-lg font-semibold text-text">
            {data.poll.matched_count}
          </div>
          <div className="text-xs text-text-muted">
            Will process: {data.poll.will_process_count}
          </div>
        </div>
        <div className="ops-card p-4">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
            Source
          </div>
          <div className="mt-1 truncate text-sm font-medium text-text">
            {data.poll.source ?? '—'}
          </div>
          <div className="text-xs text-text-muted">
            Board {data.poll.board_id ?? '—'}
          </div>
        </div>
      </div>

      {data.poll.error && (
        <div className="ops-alert ops-alert-danger">
          Poll error: {data.poll.error}
        </div>
      )}

      <div className="ops-table-wrap">
        <table className="ops-table">
          <thead>
            <tr>
              <th>Issue</th>
              <th>Jira status</th>
              <th>Assignee</th>
              <th>Filter</th>
              <th>Local</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {data.poll.issues.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-text-muted">
                  No bot-eligible issues this cycle (trigger label or bot assignee).
                  Unmatched board issues are hidden.
                </td>
              </tr>
            )}
            {data.poll.issues.map((i) => (
              <tr
                key={i.key}
                role="button"
                tabIndex={0}
                className={`cursor-pointer ${
                  i.will_process ? 'bg-accent-muted/40' : ''
                }`}
                onClick={() => onOpenIssue(i.key)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onOpenIssue(i.key)
                  }
                }}
              >
                <td>
                  <div className="font-mono text-accent-text">{i.key}</div>
                  <div className="max-w-sm truncate text-text-secondary">{i.summary}</div>
                  {i.labels.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {i.labels.map((l) => (
                        <span
                          key={l}
                          className="rounded bg-bg-elevated px-1.5 py-0.5 text-[10px] text-text-muted"
                        >
                          {l}
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="text-text-secondary">{i.jira_status || '—'}</td>
                <td className="text-text-secondary">{i.assignee || '—'}</td>
                <td>
                  <div className="flex flex-col gap-0.5 text-xs">
                    <span
                      className={
                        i.matched_label ? 'text-success-text' : 'text-text-muted'
                      }
                    >
                      Label {i.matched_label ? 'match' : 'no'}
                      {i.matched_labels.length > 0 &&
                        ` (${i.matched_labels.join(', ')})`}
                    </span>
                    <span
                      className={
                        i.matched_assignee ? 'text-success-text' : 'text-text-muted'
                      }
                    >
                      Assignee {i.matched_assignee ? 'bot' : 'no'}
                    </span>
                    <span
                      className={i.is_todo ? 'text-info-text' : 'text-text-muted'}
                    >
                      To Do {i.is_todo ? 'yes' : 'no'}
                    </span>
                  </div>
                </td>
                <td>
                  {i.local_status ? (
                    <StatusBadge status={i.local_status} size="sm" />
                  ) : (
                    <span className="text-text-muted">—</span>
                  )}
                </td>
                <td>
                  {i.will_process ? (
                    <span className="rounded border border-accent/40 bg-accent-muted px-2 py-0.5 text-xs font-medium text-accent-text">
                      Queued
                    </span>
                  ) : (
                    <span className="text-xs text-text-muted">Skip</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-text-muted">
        Last poll: {data.poll.last_poll_at ?? '—'} · Cycle {data.poll.cycle} ·
        Showing only bot-eligible issues (label / assignee match)
      </p>
    </section>
  )
}

function StatCard({
  label,
  value,
  capitalize,
}: {
  label: string
  value: string
  capitalize?: boolean
}) {
  return (
    <div className="ops-card p-4">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">
        {label}
      </div>
      <div
        className={`mt-1 text-lg font-semibold text-text ${capitalize ? 'capitalize' : ''}`}
      >
        {value}
      </div>
    </div>
  )
}
