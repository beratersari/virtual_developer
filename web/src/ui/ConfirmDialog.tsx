import { useEffect, useRef } from 'react'
import { Spinner } from './Spinner'

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Confirm',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  body: string
  confirmLabel?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  const snap = useRef<{
    title: string
    body: string
    confirmLabel: string
    onConfirm: () => void
  } | null>(null)
  if (open && !snap.current) {
    snap.current = { title, body, confirmLabel, onConfirm }
  }
  if (!open) {
    snap.current = null
  }
  const shown = snap.current || { title, body, confirmLabel, onConfirm }

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, busy, onCancel])

  if (!open) return null

  return (
    <div
      className="vd-modal-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onCancel()
      }}
    >
      <div className="vd-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h3 id="confirm-title" className="vd-modal-title">
          {shown.title}
        </h3>
        <p className="vd-modal-body whitespace-pre-wrap">{shown.body}</p>
        <div className="vd-modal-actions">
          <button
            type="button"
            className="vd-btn vd-btn-secondary px-3 py-1.5 text-sm"
            disabled={busy}
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className={`vd-btn px-3 py-1.5 text-sm ${danger ? 'vd-btn-danger' : 'vd-btn-primary'}`}
            disabled={busy}
            autoFocus
            onClick={shown.onConfirm}
          >
            {busy ? (
              <>
                <Spinner /> Working…
              </>
            ) : (
              shown.confirmLabel
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
