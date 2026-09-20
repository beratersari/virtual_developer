export type LineSeries = {
  id: string
  label: string
  color: string
  values: number[]
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

  return (
    <div className="w-full overflow-x-auto">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-auto w-full"
        role="img"
        aria-label="Line chart"
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
                <circle
                  key={`${s.id}-${i}`}
                  cx={xAt(i)}
                  cy={yAt(v)}
                  r="2.4"
                  fill={s.color}
                >
                  <title>
                    {s.label}: {v} ({labels[i]})
                  </title>
                </circle>
              ))}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
