import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { cancelQueueItem, fetchQueue } from '../../api/client'
import type { QueueItem } from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { StatusBadge } from '../../ui/StatusBadge'

export function QueuePage() {
  const live = useLive()
  const [items, setItems] = useState<QueueItem[]>([])
  const [queued, setQueued] = useState(0)
  const [running, setRunning] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [cancelId, setCancelId] = useState<string | null>(null)
  const [filter, setFilter] = useState<'open' | 'all'>('open')

  const reload = async () => {
    try {
      const p = await fetchQueue({ limit: 200 })
      const rows = p.items || []
      setQueued(p.queued_count || 0)
      setRunning(p.running_count || 0)
      setItems(
        filter === 'open'
          ? rows.filter((r) => r.status === 'queued' || r.status === 'running')
          : rows,
      )
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [filter])
  useEffect(() => {
    void reload()
  }, [live.generation])

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Waiting"
        title="Queue"
        description="Jira issues and GitLab MR comments wait here when the same work branch is busy. FIFO per repo + branch + target."
      />
      <div className="flex flex-wrap items-center gap-3 text-sm text-text-secondary">
        <span>
          Queued <span className="font-mono text-text">{queued}</span>
        </span>
        <span>
          Running <span className="font-mono text-text">{running}</span>
        </span>
        <div className="ml-auto flex gap-1 rounded-full border border-border p-1">
          <button
            type="button"
            className={`rounded-full px-3 py-1 text-sm ${
              filter === 'open' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted'
            }`}
            onClick={() => setFilter('open')}
          >
            Open
          </button>
          <button
            type="button"
            className={`rounded-full px-3 py-1 text-sm ${
              filter === 'all' ? 'bg-accent text-[#1a0d08]' : 'text-text-muted'
            }`}
            onClick={() => setFilter('all')}
          >
            All
          </button>
        </div>
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}
      {items.length === 0 ? (
        <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
          Nothing in the queue.
        </div>
      ) : (
        <div className="space-y-2.5">
          {items.map((q) => (
            <div key={q.queue_id} className="vd-job">
              <div className="vd-job-bar" />
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-sm font-semibold text-text">
                    {q.issue_key || q.queue_id}
                  </span>
                  <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-text-muted">
                    {q.source === 'gitlab' ? 'GitLab' : 'Jira'}
                  </span>
                  <StatusBadge status={q.status} size="sm" />
                </div>
                <div className="mt-1 text-[15px] text-text">
                  {q.summary || '(no title)'}
                </div>
                {q.message?.trim() && (
                  <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-sm text-text-secondary">
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
                      to={`/jobs/${q.job_id}`}
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
              {q.status === 'queued' && (
                <button
                  type="button"
                  className="text-xs text-danger-text hover:underline"
                  onClick={() => setCancelId(q.queue_id)}
                >
                  Cancel
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <ConfirmDialog
        open={Boolean(cancelId)}
        title="Cancel queued message?"
        body="It will not run. Running jobs are cancelled from the Jobs page."
        confirmLabel="Cancel item"
        onCancel={() => setCancelId(null)}
        onConfirm={() => {
          const id = cancelId
          setCancelId(null)
          if (!id) return
          void cancelQueueItem(id).then(() => reload()).catch((e) => {
            setError(e instanceof Error ? e.message : 'Cancel failed')
          })
        }}
      />
    </section>
  )
}
