export function PromptBlock({
  title,
  body,
  mono = true,
  highlight = false,
  meta,
}: {
  title: string
  body: string
  mono?: boolean
  highlight?: boolean
  meta?: string
}) {
  return (
    <div
      className={`rounded-lg border ${
        highlight ? 'border-accent/40 bg-accent-muted/30' : 'border-border bg-bg'
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-3 py-2">
        <div className="text-xs font-medium text-text-secondary">{title}</div>
        {meta ? <div className="font-mono text-[10px] text-text-muted">{meta}</div> : null}
      </div>
      <pre
        className={`max-h-[min(70vh,40rem)] overflow-auto whitespace-pre-wrap p-4 text-xs leading-relaxed text-text ${
          mono ? 'font-mono' : ''
        }`}
      >
        {body}
      </pre>
    </div>
  )
}
