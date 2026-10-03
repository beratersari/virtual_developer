/**
 * Run: npx tsx src/pages/sessions/sessionResetCopy.test.tsx
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { cloneFolder, multiRepoLabel, resetBody } from './sessionResetCopy'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const scope = 'multi:gitlab.example/group/x|gitlab.example/group/y'
const multi = resetBody({
  session_id: 'ses_aabbcc',
  kind: 'build',
  branch: 'feature/KAN-517',
  target_branch: 'main',
  scope,
  working_directory: 'C:\\vd\\t\\multi_ws',
})
assert(
  multi ===
    [
      'Multi-repo: gitlab.example/group/x, gitlab.example/group/y.',
      'Clone folder: multi_ws.',
      'The next build job that would have resumed ses_aabbcc starts a new session. Other sessions on feature/KAN-517 → main stay.',
    ].join('\n\n'),
  'multi reset copy names the session, repos, and clone',
)
assert(!multi.includes('resume implementation'), 'reset copy does not tell anyone to implement')
assert(multiRepoLabel(scope) === 'gitlab.example/group/x, gitlab.example/group/y', 'scope pipes become a repo list')
assert(multiRepoLabel('') === '', 'empty scope is one repo')
assert(cloneFolder('C:\\vd\\t\\multi_ws') === 'multi_ws', 'windows clone folder')
assert(cloneFolder('/vd/t/x') === 'x', 'posix clone folder')

const single = resetBody({
  session_id: 'ses_single',
  kind: 'build',
  branch: 'feature/KAN-517',
  target_branch: 'main',
  scope: '',
  working_directory: 'C:\\vd\\t\\clone_single',
})
assert(!single.includes('Multi-repo:'), 'one repo has no multi line')
assert(single.includes('ses_single'), 'single reset names its own session')
assert(single.includes('Clone folder: clone_single.'), 'single reset names its clone')
assert(single !== multi, 'multi and single confirms are not the same text')

const html = renderToStaticMarkup(
  <ConfirmDialog
    open
    title="Reset ses_aabbcc?"
    body={multi}
    confirmLabel="Reset session"
    danger
    onConfirm={() => {}}
    onCancel={() => {}}
  />,
)
assert(html.includes('Reset ses_aabbcc?'), 'dialog title is the session id')
assert(
  html.includes('The next build job that would have resumed ses_aabbcc starts a new session.'),
  'dialog shows the session',
)
assert(html.includes('Multi-repo: gitlab.example/group/x, gitlab.example/group/y.'), 'dialog shows the repo set')
assert(html.includes('Clone folder: multi_ws.'), 'dialog shows the clone folder')
assert(html.includes('whitespace-pre-wrap'), 'dialog keeps the blank lines')
assert(html.includes('Other sessions on feature/KAN-517 → main stay.'), 'dialog says other sessions stay')

console.log('sessionResetCopy ok')
