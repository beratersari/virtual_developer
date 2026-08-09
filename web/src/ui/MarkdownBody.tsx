import type { Components } from 'react-markdown'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** OpenCode sometimes stores the whole prompt wrapped in extra quotes. */
export function unwrapStoredText(raw: string): string {
  let t = (raw || '').replace(/^\uFEFF/, '').trim()
  if (t.length >= 2) {
    const a = t[0]
    const b = t[t.length - 1]
    if ((a === '"' && b === '"') || (a === "'" && b === "'")) {
      const inner = t.slice(1, -1)
      if (/^[#>*`-]|\n/.test(inner)) t = inner
    }
  }
  if (!t.includes('\n') && t.includes('\\n')) {
    t = t.replace(/\\n/g, '\n').replace(/\\t/g, '\t')
  }
  return t
}

const components: Components = {
  a({ href, children }) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    )
  },
  code({ className, children, ...props }) {
    const block = Boolean(className)
    if (!block) {
      return (
        <code className="vd-md-code" {...props}>
          {children}
        </code>
      )
    }
    return (
      <code className={`vd-md-pre ${className || ''}`} {...props}>
        {children}
      </code>
    )
  },
}

export function MarkdownBody({
  text,
  className = '',
}: {
  text: string
  className?: string
}) {
  const source = unwrapStoredText(text)
  if (!source.trim()) return null
  return (
    <div className={`vd-md ${className}`.trim()}>
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {source}
      </Markdown>
    </div>
  )
}
