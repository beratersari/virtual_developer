import { linkJiraMentions } from '../util/jiraMentions'

export function JiraLinkedText({
  text,
  jiraHost,
}: {
  text: string
  jiraHost?: string
}) {
  const parts = linkJiraMentions(text, jiraHost || '')
  if (parts.length === 0) return null
  return (
    <>
      {parts.map((part, index) =>
        part.href ? (
          <a
            key={`${part.href}-${index}`}
            href={part.href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent-text hover:underline"
          >
            {part.text}
          </a>
        ) : (
          <span key={`t-${index}`}>{part.text}</span>
        ),
      )}
    </>
  )
}
