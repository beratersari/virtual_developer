import { statusMeta, type StatusTone } from '../util/status'

const TONE: Record<StatusTone, string> = {
  neutral: 'bg-surface text-text-secondary',
  info: 'bg-info-muted text-info-text',
  warning: 'bg-warning-muted text-warning-text',
  success: 'bg-success-muted text-success-text',
  danger: 'bg-danger-muted text-danger-text',
}

export function StatusBadge({
  status,
  size = 'md',
}: {
  status: string
  size?: 'sm' | 'md'
}) {
  const meta = statusMeta(status)
  const pad = size === 'sm' ? 'px-2 py-0.5 text-[11px]' : 'px-2.5 py-1 text-xs'
  return (
    <span className={`vd-pill ${pad} ${TONE[meta.tone]}`} title={status}>
      {meta.label}
    </span>
  )
}
