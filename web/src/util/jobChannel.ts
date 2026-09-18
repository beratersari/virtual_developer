/** OSM-style channel badge: gitlab-review / azure-review. */

export function isReviewWorkflow(workflowType?: string | null): boolean {
  const wf = (workflowType || '').toLowerCase().replace(/_/g, '-')
  return (
    wf === 'review' ||
    wf === 'gitlab-review' ||
    wf === 'azure-review'
  )
}

export function jobChannelLabel(job: {
  workflow_type?: string | null
  source?: string | null
}): string | null {
  if (!isReviewWorkflow(job.workflow_type)) return null
  const wf = (job.workflow_type || '').toLowerCase().replace(/_/g, '-')
  if (wf === 'azure-review') return 'azure-review'
  if (wf === 'gitlab-review') return 'gitlab-review'
  return (job.source || '').toLowerCase() === 'azure' ? 'azure-review' : 'gitlab-review'
}
