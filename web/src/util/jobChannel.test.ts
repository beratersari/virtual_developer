/**
 * Run: npx tsx src/util/jobChannel.test.ts
 */
import { isReviewWorkflow, jobChannelLabel } from './jobChannel'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(
  jobChannelLabel({ workflow_type: 'review', source: 'gitlab' }) === 'gitlab-review',
  'gitlab review',
)
assert(
  jobChannelLabel({ workflow_type: 'review' }) === 'gitlab-review',
  'review defaults to gitlab-review',
)
assert(
  jobChannelLabel({ workflow_type: 'review', source: 'azure' }) === 'azure-review',
  'azure review',
)
assert(
  jobChannelLabel({ workflow_type: 'gitlab-review' }) === 'gitlab-review',
  'stored gitlab-review',
)
assert(
  jobChannelLabel({ workflow_type: 'azure-review' }) === 'azure-review',
  'stored azure-review',
)
assert(
  jobChannelLabel({ workflow_type: 'azure_review' }) === 'azure-review',
  'underscore alias',
)
assert(
  jobChannelLabel({ workflow_type: 'gitlab_mr', source: 'gitlab' }) === null,
  'build is not a review badge',
)
assert(isReviewWorkflow('review') === true, 'review is review')
assert(isReviewWorkflow('gitlab_mr') === false, 'gitlab_mr is not review')

console.log('jobChannel.test.ts ok')
