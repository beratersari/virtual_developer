/**
 * Run: npx tsx src/ui/lineChartHover.test.ts
 */
import { chartIndexAt, chartPointRows, type LineSeries } from './LineChart'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const series: LineSeries[] = [
  { id: 'total', label: 'Total', color: '#ff7a45', values: [4, 0] },
  { id: 'error', label: 'Error', color: '#f25c54', values: [1] },
]

const first = chartPointRows(series, 0)
assert(first.length === 2, 'every visible series is listed')
assert(first[0].label === 'Total' && first[0].value === 4, 'total is the exact count')
assert(first[1].label === 'Error' && first[1].value === 1, 'error is the exact count')

const second = chartPointRows(series, 1)
assert(second[0].value === 0, 'a real zero stays zero')
assert(second[1].value === 0, 'a missing bucket reads as zero')

assert(chartIndexAt(36, 36, 900, 31) === 0, 'left edge is the first day')
assert(chartIndexAt(936, 36, 900, 31) === 30, 'right edge is the last day')
assert(chartIndexAt(36 + 450, 36, 900, 31) === 15, 'the middle of the plot is the middle day')
assert(chartIndexAt(0, 36, 900, 31) === 0, 'a pointer left of the plot stays on the first day')
assert(chartIndexAt(2000, 36, 900, 31) === 30, 'a pointer right of the plot stays on the last day')
assert(chartIndexAt(10, 36, 900, 1) === 0, 'one bucket has nowhere else to land')

console.log('lineChartHover.test.ts ok')
