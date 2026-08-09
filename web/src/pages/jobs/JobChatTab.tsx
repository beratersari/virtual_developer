import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchJobChat } from '../../api/client'
import type { ChatMessage, ChatPart, JobChatPayload } from '../../api/types'
import { useLive } from '../../app/live'
import { MarkdownBody } from '../../ui/MarkdownBody'

function formatToolInput(input?: Record<string, unknown>): string {
  if (!input || Object.keys(input).length === 0) return ''
  const preferred = ['command', 'filePath', 'path', 'pattern', 'query', 'content', 'name']
  const bits: string[] = []
  for (const key of preferred) {
    const v = input[key]
    if (v == null || v === '') continue
    const s = typeof v === 'string' ? v : JSON.stringify(v)
    bits.push(s.length > 240 ? `${s.slice(0, 240)}…` : s)
  }
  if (bits.length) return bits.join(' · ')
  try {
    const s = JSON.stringify(input)
    return s.length > 240 ? `${s.slice(0, 240)}…` : s
  } catch {
    return ''
  }
}

function textFromParts(parts: ChatPart[]): string {
  return parts
    .filter((p) => p.type === 'text' && (p.text || '').trim())
    .map((p) => p.text || '')
    .join('\n\n')
    .trim()
}

function ToolBlock({ part }: { part: ChatPart }) {
  const summary = formatToolInput(part.input)
  const status = (part.status || '').toLowerCase()
  const tone =
    status === 'error' || status === 'failed'
      ? 'text-danger-text'
      : status === 'completed'
        ? 'text-success-text'
        : 'text-text-muted'
  return (
    <details className="rounded border border-border bg-bg px-3 py-2">
      <summary className="cursor-pointer font-mono text-[11px] text-text-secondary">
        <span className="font-semibold text-text">{part.tool || 'tool'}</span>
        {part.status ? <span className={`ml-2 ${tone}`}>{part.status}</span> : null}
        {summary ? <span className="ml-2 text-text-muted">{summary}</span> : null}
      </summary>
      {part.output ? (
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-text-secondary">
          {part.output}
        </pre>
      ) : (
        <p className="mt-2 text-[11px] text-text-muted">No tool output stored.</p>
      )}
    </details>
  )
}

function MessageBubble({ msg }: { msg: ChatMessage }) {
  const role = (msg.role || 'unknown').toLowerCase()
  const isUser = role === 'user'
  const isAssistant = role === 'assistant'
  const text = textFromParts(msg.parts)
  const tools = msg.parts.filter((p) => p.type === 'tool')
  const reasoning = msg.parts.filter((p) => p.type === 'reasoning' && (p.text || '').trim())
  const compactions = msg.parts.filter((p) => p.type === 'compaction')
  const visible =
    Boolean(text) || tools.length > 0 || reasoning.length > 0 || compactions.length > 0
  if (!visible) return null

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[min(52rem,92%)] rounded-2xl border px-4 py-3 ${
          isUser
            ? 'border-accent/35 bg-accent-muted'
            : isAssistant
              ? 'border-border bg-surface'
              : 'border-border bg-bg'
        }`}
      >
        <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-text-muted">
            {isUser ? 'You' : isAssistant ? msg.agent || 'Assistant' : role}
          </span>
          {msg.created_at ? (
            <span className="font-mono text-[10px] text-text-muted">{msg.created_at}</span>
          ) : null}
        </div>
        {compactions.map((p) => (
          <p key={p.id || 'compact'} className="mb-2 text-xs italic text-warning-text">
            Session compacted{p.auto ? ' (auto)' : ''}
          </p>
        ))}
        {reasoning.map((p) => (
          <details key={p.id} className="mb-2">
            <summary className="cursor-pointer text-[11px] text-text-muted">Reasoning</summary>
            <div className="mt-1 max-h-48 overflow-auto">
              <MarkdownBody text={p.text || ''} />
            </div>
          </details>
        ))}
        {text ? (
          <div className="max-h-[min(70vh,36rem)] overflow-auto">
            <MarkdownBody text={text} />
          </div>
        ) : null}
        {tools.length > 0 && (
          <div className={`space-y-1.5 ${text ? 'mt-3' : ''}`}>
            {tools.map((p) => (
              <ToolBlock key={p.id || `${p.tool}-${p.call_id}`} part={p} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export function JobChatTab({ jobId }: { jobId: string }) {
  const live = useLive()
  const [data, setData] = useState<JobChatPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [sessionFilter, setSessionFilter] = useState<string>('all')
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const loadedFor = useRef('')
  const lastSoft = useRef(0)

  useEffect(() => {
    let cancelled = false
    const id = jobId.trim()
    if (!id) return
    const soft = loadedFor.current === id
    if (soft && Date.now() - lastSoft.current < 4000) return
    if (!soft) {
      setLoading(true)
      setError(null)
      setSessionFilter('all')
    }
    lastSoft.current = Date.now()
    void fetchJobChat(id)
      .then((body) => {
        if (cancelled) return
        loadedFor.current = id
        setData(body)
        setError(null)
      })
      .catch((e) => {
        if (cancelled) return
        if (!soft) setError(e instanceof Error ? e.message : 'Failed to load chat')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [jobId, live.generation])

  const messages = useMemo(() => {
    const all = data?.messages || []
    if (sessionFilter === 'all') return all
    return all.filter((m) => m.session_id === sessionFilter)
  }, [data, sessionFilter])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, sessionFilter])

  if (loading && !data) {
    return <p className="text-sm text-text-muted">Loading chat…</p>
  }
  if (error && !data) {
    return <p className="text-sm text-danger-text">{error}</p>
  }
  const sessions = data?.sessions || []
  const ids = data?.session_ids || []

  if (ids.length === 0) {
    return (
      <div className="vd-alert vd-alert-warning">
        No OpenCode session id on this job yet. Chat appears after the agent starts and a{' '}
        <span className="font-mono">ses_*</span> id is recorded.
      </div>
    )
  }

  const sessionErrors = sessions.filter((s) => s.error)
  const total = messages.length

  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-text-muted">
          Full OpenCode transcript for this job
          {total ? ` · ${total} message${total === 1 ? '' : 's'}` : ''}.
        </p>
        {sessions.length > 1 && (
          <label className="text-xs text-text-muted">
            Session
            <select
              className="vd-input ml-2 w-auto font-mono text-xs"
              value={sessionFilter}
              onChange={(e) => setSessionFilter(e.target.value)}
            >
              <option value="all">All ({data?.messages.length || 0})</option>
              {sessions.map((s) => (
                <option key={s.session_id} value={s.session_id}>
                  {s.session_id}
                  {s.message_count ? ` · ${s.message_count}` : ''}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {sessionErrors.map((s) => (
        <p key={s.session_id} className="text-xs text-warning-text">
          {s.session_id}: {s.error}
        </p>
      ))}

      {total === 0 ? (
        <p className="text-text-muted">
          Session id is recorded, but no messages were found in the local OpenCode database.
        </p>
      ) : (
        <div className="max-h-[min(78vh,52rem)] space-y-3 overflow-auto pr-1">
          {messages.map((msg) => (
            <MessageBubble key={msg.id || `${msg.session_id}-${msg.created_at}`} msg={msg} />
          ))}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  )
}
