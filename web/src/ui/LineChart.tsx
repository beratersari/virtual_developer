import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'

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

/** Bucket under a pointer x in viewBox units. The whole column is the target. */
export function chartIndexAt(
  x: number,
  padL: number,
  innerW: number,
  n: number,
): number {
  if (n <= 1 || innerW <= 0) return 0
  const ratio = (x - padL) / innerW
  const i = Math.round(ratio * (n - 1))
  if (i < 0) return 0
  if (i > n - 1) return n - 1
  return i
}

export function LineChart({
  labels,
  series,
  height = 220,
  label = 'Line chart',
}: {
  labels: string[]
  series: LineSeries[]
  height?: number
  label?: string
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [plotWidth, setPlotWidth] = useState(720)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const apply = () => {
      const next = Math.max(280, Math.round(el.clientWidth))
      setPlotWidth((prev) => (Math.abs(prev - next) < 2 ? prev : next))
    }
    apply()
    const observer = new ResizeObserver(apply)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  // Draw in real pixels. A fixed 720×220 viewBox scaled with `h-auto`
  // made the chart half the screen tall on a wide monitor.
  const width = plotWidth
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
  const labelEvery = Math.max(
    1,
    Math.ceil(n / Math.max(4, Math.floor(width / 90))),
  )
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(
    null,
  )

  function showPoint(clientX: number, clientY: number, index: number) {
    const box = wrapRef.current?.getBoundingClientRect()
    if (!box) return
    setHover({
      index,
      x: clientX - box.left,
      y: clientY - box.top,
    })
  }

  function onPlotMove(event: ReactPointerEvent) {
    const box = wrapRef.current?.getBoundingClientRect()
    if (!box || box.width <= 0) return
    const x = ((event.clientX - box.left) / box.width) * width
    showPoint(event.clientX, event.clientY, chartIndexAt(x, padL, innerW, n))
  }

  function moveHover(delta: number) {
    const next =
      hover == null
        ? delta < 0
          ? n - 1
          : 0
        : Math.max(0, Math.min(n - 1, hover.index + delta))
    setHover({ index: next, x: xAt(next), y: padT + innerH / 2 })
  }

  const hoverRows = hover ? chartPointRows(series, hover.index) : []
  const hoverLabel = hover ? labels[hover.index] || '' : ''
  const flip =
    hover != null &&
    hover.x > (wrapRef.current?.clientWidth ?? width) * 0.62
  const tooltipBelow = hover != null && hover.y < 72

  return (
    <div ref={wrapRef} className="relative w-full">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        className="block cursor-crosshair"
        role="group"
        aria-label={label}
        tabIndex={0}
        onPointerLeave={() => setHover(null)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowRight') {
            event.preventDefault()
            moveHover(1)
          } else if (event.key === 'ArrowLeft') {
            event.preventDefault()
            moveHover(-1)
          } else if (event.key === 'Escape') {
            setHover(null)
          }
        }}
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
            </g>
          )
        })}
        {hover && (
          <g pointerEvents="none">
            <line
              x1={xAt(hover.index)}
              x2={xAt(hover.index)}
              y1={padT}
              y2={padT + innerH}
              stroke="currentColor"
              className="text-text-muted"
              strokeWidth="1"
            />
            {series.map((s) => {
              const v = s.values[hover.index]
              if (!Number.isFinite(v)) return null
              return (
                <circle
                  key={`hot-${s.id}`}
                  cx={xAt(hover.index)}
                  cy={yAt(v)}
                  r="3.5"
                  fill={s.color}
                  stroke="var(--surface)"
                  strokeWidth="1.5"
                />
              )
            })}
          </g>
        )}
        <rect
          x={padL}
          y={padT}
          width={Math.max(0, innerW)}
          height={Math.max(0, innerH)}
          fill="transparent"
          onPointerMove={onPlotMove}
        />
      </svg>
      {hover && (
        <div
          className="pointer-events-none absolute z-20 min-w-[9rem] rounded-md border border-border bg-bg-elevated px-2.5 py-2 text-xs shadow-lg"
          style={{
            left: hover.x,
            top: hover.y,
            transform: flip
              ? tooltipBelow
                ? 'translate(-100%, 12px)'
                : 'translate(-100%, -115%)'
              : tooltipBelow
                ? 'translate(10px, 12px)'
                : 'translate(10px, -115%)',
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
