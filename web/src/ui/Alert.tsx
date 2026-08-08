import type { ReactNode } from 'react'

export function Alert({
  tone = 'danger',
  children,
  action,
}: {
  tone?: 'danger' | 'warning' | 'success'
  children: ReactNode
  action?: ReactNode
}) {
  return (
    <div
      role="alert"
      className={`vd-alert vd-alert-${tone} flex flex-wrap items-center justify-between gap-3`}
    >
      <div>{children}</div>
      {action}
    </div>
  )
}
