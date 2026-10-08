/**
 * Run: npx tsx --tsconfig tsconfig.app.json src/app/updateBanner.test.tsx
 */
import { renderToStaticMarkup } from 'react-dom/server'
import { UpdateBanner, UpdateHelp } from './UpdateBanner'

function assert(cond: unknown, msg: string) {
  if (!cond) throw new Error(msg)
}

const hidden = renderToStaticMarkup(
  <UpdateBanner
    notice={{
      available: false,
      message: 'Yaver 0.9.81 is available. This install is 0.9.80.',
      steps: ['In the Yaver folder, run update.bat.'],
    }}
  />,
)
assert(hidden === '', 'a current install has no banner')

const banner = renderToStaticMarkup(
  <UpdateBanner
    notice={{
      available: true,
      message: 'Yaver 0.9.81 is available. This install is 0.9.80.',
      steps: ['In the Yaver folder, run update.bat.'],
    }}
  />,
)
assert(banner.includes('Yaver 0.9.81 is available'), 'banner names the published version')
assert(banner.includes('This install is 0.9.80'), 'banner names this install')
assert(banner.includes('aria-label="How to update"'), 'info button is labeled')
assert(banner.includes('>i<'), 'info button shows i')
assert(!banner.includes('update.bat'), 'steps stay behind the info button')

const help = renderToStaticMarkup(
  <UpdateHelp
    steps={[
      'In the Yaver folder, run update.bat.',
      'When the script prints a line that begins with Updated to, start yaver.exe.',
    ]}
    onClose={() => undefined}
  />,
)
assert(help.includes('How to update'), 'dialog title')
assert(help.includes('run update.bat'), 'dialog tells the operator to run the script')
assert(help.includes('Updated to'), 'dialog tells the operator when the script is done')
assert(!help.includes('Download'), 'dialog has no download action')

console.log('updateBanner ok')
