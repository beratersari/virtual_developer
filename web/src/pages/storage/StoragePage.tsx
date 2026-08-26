import { useEffect, useState } from 'react'
import { deleteTempFolder, fetchStorage } from '../../api/client'
import type { StorageFolder, StoragePayload } from '../../api/types'
import { useLive } from '../../app/live'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

export function StoragePage() {
  const live = useLive()
  const [data, setData] = useState<StoragePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<StorageFolder | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = async () => {
    try {
      const payload = await fetchStorage()
      setData(payload)
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

  const onDelete = async () => {
    if (!pending) return
    setBusy(true)
    try {
      await deleteTempFolder(pending.name)
      setPending(null)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    } finally {
      setBusy(false)
    }
  }

  const disk = data?.disk
  const usedPct = Math.max(0, Math.min(100, disk?.used_percent ?? 0))

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Host"
        title="Storage"
        description="Disk that holds cloned workspaces (TEMP_DIR_BASE). Force-delete removes the folder including Windows reserved names such as nul."
        actions={
          <button type="button" className="vd-btn vd-btn-secondary text-xs" onClick={() => void reload()}>
            Refresh
          </button>
        }
      />
      {error && <p className="text-sm text-danger-text">{error}</p>}
      {!data && !error && (
        <div className="flex items-center gap-2 text-sm text-text-muted">
          <Spinner /> Loading…
        </div>
      )}
      {disk && (
        <div className="rounded-2xl border border-border bg-surface px-4 py-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">
                Volume {disk.volume}
              </div>
              <div className="mt-1 font-mono text-sm text-text">{disk.path}</div>
            </div>
            <div className="text-right text-sm">
              <div className="font-semibold text-text">{disk.free_label} free</div>
              <div className="text-text-muted">
                {disk.used_label} used of {disk.total_label}
              </div>
            </div>
          </div>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-border">
            <div
              className="h-full rounded-full bg-accent"
              style={{ width: `${usedPct}%` }}
              title={`${usedPct}% used`}
            />
          </div>
          <div className="mt-2 text-xs text-text-secondary">
            {data.folder_count} clone folder{data.folder_count === 1 ? '' : 's'} · {data.folders_label}
          </div>
        </div>
      )}
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {(data?.folders || []).map((folder) => (
          <li key={folder.name} className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm">
            <div className="min-w-0 space-y-0.5">
              <div className="font-mono text-sm font-semibold text-text">{folder.name}</div>
              <div className="truncate font-mono text-[11px] text-text-muted">{folder.path}</div>
              <div className="text-xs text-text-secondary">
                {folder.size_label}
                {folder.modified_at ? ` · ${folder.modified_at}` : ''}
                {folder.in_use ? ' · in use' : ''}
              </div>
            </div>
            <button
              type="button"
              className="vd-btn vd-btn-danger text-xs"
              onClick={() => setPending(folder)}
            >
              Delete
            </button>
          </li>
        ))}
        {data && data.folders.length === 0 && (
          <li className="py-6 text-text-muted">No clone folders under this path.</li>
        )}
      </ul>
      <ConfirmDialog
        open={Boolean(pending)}
        title="Force-delete this clone?"
        body={
          pending
            ? `Permanently delete ${pending.path}\n\nThis cannot be undone. Windows reserved files (nul, con, …) are removed with rd /s /q \\\\?\\… and del \\\\.\\…`
            : ''
        }
        confirmLabel="Force delete"
        danger
        busy={busy}
        onConfirm={() => void onDelete()}
        onCancel={() => {
          if (!busy) setPending(null)
        }}
      />
    </section>
  )
}
