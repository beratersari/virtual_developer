import type { GitDelivery } from '../../api/types'

export function repositoryLabel(row: GitDelivery): string {
  const explicit = (row.repository_url || '').trim().replace(/\/$/, '')
  if (explicit) {
    const name = explicit.split('/').pop() || explicit
    return name.toLowerCase().endsWith('.git') ? name.slice(0, -4) : name
  }
  const url = row.merge_request_url || row.commit_url || ''
  const match = url.match(/https?:\/\/[^/]+\/(.+?)\/-\//)
  if (!match) return 'Repository'
  const path = match[1].split('/')
  return path[path.length - 1] || match[1]
}

export function groupDeliveries(rows: GitDelivery[]): { repo: string; rows: GitDelivery[] }[] {
  const order: string[] = []
  const groups = new Map<string, GitDelivery[]>()
  for (const row of rows) {
    const repo = repositoryLabel(row)
    const list = groups.get(repo)
    if (list) list.push(row)
    else {
      groups.set(repo, [row])
      order.push(repo)
    }
  }
  return order.map((repo) => ({ repo, rows: groups.get(repo) || [] }))
}
