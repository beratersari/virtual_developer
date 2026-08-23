/**
 * Run: npx tsx src/util/worker.test.ts
 */
import type { JobItem } from '../api/types'
import { resolveJobWorker, workerLabel } from './worker'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

function job(partial: Partial<JobItem>): JobItem {
  return {
    job_id: 'job_x',
    issue_key: 'KAN-1',
    summary: 's',
    workflow_type: 'execution',
    agent: 'build',
    status: 'executing',
    progress_percentage: 0,
    live: false,
    ...partial,
  }
}

assert(resolveJobWorker(job({ backend: 'codex' })) === 'codex', 'stored codex')
assert(resolveJobWorker(job({ backend: 'opencode' })) === 'opencode', 'stored opencode')
assert(
  resolveJobWorker(job({ description: '{params}\nBackend: codex\n{params}' })) === 'codex',
  'params infer',
)
assert(
  resolveJobWorker(job({ opencode_session_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' })) ===
    'codex',
  'uuid thread',
)
assert(resolveJobWorker(job({ opencode_session_id: 'ses_abc123xyz' })) === 'opencode', 'ses_')
assert(workerLabel('codex') === 'Codex', 'label codex')
assert(workerLabel('opencode') === 'OpenCode', 'label opencode')
console.log('worker.test.ts ok')
