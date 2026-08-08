export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: string; count?: number }[]
  value: T
  onChange: (id: T) => void
}) {
  return (
    <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          onClick={() => onChange(t.id)}
          className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors transition-transform duration-150 ${
            value === t.id ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
          } active:scale-[0.97]`}
        >
          {t.label}
          {t.count != null && t.count > 0 ? (
            <span className="ml-1.5 text-[11px] opacity-80">{t.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  )
}
