import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'

export type LineSeries = {
  id: string
  label: string
  color: string
  values: number[]
}

export type ChartHoverRow = {
  id: string
  label: string
  color: string
  value: number
}

/** Exact counts for one bucket. Missing series values read as 0. */
export function chartPointRows(
  series: LineSeries[],
  index: number,
): ChartHoverRow[] {
  return series.map((s) => ({
    id: s.id,
    label: s.label,
    color: s.color,
    value: Number.isFinite(s.values[index]) ? s.values[index] : 0,
  }))
}

export function LineChart({
  labels,
  series,
  height = 220,
}: {
  labels: string[]
  series: LineSeries[]
  height?: number
}) {
  const width = 720
  const padL = 36
  const padR = 12
  const padT = 12
  const padB = 36
  const innerW = width - padL - padR
  const innerH = height - padT - padB
  const max = Math.max(1, ...series.flatMap((s) => s.values))
  const n = Math.max(1, labels.length)
  const xAt = (i: number) => padL + (n <= 1 ? innerW / 2 : (i * innerW) / (n - 1))
  const yAt = (v: number) => padT + innerH - (v / max) * innerH
  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) =>
    Math.round((max * (ticks - i)) / ticks),
  )
  const labelEvery = Math.max(1, Math.ceil(n / 8))
  const wrapRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(
    null,
  )

  function showPoint(event: ReactPointerEvent, index: number) {
    const box = wrapRef.current?.getBoundingClientRect()
    if (!box) return
    setHover({
      index,
      x: event.clientX - box.left,
      y: event.clientY - box.top,
    })
  }

  const hoverRows = hover ? chartPointRows(series, hover.index) : []
  const hoverLabel = hover ? labels[hover.index] || '' : ''
  const flip =
    hover != null &&
    hover.x > (wrapRef.current?.clientWidth ?? width) * 0.62

  return (
    <div ref={wrapRef} className="relative w-full">
      <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-auto w-full"
        role="img"
        aria-label="Line chart"
        onPointerLeave={() => setHover(null)}
      >
        {yTicks.map((tick, i) => {
          const y = yAt(tick)
          return (
            <g key={`y-${i}`}>
              <line
                x1={padL}
                x2={width - padR}
                y1={y}
                y2={y}
                stroke="currentColor"
                className="text-border"
                strokeWidth="1"
              />
              <text
                x={padL - 6}
                y={y + 3}
                textAnchor="end"
                className="fill-text-muted"
                fontSize="10"
              >
                {tick}
              </text>
            </g>
          )
        })}
        {labels.map((lab, i) =>
          i % labelEvery === 0 || i === n - 1 ? (
            <text
              key={`x-${i}`}
              x={xAt(i)}
              y={height - 8}
              textAnchor="middle"
              className="fill-text-muted"
              fontSize="10"
            >
              {lab}
            </text>
          ) : null,
        )}
        {series.map((s) => {
          if (s.values.length === 0) return null
          const d = s.values
            .map((v, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i).toFixed(1)} ${yAt(v).toFixed(1)}`)
            .join(' ')
          return (
            <g key={s.id}>
              <path d={d} fill="none" stroke={s.color} strokeWidth="2.2" />
              {s.values.map((v, i) => (
                <g key={`${s.id}-${i}`}>
                  <circle
                    cx={xAt(i)}
                    cy={yAt(v)}
                    r="11"
                    fill="transparent"
                    className="cursor-pointer"
                    onPointerEnter={(event) => showPoint(event, i)}
                    onPointerMove={(event) => showPoint(event, i)}
                  >
                    <title>
                      {labels[i]}
                      {chartPointRows(series, i)
                        .map((row) => `\n${row.label}: ${row.value}`)
                        .join('')}
                    </title>
                  </circle>
                  <circle
                    cx={xAt(i)}
                    cy={yAt(v)}
                    r={hover?.index === i ? 3.6 : 2.4}
                    fill={s.color}
                    pointerEvents="none"
                  />
                </g>
              ))}
            </g>
          )
        })}
      </svg>
      </div>
      {hover && (
        <div
          className="pointer-events-none absolute z-20 min-w-[9rem] rounded-md border border-border bg-bg-elevated px-2.5 py-2 text-xs shadow-lg"
          style={{
            left: hover.x,
            top: hover.y,
            transform: flip ? 'translate(-100%, -115%)' : 'translate(10px, -115%)',
          }}
          role="tooltip"
        >
          <div className="mb-1 font-medium text-text">{hoverLabel}</div>
          <ul className="space-y-0.5">
            {hoverRows.map((row) => (
              <li key={row.id} className="flex items-center justify-between gap-3">
                <span className="inline-flex items-center gap-1.5 text-text-secondary">
                  <span
                    className="inline-block h-2 w-2 rounded-full"
                    style={{ background: row.color }}
                  />
                  {row.label}
                </span>
                <span className="font-mono tabular-nums text-text">{row.value}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
