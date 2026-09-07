import type { JobItem, JobsPayload, TaskDetail } from '../api/types'

let jobsPayload: JobsPayload | null = null
const jobsById = new Map<string, JobItem>()
const tasksByKey = new Map<string, TaskDetail>()

export function rememberJobsPayload(payload: JobsPayload) {
  jobsPayload = payload
  for (const j of payload.jobs || []) rememberJob(j)
}

export function peekJobsPayload(): JobsPayload | null {
  return jobsPayload
}

export function rememberJob(job: JobItem) {
  if (!job?.job_id) return
  const prev = jobsById.get(job.job_id)
  // List rows used to omit description; never let a later empty field wipe one
  // we already have from a detail fetch.
  if (prev && !(job.description || '').trim() && (prev.description || '').trim()) {
    job = { ...job, description: prev.description }
  }
  jobsById.set(job.job_id, job)
}

export function peekJob(jobId: string): JobItem | null {
  return jobsById.get(jobId) || null
}

export function rememberTask(detail: TaskDetail) {
  const key = (detail.issue_key || '').trim().toUpperCase()
  if (!key) return
  tasksByKey.set(key, detail)
}

export function peekTask(issueKey: string): TaskDetail | null {
  return tasksByKey.get(issueKey.trim().toUpperCase()) || null
}

export function forgetJob(jobId: string) {
  jobsById.delete(jobId)
  if (jobsPayload) {
    jobsPayload = {
      ...jobsPayload,
      jobs: jobsPayload.jobs.filter((j) => j.job_id !== jobId),
      total: Math.max(0, (jobsPayload.total || 0) - 1),
    }
  }
}
