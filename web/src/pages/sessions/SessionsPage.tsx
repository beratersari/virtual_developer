import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { fetchOpencodeWorkspaces } from '../../api/client'
import type { OpencodeWorkspaceItem } from '../../api/types'
import { useLive } from '../../app/live'
import { PageHeader } from '../../ui/PageHeader'

function kindChips(kinds: string[]): string {
  const labels = kinds.map((k) => (k === '' ? 'legacy' : k))
  return labels.join(' · ') || '—'
}

export function SessionsPage() {
  const live = useLive()
  const navigate = useNavigate()
  const [rows, setRows] = useState<OpencodeWorkspaceItem[]>([])
  const [error, setError] = useState<string | null>(null)
  const lastGenReload = useRef(0)

  const reload = async () => {
    try {
      const p = await fetchOpencodeWorkspaces()
      setRows(p.workspaces || [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [])
  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload()
  }, [live.generation])

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="OpenCode"
        title="Sessions"
        description="Each row is one repository + source + target. Open it to see the plan/build/test chats and every job that ran there."
      />
      {error && <p className="text-sm text-danger-text">{error}</p>}

      <div className="space-y-2.5">
        {rows.length === 0 && (
          <div className="vd-panel px-5 py-10 text-center text-sm text-text-muted">
            No bound workspaces yet. A plan, build, or test run creates one.
          </div>
        )}
        {rows.map((w) => (
          <button
            key={w.workspace_id}
            type="button"
            className="vd-job w-full text-left"
            onClick={() =>
              navigate(`/sessions/${encodeURIComponent(w.workspace_id)}`)
            }
          >
            <div className="vd-job-bar tone-neutral" />
            <div className="min-w-0">
              <div className="font-mono text-xs text-text-muted">
                {w.repository_key || w.repository_url || '—'}
              </div>
              <div className="mt-0.5 font-mono text-sm font-semibold text-text">
                {w.branch}
                {w.target_branch ? ` → ${w.target_branch}` : ''}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
                <span>{kindChips(w.kinds)}</span>
                <span>
                  {w.session_count} session{w.session_count === 1 ? '' : 's'}
                </span>
                <span>
                  {w.job_count} job{w.job_count === 1 ? '' : 's'}
                </span>
                {w.issue_key ? <span>last {w.issue_key}</span> : null}
                {w.updated_at ? <span>{w.updated_at}</span> : null}
              </div>
            </div>
          </button>
        ))}
      </div>
    </section>
  )
}
