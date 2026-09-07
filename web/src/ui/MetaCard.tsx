import type { ReactNode } from 'react'

export function MetaCard({
  label,
  value,
  valueNode,
  mono,
  className = '',
}: {
  label: string
  value?: string
  valueNode?: ReactNode
  mono?: boolean
  className?: string
}) {
  return (
    <div className={className}>
      <div className="text-[11px] text-text-muted">{label}</div>
      {valueNode ? (
        <div className="mt-0.5">{valueNode}</div>
      ) : (
        <div className={`mt-0.5 break-all text-text ${mono ? 'font-mono text-xs' : 'text-sm'}`}>
          {value}
        </div>
      )}
    </div>
  )
}
