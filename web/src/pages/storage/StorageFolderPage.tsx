import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  deleteTempFolder,
  fetchStorageFolderSessions,
  isAbortError,
  resetOpencodeSession,
} from '../../api/client'
import { noteLiveGeneration, usePageLoad } from '../../api/pageLoad'
import type { OpencodeSessionBind, StorageFolder, StorageFolderSessions } from '../../api/types'
import { useLive } from '../../app/live'
import { usePageTitle } from '../../app/pageTitleContext'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'
import { folderPageName } from '../../util/pageTitle'
import { ReviewLinks } from './folderDisplay'
import {
  cloneFolder,
  kindLabel,
  multiRepoLabel,
  resetBody,
} from '../sessions/sessionResetCopy'

function folderHref(folder: StorageFolder): string | null {
  if (folder.job_id) return `/jobs/${encodeURIComponent(folder.job_id)}`
  if (folder.issue_key) return `/tasks/${encodeURIComponent(folder.issue_key)}`
  return null
}

export function StorageFolderPage() {
  const { folderName = '' } = useParams()
  const name = folderName.trim()
  const live = useLive()
  const [detail, setDetail] = useState<StorageFolderSessions | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [resetId, setResetId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState(false)
  const [busy, setBusy] = useState(false)
  const lastGenReload = useRef(0)
  const genSeen = useRef<number | null>(null)
  const flight = usePageLoad()

  const reload = useCallback(async (mode: 'query' | 'tick') => {
    if (!name) return
    const signal = mode === 'tick' ? flight.tick(name) : flight.query(name)
    if (!signal) return
    if (mode === 'query') setError(null)
    try {
      const body = await fetchStorageFolderSessions(name, signal)
      if (signal.aborted) return
      setDetail(body)
      setError(null)
    } catch (e) {
      if (signal.aborted || isAbortError(e)) return
      setDetail(null)
      setError(e instanceof Error ? e.message : 'Load failed')
    } finally {
      if (flight.settle(signal)) void reload('tick')
    }
  }, [flight, name])

  useEffect(() => {
    void reload('query')
  }, [reload])
  useEffect(() => {
    if (!noteLiveGeneration(genSeen, live.generation)) return
    const now = Date.now()
    if (now - lastGenReload.current < 1500) return
    lastGenReload.current = now
    void reload('tick')
  }, [live.generation, reload])

  const loaded = detail && detail.folder.name === name ? detail : null
  const folder = loaded?.folder
  usePageTitle(folder ? folderPageName(folder) : name || 'Folder')

  const deleting = folder?.delete?.status === 'deleting' || folder?.delete?.status === 'done'
  const sizesPending = Boolean(folder?.size_pending)
  useEffect(() => {
    if (!deleting && !sizesPending) return
    let cancelled = false
    let timer: number | undefined
    const tick = () => {
      if (cancelled) return
      void reload('tick')
      timer = window.setTimeout(tick, deleting ? 400 : 2500)
    }
    timer = window.setTimeout(tick, deleting ? 400 : 2500)
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [deleting, reload, sizesPending])

  const sessions = loaded?.sessions || []
  const target = sessions.find((s) => s.bind_id === resetId)
  const targetKind = kindLabel(target?.kind)
  const href = folder ? folderHref(folder) : null
  const pct = Math.max(0, Math.min(100, folder?.delete?.percent ?? 0))

  return (
    <section className="space-y-5">
      <div>
        <Link to="/storage" className="vd-btn-ghost mb-3 inline-block text-sm">
          ← Storage and Sessions
        </Link>
        <PageHeader
          title={folder ? folderPageName(folder) : name || 'Folder'}
          description="OpenCode chats whose working directory is this clone. Claude Code and Codex replies stay on the job Transcript tab."
        />
      </div>
      {error && <p className="text-sm text-danger-text">{error}</p>}
      {!loaded && !error && (
        <p className="flex items-center gap-2 text-sm text-text-muted" aria-busy="true">
          <Spinner /> Loading sessions…
        </p>
      )}
      {folder && (
        <div className="rounded-2xl border border-border bg-surface px-4 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 space-y-0.5">
              <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                {folder.issue_key ? (
                  href ? (
                    <Link
                      to={href}
                      className="font-mono text-sm font-semibold text-accent-text hover:underline"
                    >
                      {folder.issue_key}
                    </Link>
                  ) : (
                    <span className="font-mono text-sm font-semibold text-text">
                      {folder.issue_key}
                    </span>
                  )
                ) : (
                  <span className="font-mono text-sm font-semibold text-text">{folder.name}</span>
                )}
                <ReviewLinks folder={folder} />
              </div>
              <div className="truncate font-mono text-[11px] text-text-muted">{folder.name}</div>
              <div className="truncate font-mono text-[11px] text-text-muted">{folder.path}</div>
              <div className="text-xs text-text-secondary">
                {folder.exists === false
                  ? 'Not on disk'
                  : folder.size_pending
                    ? 'Measuring…'
                    : folder.size_label || '0 B'}
                {folder.modified_at ? ` · ${folder.modified_at}` : ''}
                {folder.in_use ? ' · in use' : ''}
              </div>
              {deleting && (
                <div className="mt-2 max-w-sm">
                  <div className="flex items-center justify-between text-xs text-text-secondary">
                    <span>Deleting…</span>
                    <span className="font-mono tabular-nums">{pct}%</span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-border">
                    <div
                      className="h-full rounded-full bg-danger transition-[width] duration-200"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )}
              {folder.delete?.status === 'error' && (
                <div className="mt-1 text-xs text-danger-text">
                  Delete failed{folder.delete.error ? `: ${folder.delete.error}` : ''}
                </div>
              )}
            </div>
            <button
              type="button"
              className="vd-btn vd-btn-danger text-xs"
              disabled={deleting || folder.in_use || folder.exists === false}
              title={
                folder.in_use
                  ? 'Clone is in use by a running job; stop the job first'
                  : folder.exists === false
                    ? 'Folder is not on disk'
                    : undefined
              }
              onClick={() => {
                if (folder.in_use || deleting || folder.exists === false) return
                setPendingDelete(true)
              }}
            >
              {deleting ? `${pct}%` : folder.in_use ? 'In use' : 'Delete'}
            </button>
          </div>
        </div>
      )}

      {loaded && (
        <div className="space-y-2">
          <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">
            Sessions
          </h2>
          <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
            {sessions.map((s) => (
              <SessionRow key={s.bind_id} session={s} folderName={folder?.name || name} onReset={setResetId} />
            ))}
            {sessions.length === 0 && (
              <li className="py-6 text-sm text-text-muted">
                No OpenCode sessions use this folder.
              </li>
            )}
          </ul>
        </div>
      )}

      <ConfirmDialog
        open={Boolean(resetId)}
        title={target ? `Reset ${target.session_id}?` : `Reset this ${targetKind} session?`}
        body={target ? resetBody(target) : 'Next job on this bind starts a new session.'}
        confirmLabel="Reset session"
        danger
        busy={busy}
        onConfirm={async () => {
          if (!resetId) return
          setBusy(true)
          try {
            await resetOpencodeSession(resetId)
            setResetId(null)
            await reload('query')
          } catch (e) {
            setError(e instanceof Error ? e.message : 'Reset failed')
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setResetId(null)}
      />
      <ConfirmDialog
        open={pendingDelete}
        title="Force-delete this clone?"
        body={
          folder
            ? `Permanently delete ${folderPageName(folder)}\n${folder.path}\n\nThis cannot be undone.`
            : ''
        }
        confirmLabel="Delete"
        danger
        busy={busy}
        onConfirm={async () => {
          if (!folder || folder.in_use || folder.exists === false) {
            setPendingDelete(false)
            return
          }
          setBusy(true)
          setPendingDelete(false)
          setDetail((prev) =>
            prev
              ? {
                  ...prev,
                  folder: {
                    ...prev.folder,
                    delete: { status: 'deleting', percent: prev.folder.delete?.percent ?? 0 },
                  },
                }
              : prev,
          )
          try {
            await deleteTempFolder(folder.name)
          } catch (e) {
            setError(e instanceof Error ? e.message : 'Delete failed')
            await reload('query')
          } finally {
            setBusy(false)
          }
        }}
        onCancel={() => setPendingDelete(false)}
      />
    </section>
  )
}

export function SessionRow({
  session,
  folderName,
  onReset,
  showDirectory = false,
}: {
  session: OpencodeSessionBind
  folderName: string
  onReset: (bindId: string) => void
  showDirectory?: boolean
}) {
  const repos = multiRepoLabel(session.scope)
  const nested = cloneFolder(session.working_directory)
  const showNested = Boolean(nested && nested !== folderName)
  const directory = (session.working_directory || '').trim()
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm">
      <div className="min-w-0 space-y-0.5">
        <div className="font-semibold text-text">
          {kindLabel(session.kind)}
          {repos ? ' · multi-repo' : ''}
        </div>
        <div className="font-mono text-sm text-text">
          {session.branch}
          {session.target_branch ? ` → ${session.target_branch}` : ''}
        </div>
        <div className="font-mono text-[11px] text-text-secondary">
          {session.session_id}
          {session.issue_key ? ` · last ${session.issue_key}` : ''}
          {session.updated_at ? ` · ${session.updated_at}` : ''}
        </div>
        <div className="truncate font-mono text-[11px] text-text-muted">
          {session.repository_key || session.repository_url || '—'}
          {repos ? ` · ${repos}` : ''}
          {showNested ? ` · ${nested}` : ''}
        </div>
        {showDirectory ? (
          <div className="truncate font-mono text-[11px] text-text-muted" title={directory || undefined}>
            {directory || 'No working directory'}
          </div>
        ) : null}
      </div>
      <button
        type="button"
        className="vd-btn vd-btn-secondary text-xs"
        aria-label={`Reset ${session.session_id || 'session'}`}
        onClick={() => onReset(session.bind_id)}
      >
        Reset
      </button>
    </li>
  )
}
