/** Positive page number from the last path segment. Page 1 has no segment. */

export function listPageFromSegment(raw: string | undefined): number | null {
  const text = (raw || '').trim()
  if (!/^[1-9]\d*$/.test(text)) return null
  const n = Number(text)
  return Number.isSafeInteger(n) ? n : null
}

export function withListPage(path: string, page: number): string {
  const base = path.replace(/\/+$/, '') || '/'
  if (!page || page <= 1) return base
  return `${base}/${page}`
}
