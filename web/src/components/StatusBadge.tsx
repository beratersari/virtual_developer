import { statusMeta, type StatusTone } from '../util/status'

const TONE_DOT: Record<StatusTone, string> = {
  neutral: 'bg-text-muted',
  info: 'bg-info',
  warning: 'bg-warning',
  success: 'bg-success',
  danger: 'bg-danger',
}

const TONE_TEXT: Record<StatusTone, string> = {
  neutral: 'text-text-secondary',
  info: 'text-info-text',
  warning: 'text-warning-text',
  success: 'text-success-text',
  danger: 'text-danger-text',
}

export function StatusBadge({
  status,
  size = 'md',
}: {
  status: string
  size?: 'sm' | 'md'
}) {
  const meta = statusMeta(status)
  const pad = size === 'sm' ? 'px-1.5 py-0.5 text-[10px]' : 'px-2 py-0.5 text-xs'
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border border-border bg-bg-elevated font-medium ${pad} ${TONE_TEXT[meta.tone]}`}
      title={status}
    >
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[meta.tone]}`}
        aria-hidden
      />
      {meta.label}
    </span>
  )
}
