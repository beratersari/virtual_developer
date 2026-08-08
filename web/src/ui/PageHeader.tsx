import type { ReactNode } from 'react'

export function PageHeader({
  kicker,
  title,
  description,
  actions,
}: {
  kicker?: string
  title: string
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        {kicker && (
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-text-muted">
            {kicker}
          </div>
        )}
        <h1 className="text-2xl font-semibold tracking-tight text-text">{title}</h1>
        {description && (
          <div className="mt-1 max-w-2xl text-sm text-text-secondary">{description}</div>
        )}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}
