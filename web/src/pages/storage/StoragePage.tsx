import { useEffect, useState } from 'react'
import { deleteTempFolder, fetchStorage, fetchStorageDeletes } from '../../api/client'
import type { StorageDeleteJob, StorageFolder, StoragePayload } from '../../api/types'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

function applyDeletes(prev: StoragePayload | null, deletes: StorageDeleteJob[]): StoragePayload | null {
  if (!prev) return prev
  const byName = new Map(
    deletes.filter((d) => (d.area || 'temp') === 'temp').map((d) => [d.name, d]),
  )

  const next = prev.folders.map((folder) => {
    const job = byName.get(folder.name)
    if (!job) {
      if (folder.delete?.status === 'deleting' || folder.delete?.status === 'done') {
        return { ...folder, delete: undefined }
      }
      return folder
    }
    return {
      ...folder,
      delete: { status: job.status, percent: job.percent, error: job.error },
    }
  })
  for (const job of byName.values()) {
    if (next.some((f) => f.name === job.name)) continue
    next.push({
      name: job.name,
      path: job.path || job.name,
      size_bytes: 0,
      size_label: '',
      in_use: false,
      area: 'temp',
      delete: { status: job.status, percent: job.percent, error: job.error },
    })
  }
  return { ...prev, folders: next, folder_count: next.length }
}

function markDeleting(prev: StoragePayload | null, name: string): StoragePayload | null {
  if (!prev) return prev
  return {
    ...prev,
    folders: prev.folders.map((folder) =>
      folder.name === name
        ? { ...folder, delete: { status: 'deleting', percent: folder.delete?.percent ?? 0 } }
        : folder,
    ),
  }
}

function StorageList({
  title,
  empty,
  items,
  extra,
  onDelete,
}: {
  title: string
  empty: string
  items: StorageFolder[]
  extra?: string
  onDelete: (folder: StorageFolder) => void
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">
          {title}
        </h2>
        {extra ? <div className="text-xs text-text-secondary">{extra}</div> : null}
      </div>
      <ul className="divide-y divide-border rounded-2xl border border-border bg-surface px-4">
        {items.map((folder) => {
          const del = folder.delete
          const isDeleting = del?.status === 'deleting' || del?.status === 'done'
          const pct = Math.max(0, Math.min(100, del?.percent ?? 0))
          return (
            <li
              key={folder.path || folder.name}
              className="flex flex-wrap items-start justify-between gap-3 py-3 text-sm"
            >
              <div className="min-w-0 flex-1 space-y-0.5">
                <div className="font-mono text-sm font-semibold text-text">{folder.name}</div>
                <div className="truncate font-mono text-[11px] text-text-muted">{folder.path}</div>
                <div className="text-xs text-text-secondary">
                  {folder.size_pending ? 'Measuring…' : folder.size_label || '0 B'}
                  {folder.modified_at ? ` · ${folder.modified_at}` : ''}
                  {folder.in_use ? ' · in use' : ''}
                </div>
                {isDeleting && (
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
                {del?.status === 'error' && (
                  <div className="mt-1 text-xs text-danger-text">
                    Delete failed{del.error ? `: ${del.error}` : ''}
                  </div>
                )}
              </div>
              <button
                type="button"
                className="vd-btn vd-btn-danger text-xs"
                disabled={isDeleting}
                onClick={() => onDelete(folder)}
              >
                {isDeleting ? `${pct}%` : 'Delete'}
              </button>
            </li>
          )
        })}
        {items.length === 0 && <li className="py-6 text-text-muted">{empty}</li>}
      </ul>
    </div>
  )
}

export function StoragePage() {
  const [data, setData] = useState<StoragePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<StorageFolder | null>(null)

  const reload = async (refresh = false) => {
    try {
      const payload = await fetchStorage({ refresh })
      setData(payload)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Load failed')
    }
  }

  useEffect(() => {
    void reload()
  }, [])

  const deleting = (data?.folders || []).some((folder) => folder.delete?.status === 'deleting')
  const sizesPending = Boolean(data?.sizes_pending)
  useEffect(() => {
    if (!deleting && !sizesPending) return
    let cancelled = false
    const tick = async () => {
      try {
        if (deleting) {
          const payload = await fetchStorageDeletes()
          if (cancelled) return
          setData((prev) => applyDeletes(prev, payload.deletes))
          const still = payload.deletes.some((d) => d.status === 'deleting')
          if (!still) void reload()
        } else if (sizesPending) {
          await reload()
        }
      } catch {
        /* keep last known snapshot */
      }
    }
    const id = window.setInterval(() => void tick(), deleting ? 400 : 800)
    void tick()
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [deleting, sizesPending])

  const onDelete = () => {
    if (!pending) return
    const name = pending.name
    setPending(null)
    setData((prev) => markDeleting(prev, name))
    void (async () => {
      try {
        await deleteTempFolder(name)
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Delete failed')
        await reload()
      }
    })()
  }

  const disk = data?.disk
  const usedPct = Math.max(0, Math.min(100, disk?.used_percent ?? 0))

  return (
    <section className="space-y-5">
      <PageHeader
        kicker="Host"
        title="Storage"
        description="Temp clones under TEMP_DIR_BASE. Delete a folder to reclaim disk. Same path layout on Windows and Linux."
        actions={
          <button type="button" className="vd-btn vd-btn-secondary text-xs" onClick={() => void reload(true)}>
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
            {data.folder_count} clone folder{data.folder_count === 1 ? '' : 's'}
            {data.sizes_pending ? ' · measuring sizes…' : ` · ${data.folders_label}`}
          </div>
        </div>
      )}
      <StorageList
        title="Temp clones"
        empty="No clone folders under this path."
        items={data?.folders || []}
        onDelete={(folder) => setPending(folder)}
      />
      <ConfirmDialog
        open={Boolean(pending)}
        title="Force-delete this clone?"
        body={
          pending
            ? `Permanently delete ${pending.path}\n\nThis cannot be undone.`
            : ''
        }
        confirmLabel="Delete"
        danger
        onConfirm={onDelete}
        onCancel={() => setPending(null)}
      />
    </section>
  )
}
