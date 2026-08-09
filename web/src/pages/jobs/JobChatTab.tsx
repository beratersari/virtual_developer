import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchJobChat } from '../../api/client'
import type { ChatPart, JobChatPayload } from '../../api/types'
import { useLive } from '../../app/live'
import { MarkdownBody } from '../../ui/MarkdownBody'
import { groupChatMessages, type ChatGroup } from '../../util/chatParts'
import { formatChatTime } from '../../util/time'

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

function ThinkingBlock({ part }: { part: ChatPart }) {
  return (
    <details className="rounded border border-border bg-bg px-3 py-2">
      <summary className="cursor-pointer text-[11px] font-medium text-text-muted">Thinking</summary>
      <div className="mt-2 max-h-64 overflow-auto text-text-secondary">
        <MarkdownBody text={part.text || ''} />
      </div>
    </details>
  )
}

function MessageBubble({ group }: { group: ChatGroup }) {
  const role = group.role
  const isUser = role === 'user'
  const isAssistant = role === 'assistant'

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
            {isUser ? 'You' : isAssistant ? group.agent || 'Assistant' : role}
          </span>
          {group.created_at ? (
            <span className="font-mono text-[10px] text-text-muted">
              {formatChatTime(group.created_at) || group.created_at}
            </span>
          ) : null}
        </div>
        <div className="space-y-2">
          {group.parts.map((p, i) => {
            const key = p.id || `${group.key}-${p.type}-${i}`
            if (p.type === 'reasoning') return <ThinkingBlock key={key} part={p} />
            if (p.type === 'tool') return <ToolBlock key={key} part={p} />
            if (p.type === 'compaction') {
              return (
                <p key={key} className="text-xs italic text-warning-text">
                  Session compacted{p.auto ? ' (auto)' : ''}
                </p>
              )
            }
            if ((p.text || '').trim()) {
              return (
                <div key={key} className="max-h-[min(70vh,36rem)] overflow-auto">
                  <MarkdownBody text={p.text || ''} />
                </div>
              )
            }
            return null
          })}
        </div>
      </div>
    </div>
  )
}

const LIVE_CHAT_MS = 1500

export function JobChatTab({
  jobId,
  liveRun = false,
}: {
  jobId: string
  liveRun?: boolean
}) {
  const live = useLive()
  const [data, setData] = useState<JobChatPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [sessionFilter, setSessionFilter] = useState<string>('all')
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const stickToBottom = useRef(true)
  const loadedFor = useRef('')
  const lastSoft = useRef(0)
  const fetchGen = useRef(0)
  const wasLive = useRef(false)

  const load = (soft: boolean) => {
    const id = jobId.trim()
    if (!id) return
    const gen = ++fetchGen.current
    if (!soft) {
      setLoading(true)
      setError(null)
      setSessionFilter('all')
    }
    lastSoft.current = Date.now()
    void fetchJobChat(id)
      .then((body) => {
        if (gen !== fetchGen.current) return
        loadedFor.current = id
        setData(body)
        setError(null)
      })
      .catch((e) => {
        if (gen !== fetchGen.current) return
        if (!soft) setError(e instanceof Error ? e.message : 'Failed to load chat')
      })
      .finally(() => {
        if (gen === fetchGen.current) setLoading(false)
      })
  }

  useEffect(() => {
    loadedFor.current = ''
    stickToBottom.current = true
    load(false)
    return () => {
      fetchGen.current += 1
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- remount load per job
  }, [jobId])

  useEffect(() => {
    if (liveRun) return
    if (Date.now() - lastSoft.current < 4000) return
    load(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live.generation, liveRun, jobId])

  useEffect(() => {
    if (!liveRun) {
      if (wasLive.current) {
        wasLive.current = false
        load(true)
      }
      return
    }
    wasLive.current = true
    const timer = window.setInterval(() => load(true), LIVE_CHAT_MS)
    return () => window.clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveRun, jobId])

  const groups = useMemo(() => {
    const all = data?.messages || []
    const filtered =
      sessionFilter === 'all' ? all : all.filter((m) => m.session_id === sessionFilter)
    return groupChatMessages(filtered)
  }, [data, sessionFilter])

  useEffect(() => {
    if (!stickToBottom.current) return
    bottomRef.current?.scrollIntoView({ block: 'end' })
  }, [groups])

  if (loading && !data) {
    return <p className="text-sm text-text-muted">Loading chat…</p>
  }
  if (error && !data) {
    return <p className="text-sm text-danger-text">{error}</p>
  }
  const sessions = data?.sessions || []
  const ids = data?.session_ids || []

  if (ids.length === 0) {
    if (liveRun) {
      return (
        <p className="text-sm text-text-muted">
          <span className="mr-2 inline-flex items-center gap-1 text-live">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-live" />
            live
          </span>
          Waiting for OpenCode session id…
        </p>
      )
    }
    return (
      <div className="vd-alert vd-alert-warning">
        No OpenCode session id on this job yet. Chat appears after the agent starts and a{' '}
        <span className="font-mono">ses_*</span> id is recorded.
      </div>
    )
  }

  const sessionErrors = sessions.filter((s) => s.error)
  const total = groups.length

  return (
    <div className="space-y-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-text-muted">
          Full OpenCode transcript for this job
          {total ? ` · ${total} turn${total === 1 ? '' : 's'}` : ''}.
          {liveRun ? (
            <span className="ml-2 inline-flex items-center gap-1 text-live">
              <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-live" />
              live
            </span>
          ) : null}
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
        <div
          ref={scrollerRef}
          className="max-h-[min(78vh,52rem)] space-y-3 overflow-auto pr-1"
          onScroll={() => {
            const el = scrollerRef.current
            if (!el) return
            stickToBottom.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 96
          }}
        >
          {groups.map((group) => (
            <MessageBubble key={group.key} group={group} />
          ))}
          <div ref={bottomRef} />
        </div>
      )}
    </div>
  )
}
