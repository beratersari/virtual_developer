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
    <div className="vd-seg">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          aria-pressed={value === t.id}
          onClick={() => onChange(t.id)}
          className={`vd-seg-btn ${value === t.id ? 'is-on' : ''}`}
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
