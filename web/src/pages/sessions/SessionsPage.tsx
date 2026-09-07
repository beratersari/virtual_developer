import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchOpencodeSessions, resetOpencodeSession } from '../../api/client'
import type { OpencodeSessionBind } from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { groupSessionBinds, sessionKindGroup } from '../../util/sessions'

function kindLabel(kind?: string | null): string {
  const group = sessionKindGroup(kind)
  if (group === 'plan') return 'plan'
  if (group === 'build') return 'build'
  return 'legacy'
}

function SessionList({
  rows,
  empty,
  onReset,
}: {
  rows: OpencodeSessionBind[]
  empty: string
  onReset: (bindId: string) => void
}) {
  return (
    <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
      {rows.map((s) => (
        <li key={s.bind_id} className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm">
          <div className="min-w-0 space-y-0.5">
            <div className="font-mono text-xs text-text-muted">
              {s.repository_key || s.repository_url}
            </div>
            <div className="font-mono text-sm font-semibold text-text">
              {s.branch}
              {s.target_branch ? ` → ${s.target_branch}` : ''}
            </div>
            <div className="font-mono text-[11px] text-text-secondary">
              {s.session_id}
              {s.issue_key ? ` · last ${s.issue_key}` : ''}
              {s.updated_at ? ` · ${s.updated_at}` : ''}
            </div>
            {s.working_directory && (
              <div className="truncate font-mono text-[11px] text-text-muted">
                {s.working_directory}
              </div>
            )}
          </div>
          <button
            type="button"
            className="vd-btn vd-btn-secondary text-xs"
            onClick={() => onReset(s.bind_id)}
          >
            Reset
          </button>
        </li>
      ))}
      {rows.length === 0 && <li className="py-6 text-text-muted">{empty}</li>}
    </ul>
  )
}

export function SessionsPage() {
  const live = useLive()
  const [rows, setRows] = useState<OpencodeSessionBind[]>([])
  const [error, setError] = useState<string | null>(null)
  const [resetId, setResetId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const lastGenReload = useRef(0)

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
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload()
  }, [live.generation])

  const groups = useMemo(() => groupSessionBinds(rows), [rows])
  const target = rows.find((r) => r.bind_id === resetId)
  const targetKind = kindLabel(target?.kind)

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="OpenCode"
        title="Sessions"
        description="Plan and build keep separate sessions for the same repository + Source + Target. Reset only that map; the other kind is unchanged."
      />
      {error && <p className="text-sm text-danger-text">{error}</p>}

      <div className="space-y-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          Plan
        </h2>
        <SessionList
          rows={groups.plan}
          empty="No plan sessions bound yet."
          onReset={setResetId}
        />
      </div>

      <div className="space-y-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          Build
        </h2>
        <SessionList
          rows={groups.build}
          empty="No build sessions bound yet."
          onReset={setResetId}
        />
      </div>

      {groups.other.length > 0 && (
        <div className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
            Other
          </h2>
          <SessionList
            rows={groups.other}
            empty="No other sessions."
            onReset={setResetId}
          />
        </div>
      )}

      <ConfirmDialog
        open={Boolean(resetId)}
        title={`Reset this ${targetKind} session?`}
        body={
          target
            ? `Next ${targetKind} job on ${target.branch}${target.target_branch ? ` → ${target.target_branch}` : ''} (${target.repository_key || target.repository_url}) starts a new ${targetKind} session.\n\nThe other kind (plan vs build) is left alone. Does not delete OpenCode’s own history — only our resume pointer.`
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
