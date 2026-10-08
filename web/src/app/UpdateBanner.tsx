import { useEffect, useId, useRef, useState } from 'react'
import type { ReleaseNotice } from '../api/types'

export function UpdateHelp({
  steps,
  onClose,
}: {
  steps: string[]
  onClose: () => void
}) {
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeRef.current?.focus()
  }, [])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div
      className="vd-modal-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="vd-modal vd-modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h2 id={titleId} className="vd-modal-title">
          How to update
        </h2>
        <ol className="vd-modal-body list-decimal space-y-2 pl-5">
          {steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
        <div className="vd-modal-actions">
          <button
            ref={closeRef}
            type="button"
            className="vd-btn vd-btn-secondary"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}

export function UpdateBanner({ notice }: { notice: ReleaseNotice | null }) {
  const [open, setOpen] = useState(false)
  const infoRef = useRef<HTMLButtonElement>(null)
  if (!notice?.available || !notice.message) return null
  const steps = notice.steps || []

  const close = () => {
    setOpen(false)
    infoRef.current?.focus()
  }

  return (
    <>
      <div className="vd-alert vd-alert-warning flex flex-wrap items-center justify-between gap-3" role="status">
        <p>{notice.message}</p>
        <button
          ref={infoRef}
          type="button"
          className="vd-btn vd-btn-secondary h-8 w-8 shrink-0 px-0 text-sm"
          aria-label="How to update"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={() => setOpen(true)}
        >
          i
        </button>
      </div>
      {open ? <UpdateHelp steps={steps} onClose={close} /> : null}
    </>
  )
}
