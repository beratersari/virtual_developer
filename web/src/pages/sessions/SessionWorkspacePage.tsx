import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  deleteTempFolder,
  fetchOpencodeWorkspace,
  resetOpencodeSession,
} from '../../api/client'
import type { OpencodeWorkspaceDetail, WorkspacePlanFile } from '../../api/types'
import { rememberJob } from '../../app/entityCache'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { MarkdownBody } from '../../ui/MarkdownBody'
import { PageHeader } from '../../ui/PageHeader'
import { JobsTable } from '../jobs/JobsTable'
import { sessionKindGroup } from '../../util/sessions'

function kindLabel(kind?: string | null): string {
  const group = sessionKindGroup(kind)
  if (group === 'plan') return 'plan'
  if (group === 'build') return 'build'
  if (group === 'test') return 'test'
  const raw = (kind || '').trim()
  return raw || 'legacy'
}

export function SessionWorkspacePage() {
  const { workspaceId = '' } = useParams()
  const navigate = useNavigate()
  const live = useLive()
  const [detail, setDetail] = useState<OpencodeWorkspaceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resetId, setResetId] = useState<string | null>(null)
  const [deleteClone, setDeleteClone] = useState(false)
  const [busy, setBusy] = useState(false)
  const lastGenReload = useRef(0)

  const reload = async () => {
    const id = workspaceId.trim()
    if (!id) return
    try {
      const body = await fetchOpencodeWorkspace(id)
      setDetail(body)
      for (const job of body.jobs || []) rememberJob(job)
      setError(null)
    } catch (e) {
      setDetail(null)
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [workspaceId])
  useEffect(() => {
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload()
  }, [live.generation, workspaceId])

  const w = detail?.workspace
  const target = detail?.sessions.find((s) => s.bind_id === resetId)
  const targetKind = kindLabel(target?.kind)

  return (
    <section className="space-y-5">
      <div>
        <Link to="/sessions" className="vd-btn-ghost mb-3 inline-block text-sm">
          ← Sessions
        </Link>
        <PageHeader
          kicker="OpenCode workspace"
          title={w ? `${w.branch}${w.target_branch ? ` → ${w.target_branch}` : ''}` : 'Workspace'}
          description={
            w
              ? w.repository_key || w.repository_url
              : 'Linked chats and jobs for this repository + source + target.'
          }
        />
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}

      <CloneBlock
        detail={detail}
        deleting={busy && deleteClone}
        onDelete={() => setDeleteClone(true)}
      />

      <PlanFiles plans={detail?.plans || []} />

      <div className="space-y-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          OpenCode sessions
        </h2>
        <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
          {(detail?.sessions || []).map((s) => (
            <li
              key={s.bind_id}
              className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm"
            >
              <div className="min-w-0 space-y-0.5">
                <div className="font-semibold text-text">{kindLabel(s.kind)}</div>
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
          {detail && detail.sessions.length === 0 && (
            <li className="py-6 text-text-muted">No live session binds.</li>
          )}
        </ul>
      </div>

      <div className="space-y-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          Jobs
        </h2>
        <JobsTable
          jobs={detail?.jobs || []}
          compact
          empty="No jobs recorded for this repository + source + target yet."
          onOpenJob={(_issueKey, jobId) =>
            navigate(`/jobs/${encodeURIComponent(jobId)}`)
          }
        />
      </div>

      <ConfirmDialog
        open={Boolean(resetId)}
        title={`Reset this ${targetKind} session?`}
        body={
          target
            ? `Next ${targetKind} job on ${target.branch}${target.target_branch ? ` → ${target.target_branch}` : ''} starts a new ${targetKind} session.\n\nThe other kinds on this workspace are left alone. Does not delete OpenCode’s own history — only our resume pointer.`
            : 'Next job on this bind starts a new session.'
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
      <ConfirmDialog
        open={deleteClone}
        title="Delete this clone?"
        body={
          detail?.clone
            ? `Removes ${detail.clone.path} from disk. Session resume for this workspace will need a fresh clone. Stop a live job first if Delete is disabled.`
            : 'Removes the temp clone from disk.'
        }
        confirmLabel="Delete clone"
        danger
        busy={busy}
        onConfirm={async () => {
          const name = detail?.clone?.name
          if (!name || !detail?.clone?.can_delete) {
            setDeleteClone(false)
            return
          }
          setBusy(true)
          try {
            await deleteTempFolder(name)
            setDeleteClone(false)
            await reload()
          } catch (e) {
            setError(e instanceof Error ? e.message : 'Delete failed')
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setDeleteClone(false)}
      />
    </section>
  )
}

function CloneBlock({
  detail,
  deleting,
  onDelete,
}: {
  detail: OpencodeWorkspaceDetail | null
  deleting: boolean
  onDelete: () => void
}) {
  const clone = detail?.clone
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
          Clone
        </h2>
        <Link to="/storage" className="text-xs text-accent-text hover:underline">
          Open Storage
        </Link>
      </div>
      {!clone ? (
        <div className="vd-panel px-4 py-4 text-sm text-text-muted">
          No clone path is bound to this workspace yet.
        </div>
      ) : (
        <div className="vd-panel flex flex-wrap items-start justify-between gap-3 px-4 py-4">
          <div className="min-w-0">
            <div className="truncate font-mono text-sm text-text">{clone.path}</div>
            <div className="mt-1 text-xs text-text-secondary">
              {!clone.exists
                ? 'Not on disk'
                : [
                    clone.size_label || '0 B',
                    clone.modified_at,
                    clone.in_use ? 'in use' : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
            </div>
          </div>
          <button
            type="button"
            className="vd-btn vd-btn-danger text-xs"
            disabled={deleting || !clone.can_delete}
            title={
              clone.in_use
                ? 'Clone is in use by a running job; stop the job first'
                : !clone.exists
                  ? 'Folder is not on disk'
                  : !clone.name
                    ? 'Clone is not under the temp directory; delete it from Storage if needed'
                    : undefined
            }
            onClick={() => {
              if (!clone.can_delete) return
              onDelete()
            }}
          >
            {deleting ? 'Deleting…' : clone.in_use ? 'In use' : 'Delete'}
          </button>
        </div>
      )}
    </div>
  )
}

function PlanFiles({ plans }: { plans: WorkspacePlanFile[] }) {
  if (plans.length === 0) return null
  return (
    <div className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-[0.12em] text-text-muted">
        Plan files
      </h2>
      <ul className="space-y-3">
        {plans.map((p) => (
          <li
            key={p.issue_key}
            className="rounded-2xl border border-border bg-surface px-4 py-3"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div className="font-mono text-sm font-semibold text-text">{p.issue_key}</div>
              <div className="text-xs text-text-muted">
                {p.exists
                  ? [p.size_label, p.modified_at].filter(Boolean).join(' · ')
                  : 'missing on disk'}
              </div>
            </div>
            <div className="mt-1 truncate font-mono text-[11px] text-text-muted">{p.path}</div>
            {p.exists && p.preview ? (
              <div className="mt-3 max-h-80 overflow-auto rounded-lg border border-border bg-bg px-3 py-2 text-sm">
                <MarkdownBody text={p.preview} />
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}
