import { useEffect, useState } from 'react'
import { fetchOpencodeSessions, resetOpencodeSession } from '../../api/client'
import type { OpencodeSessionBind } from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'

export function SessionsPage() {
  const live = useLive()
  const [rows, setRows] = useState<OpencodeSessionBind[]>([])
  const [error, setError] = useState<string | null>(null)
  const [resetId, setResetId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = async () => {
    try {
      const p = await fetchOpencodeSessions()
      setRows(p.sessions || [])
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [])
  useEffect(() => {
    void reload()
  }, [live.generation])

  const target = rows.find((r) => r.bind_id === resetId)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="OpenCode"
        title="Sessions"
        description="One session per git repository + work branch. The next job on that branch continues the same OpenCode session (CLI --session or serve). Reset starts cold."
      />
      {error && <p className="text-sm text-danger-text">{error}</p>}
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {rows.map((s) => (
          <li key={s.bind_id} className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm">
            <div className="min-w-0 space-y-0.5">
              <div className="font-mono text-xs text-text-muted">
                {s.repository_key || s.repository_url}
              </div>
              <div className="font-mono text-sm font-semibold text-text">{s.branch}</div>
              <div className="font-mono text-[11px] text-text-secondary">
                {s.session_id}
                {s.issue_key ? ` · last ${s.issue_key}` : ''}
                {s.updated_at ? ` · ${s.updated_at}` : ''}
              </div>
            </div>
            <button
              type="button"
              className="vd-btn vd-btn-secondary text-xs"
              onClick={() => setResetId(s.bind_id)}
            >
              Reset
            </button>
          </li>
        ))}
        {rows.length === 0 && (
          <li className="py-6 text-text-muted">No bound sessions yet.</li>
        )}
      </ul>
      <ConfirmDialog
        open={Boolean(resetId)}
        title="Reset this OpenCode session?"
        body={
          target
            ? `Next job on ${target.branch} (${target.repository_key || target.repository_url}) starts a new session.\n\nDoes not delete OpenCode’s own history — only our resume pointer.`
            : 'Next job on this branch starts a new session.'
        }
        confirmLabel="Reset session"
        danger
        busy={busy}
        onConfirm={async () => {
          if (!resetId) return
          setBusy(true)
          try {
            await resetOpencodeSession(resetId)
            setResetId(null)
            await reload()
          } catch (e) {
            setError(e instanceof Error ? e.message : 'Reset failed')
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setResetId(null)}
      />
    </section>
  )
}
