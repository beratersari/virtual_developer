/** Jira issue keys written in a title or description. GL- and AZ- are Yaver's own ids. */
const JIRA_KEY = /[A-Za-z][A-Za-z0-9]+-\d+/g

export type JiraTextPart = { text: string; href?: string }

export function jiraBrowseUrl(host: string, key: string): string {
  const base = host.trim().replace(/\/+$/, '')
  return `${base}/browse/${key.toUpperCase()}`
}

function projectOf(key: string): string {
  return key.slice(0, key.lastIndexOf('-')).toUpperCase()
}

/** True when this match sits inside an existing http(s) URL. */
function insideUrl(text: string, index: number): boolean {
  let start = index
  while (start > 0 && !/\s/.test(text[start - 1])) start -= 1
  return /^https?:\/\//i.test(text.slice(start, index))
}

/** Split text so each bare Jira key can become a browse link. */
export function linkJiraMentions(text: string, jiraHost: string): JiraTextPart[] {
  const host = (jiraHost || '').trim()
  if (!text) return []
  if (!host) return [{ text }]
  const parts: JiraTextPart[] = []
  const pattern = new RegExp(JIRA_KEY.source, 'g')
  let cursor = 0
  for (const match of text.matchAll(pattern)) {
    const key = match[0]
    const at = match.index ?? 0
    const project = projectOf(key)
    if (project === 'GL' || project === 'AZ') continue
    if (insideUrl(text, at)) continue
    if (at > cursor) parts.push({ text: text.slice(cursor, at) })
    parts.push({ text: key, href: jiraBrowseUrl(host, key) })
    cursor = at + key.length
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) })
  return parts.length ? parts : [{ text }]
}
