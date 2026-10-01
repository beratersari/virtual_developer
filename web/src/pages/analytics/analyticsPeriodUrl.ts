export const ANALYTICS_PERIODS = ['24h', '7d', '30d', '90d', '1y', 'all', 'custom'] as const

export type AnalyticsPeriod = (typeof ANALYTICS_PERIODS)[number]

/** Labels on the Analytics period control. The document title uses the same words. */
export const ANALYTICS_PERIOD_LABELS: Record<AnalyticsPeriod, string> = {
  '24h': '24 hours',
  '7d': '7 days',
  '30d': '30 days',
  '90d': '90 days',
  '1y': '1 year',
  all: 'All',
  custom: 'Custom',
}

const DEFAULT_PERIOD: AnalyticsPeriod = '30d'

export function analyticsPeriodFromParam(section: string | undefined): AnalyticsPeriod | null {
  const raw = (section || '').trim().toLowerCase()
  if (!raw) return DEFAULT_PERIOD
  return (ANALYTICS_PERIODS as readonly string[]).includes(raw) ? (raw as AnalyticsPeriod) : null
}

export function analyticsPeriodPath(period: AnalyticsPeriod): string {
  return period === DEFAULT_PERIOD ? '/analytics' : `/analytics/${period}`
}

/** Review-list search back to the chart. Period lives in the path, not ?period=. */
export function analyticsBackHref(search: string): string {
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search)
  const periodRaw = (params.get('period') || '').trim().toLowerCase()
  const from = params.get('from') || ''
  const to = params.get('to') || ''
  const period: AnalyticsPeriod =
    from && to && (periodRaw === 'all' || periodRaw === 'custom' || periodRaw === '')
      ? 'custom'
      : (analyticsPeriodFromParam(periodRaw) ?? DEFAULT_PERIOD)
  params.delete('period')
  params.delete('state')
  params.delete('origin')
  params.delete('page')
  if (period !== 'custom') {
    params.delete('from')
    params.delete('to')
  }
  const path = analyticsPeriodPath(period)
  const query = params.toString()
  return query ? `${path}?${query}` : path
}
