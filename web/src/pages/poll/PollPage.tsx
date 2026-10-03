import { useNavigate } from 'react-router-dom'
import { useLive } from '../../app/live'
import { formatChatTime, formatCountdown } from '../../util/time'
import { PageHeader } from '../../ui/PageHeader'
import { StatusBadge } from '../../ui/StatusBadge'

export function PollPage() {
  const live = useLive()
  const navigate = useNavigate()
  const poll = live.poll

  if (!poll) {
    return <p className="text-sm text-text-muted">Waiting for the next board snapshot…</p>
  }

  return (
    <section className="space-y-5">
      <PageHeader
        title="Board"
        description="Work starts when a To Do ticket is assigned to the bot."
      />

      <div className="vd-hero">
        <div className="vd-panel px-5 py-5">
          <div className="text-sm text-text-muted">Next poll</div>
          <div className="mt-1 font-mono text-4xl font-semibold tracking-tight text-text">
            {formatCountdown(live.pollCountdown)}
          </div>
        </div>
        <div className="vd-panel px-5 py-5">
          <div className="text-sm text-text-muted">Will process</div>
          <div className="mt-1 text-4xl font-semibold text-accent-text">{poll.will_process_count}</div>
          {poll.matched_count > 0 && (
            <div className="mt-2 text-sm text-text-secondary">
              of {poll.matched_count} on the board
            </div>
          )}
        </div>
        <div className="vd-panel px-5 py-5">
          <div className="text-sm text-text-muted">Source</div>
          <div className="mt-2 text-lg font-medium">
            {poll.board_id != null ? `Board ${poll.board_id}` : poll.source ?? '—'}
          </div>
          <div className="mt-3 text-xs text-text-muted">
            Last {formatChatTime(poll.last_poll_at) || poll.last_poll_at || '—'}
          </div>
        </div>
      </div>

      <div className="space-y-2.5">
        {poll.issues.length === 0 && (
          <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
            No eligible issues this cycle.
          </div>
        )}
        {poll.issues.map((i) => (
          <button
            key={i.key}
            type="button"
            className="vd-job"
            onClick={() => navigate(`/tasks/${encodeURIComponent(i.key)}`)}
          >
            <div className={`vd-job-bar ${i.will_process ? 'tone-warning' : 'tone-neutral'}`} />
            <div className="min-w-0 text-left">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm font-semibold text-accent-text">{i.key}</span>
                {i.local_status && <StatusBadge status={i.local_status} size="sm" />}
                <span className="text-xs text-text-muted">{i.jira_status || '—'}</span>
              </div>
              <div className="mt-1 truncate text-[15px]">{i.summary || '—'}</div>
              <div className="mt-1.5 flex flex-wrap gap-2 text-xs text-text-muted">
                <span>{i.assignee || 'unassigned'}</span>
                {i.matched_assignee && <span className="text-success-text">bot assignee</span>}
                {i.matched_label && <span className="text-success-text">trigger label</span>}
                {i.is_todo && <span>To Do</span>}
                {i.labels.slice(0, 6).map((l) => (
                  <span key={l} className="vd-tag">
                    {l}
                  </span>
                ))}
              </div>
            </div>
            <div>
              {i.will_process ? (
                <span className="vd-pill vd-pill-solid">This cycle</span>
              ) : (
                <span className="text-xs text-text-muted">Skip</span>
              )}
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}
