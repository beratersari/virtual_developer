import type { StorageFolder } from '../../api/types'
import { storageMrLabel } from './mrLabel'

function mrStateLabel(state?: string | null): string {
  const raw = (state || '').trim().toLowerCase()
  if (!raw) return '…'
  if (raw === 'opened' || raw === 'open') return 'open'
  if (raw === 'unknown') return 'unknown'
  return raw
}

function folderReviews(folder: StorageFolder): { url: string; state?: string | null }[] {
  const listed = (folder.merge_requests || [])
    .map((row) => ({ url: (row.url || '').trim(), state: row.state }))
    .filter((row) => row.url)
  if (listed.length) return listed
  const url = (folder.merge_request_url || '').trim()
  if (!url) return []
  return [{ url, state: folder.merge_request_state }]
}

export function ReviewLinks({ folder }: { folder: StorageFolder }) {
  const reviews = folderReviews(folder)
  if (!reviews.length) return null
  return (
    <>
      {reviews.map((review) => (
        <span key={review.url} className="inline-flex items-baseline gap-1">
          <a
            href={review.url}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-xs text-accent-text hover:underline"
          >
            {storageMrLabel(review.url)}
          </a>
          <span className="vd-tag">{mrStateLabel(review.state)}</span>
        </span>
      ))}
    </>
  )
}
