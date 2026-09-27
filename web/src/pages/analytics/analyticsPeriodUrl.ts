export const ANALYTICS_PERIODS = ['24h', '7d', '30d', '90d', '1y', 'all', 'custom'] as const

export type AnalyticsPeriod = (typeof ANALYTICS_PERIODS)[number]

const DEFAULT_PERIOD: AnalyticsPeriod = '30d'

export function analyticsPeriodFromParam(section: string | undefined): AnalyticsPeriod | null {
  const raw = (section || '').trim().toLowerCase()
  if (!raw) return DEFAULT_PERIOD
  return (ANALYTICS_PERIODS as readonly string[]).includes(raw) ? (raw as AnalyticsPeriod) : null
}

export function analyticsPeriodPath(period: AnalyticsPeriod): string {
  return period === DEFAULT_PERIOD ? '/analytics' : `/analytics/${period}`
}
