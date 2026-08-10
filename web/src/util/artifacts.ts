/** Decide when the Output/Prompt tabs should refetch job files. */

export function jobArtifactPathSignature(job: {
  session_log_path?: string | null
  session_log_paths?: string[] | null
  prompt_path?: string | null
  prompt_paths?: string[] | null
}): string {
  const parts = [
    ...(job.session_log_paths || []),
    job.session_log_path || '',
    ...(job.prompt_paths || []),
    job.prompt_path || '',
  ]
  return parts.filter(Boolean).join('|')
}

export function artifactsHaveContent(
  prompts?: Array<{ content?: string | null; error?: string | null }>,
  sessionLogs?: Array<{ content?: string | null; error?: string | null }>,
): boolean {
  const nonempty = (row?: { content?: string | null; error?: string | null }) =>
    Boolean((row?.content || '').trim() || (row?.error || '').trim())
  return Boolean(
    (prompts || []).some(nonempty) || (sessionLogs || []).some(nonempty),
  )
}

/**
 * Output is a file snapshot; Chat polls OpenCode separately.
 * Refetch when the job id changes, linked paths appear, the last read was
 * empty, the caller forces, or the run is still live (log still growing).
 */
export function shouldRefetchJobArtifacts(opts: {
  jobId: string
  lastJobId: string
  force?: boolean
  live?: boolean
  pathSignature: string
  lastPathSignature: string
  lastHadContent: boolean
}): boolean {
  if (!opts.jobId) return false
  if (opts.force) return true
  if (opts.lastJobId !== opts.jobId) return true
  if (opts.live) return true
  if (opts.pathSignature !== opts.lastPathSignature) return true
  if (!opts.lastHadContent && opts.pathSignature) return true
  return false
}
