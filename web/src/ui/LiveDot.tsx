export function LiveDot({ label = 'live' }: { label?: string }) {
  return (
    <span className="vd-pill bg-success-muted text-success-text">
      <span className="vd-pulse h-1.5 w-1.5 rounded-full bg-live" aria-hidden />
      {label}
    </span>
  )
}
