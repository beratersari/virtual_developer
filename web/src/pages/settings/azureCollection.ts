/** Why a Settings collection value cannot be stored, or null when it can. */
export function azureCollectionProblem(raw: string): string | null {
  const text = raw.trim()
  if (!text) return null
  const withScheme = /^https?:\/\//i.test(text) ? text : `https://${text}`
  let parsed: URL
  try {
    parsed = new URL(withScheme)
  } catch {
    return azureCollectionMessage(text)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    return azureCollectionMessage(text)
  }
  const parts = parsed.pathname.split('/').filter(Boolean)
  if (parts.length === 0) return azureCollectionMessage(text)
  const reserved = new Set(['tfs', '_apis', '_git', '_workitems'])
  const name = parts[0].toLowerCase() === 'tfs' ? parts[1] : parts[0]
  if (!name || reserved.has(name.toLowerCase())) return azureCollectionMessage(text)
  return null
}

function azureCollectionMessage(text: string): string {
  return (
    `Azure collection "${text}" needs a collection name in the path, ` +
    'for example https://host/tfs/DefaultCollection'
  )
}
