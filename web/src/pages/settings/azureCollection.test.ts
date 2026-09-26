/**
 * Run: npx tsx src/pages/settings/azureCollection.test.ts
 */
import { azureCollectionProblem } from './azureCollection'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

assert(
  azureCollectionProblem('dafsfasdf')?.includes('dafsfasdf'),
  'a bare name is not a collection URL',
)
assert(
  azureCollectionProblem('ASfdasdf')?.includes('ASfdasdf'),
  'a mixed-case bare name is not a collection URL',
)
assert(
  azureCollectionProblem('https://tfs.example.com/tfs/DefaultCollection') === null,
  'a /tfs collection URL is accepted',
)
assert(
  azureCollectionProblem('https://tfs.example.com') !== null,
  'a host with no collection name is rejected',
)

console.log('azureCollection.test.ts: ok')
