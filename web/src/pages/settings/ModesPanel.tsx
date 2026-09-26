import { useEffect, useId, useRef, useState } from 'react'
import {
  createOpencodeAgent,
  fetchOpencodeAgent,
  fetchOpencodeAgents,
  saveOpencodeAgent,
  syncOpencodeAgents,
} from '../../api/client'
import type { WorkMode } from '../../api/types'
import { ConfirmDialog } from '../../ui/ConfirmDialog'

type Props = {
  modes: WorkMode[]
  onChange: (modes: WorkMode[]) => void
}

type Editor = {
  name: string
  text: string
  loading: boolean
  saving: boolean
  error: string | null
}

export function ModesPanel({ modes, onChange }: Props) {
  const [agents, setAgents] = useState<string[]>([])
  const [synced, setSynced] = useState(true)
  const [editor, setEditor] = useState<Editor | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [editName, setEditName] = useState('')
  const [nameDraft, setNameDraft] = useState('')
  const [creating, setCreating] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [existsName, setExistsName] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const returnFocus = useRef<HTMLElement | null>(null)
  const textRef = useRef<HTMLTextAreaElement | null>(null)
  const nameRef = useRef<HTMLInputElement | null>(null)
  const titleId = useId()
  const createTitleId = useId()
  const editTitleId = useId()

  async function reloadAgents() {
    const payload = await fetchOpencodeAgents()
    setAgents(payload.agents)
    setSynced(payload.synced !== false)
  }

  useEffect(() => {
    reloadAgents().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not list agents'),
    )
  }, [])

  function closeEditor() {
    setEditor(null)
    const back = returnFocus.current
    returnFocus.current = null
    back?.focus()
  }

  useEffect(() => {
    if (!editor || editor.saving) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeEditor()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editor])

  useEffect(() => {
    if (!editor || editor.loading) return
    textRef.current?.focus()
  }, [editor?.name, editor?.loading])

  useEffect(() => {
    if (!createOpen || creating) return
    nameRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setCreateOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [createOpen, creating])

  useEffect(() => {
    if (!editOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeEdit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [editOpen])

  function update(index: number, patch: Partial<WorkMode>) {
    onChange(modes.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function rememberFocus() {
    const active = document.activeElement
    returnFocus.current = active instanceof HTMLElement ? active : null
  }

  async function openAgent(name: string, keepFocus = false) {
    const trimmed = name.trim()
    if (!trimmed) return
    if (!keepFocus) rememberFocus()
    setError(null)
    setMessage(null)
    setEditor({ name: trimmed, text: '', loading: true, saving: false, error: null })
    try {
      const row = await fetchOpencodeAgent(trimmed)
      setEditor({
        name: row.name,
        text: row.text,
        loading: false,
        saving: false,
        error: null,
      })
    } catch (e) {
      setEditor(null)
      returnFocus.current = null
      setError(e instanceof Error ? e.message : 'Could not read agent')
    }
  }

  async function saveText() {
    if (!editor || editor.loading || editor.saving) return
    setEditor({ ...editor, saving: true, error: null })
    try {
      await saveOpencodeAgent(editor.name, editor.text)
      await reloadAgents()
      setMessage(`Saved ${editor.name}`)
      closeEditor()
    } catch (e) {
      setEditor({
        ...editor,
        saving: false,
        error: e instanceof Error ? e.message : 'Could not save agent',
      })
    }
  }

  function agentChoices(current: string): string[] {
    const names = Array.isArray(agents) ? agents : []
    const selected = (current || '').trim()
    if (selected && !names.some((name) => name.toLowerCase() === selected.toLowerCase())) {
      return [selected, ...names]
    }
    return names
  }

  function listedAgent(name: string): string {
    const want = name.trim().toLowerCase()
    if (!want) return ''
    const fromList = agents.find((item) => item.toLowerCase() === want)
    if (fromList) return fromList
    const fromMode = modes.find((row) => row.agent.trim().toLowerCase() === want)
    return fromMode?.agent.trim() ?? ''
  }

  async function syncAgents() {
    if (syncing) return
    setSyncing(true)
    setError(null)
    setMessage(null)
    try {
      const result = await syncOpencodeAgents()
      setMessage(`Synced ${result.agents.length} agents to OpenCode and Claude.`)
      await reloadAgents()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not sync agents')
    } finally {
      setSyncing(false)
    }
  }

  function openEdit() {
    if (creating || syncing || agents.length === 0) return
    rememberFocus()
    setEditName(agents[0] || '')
    setError(null)
    setEditOpen(true)
  }

  function closeEdit() {
    setEditOpen(false)
    const back = returnFocus.current
    returnFocus.current = null
    back?.focus()
  }

  function openCreate() {
    if (creating || syncing) return
    rememberFocus()
    setNameDraft('')
    setError(null)
    setCreateOpen(true)
  }

  function closeCreate() {
    if (creating) return
    setCreateOpen(false)
    const back = returnFocus.current
    returnFocus.current = null
    back?.focus()
  }

  async function createAgent() {
    const name = nameDraft.trim()
    if (!name || creating) return
    const known = listedAgent(name)
    if (known) {
      setCreateOpen(false)
      setError(null)
      setExistsName(known)
      return
    }
    setCreating(true)
    setError(null)
    setMessage(null)
    try {
      const created = await createOpencodeAgent(name)
      setNameDraft('')
      setCreateOpen(false)
      await reloadAgents()
      setEditor({
        name: created.name,
        text: created.text,
        loading: false,
        saving: false,
        error: null,
      })
      setMessage(`Created ${created.name}`)
    } catch (e) {
      const text = e instanceof Error ? e.message : 'Could not create agent'
      const match = text.match(/^Agent (.+) already exists$/)
      if (match) {
        setCreateOpen(false)
        setExistsName(match[1])
      } else setError(text)
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-3">
      {synced === false && (
        <div className="vd-alert vd-alert-warning" role="status">
          Your agent files are not synced.
        </div>
      )}
      <div className="text-sm font-semibold text-text">Modes</div>
      <p className="text-xs text-text-muted">
        The list is only the agents in opencoderman/agents. Edit those files
        here. Sync copies them into the OpenCode and Claude homes, which is
        where jobs read agents. Plan stops without a push. Build and test push
        and open a merge request. A mode you add follows build. Write{' '}
        <span className="font-mono">Mode: name</span> in the issue params.
      </p>
      {modes.map((row, index) => (
        <div key={row.builtin ? `builtin-${row.name}` : `custom-${index}`} className="grid gap-2 rounded-lg border border-border p-3 sm:grid-cols-2">
          <label className="field">
            <span>Mode</span>
            <input
              value={row.name}
              disabled={row.builtin}
              spellCheck={false}
              onChange={(e) => update(index, { name: e.target.value })}
              onBlur={(e) =>
                update(index, { name: e.target.value.trim().toLowerCase() })
              }
            />
          </label>
          <label className="field">
            <span>OpenCode agent</span>
            <select
              value={row.agent}
              onChange={(e) => update(index, { agent: e.target.value })}
            >
              <option value="">Select an agent</option>
              {agentChoices(row.agent).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          {!row.builtin && (
            <div className="flex flex-wrap gap-2 sm:col-span-2">
              <button
                type="button"
                className="vd-btn vd-btn-secondary text-danger-text"
                onClick={() => onChange(modes.filter((_, i) => i !== index))}
              >
                Remove mode
              </button>
            </div>
          )}
        </div>
      ))}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          onClick={() =>
            onChange([
              ...modes,
              { name: '', behavior: 'build', agent: '', builtin: false },
            ])
          }
        >
          Add mode
        </button>
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={creating || syncing}
          onClick={openCreate}
        >
          Create agent
        </button>
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={creating || syncing || agents.length === 0}
          onClick={openEdit}
        >
          Edit agents
        </button>
        <button
          type="button"
          className="vd-btn vd-btn-secondary"
          disabled={creating || syncing}
          onClick={() => syncAgents()}
        >
          {syncing ? 'Syncing…' : 'Sync'}
        </button>
      </div>
      {editOpen && (
        <div
          className="vd-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeEdit()
          }}
        >
          <form
            className="vd-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={editTitleId}
            onSubmit={(e) => {
              e.preventDefault()
              const name = editName.trim()
              if (!name) return
              setEditOpen(false)
              void openAgent(name, true)
            }}
          >
            <h3 id={editTitleId} className="vd-modal-title">
              Edit agents
            </h3>
            <label className="field">
              <span>Agent</span>
              <select
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
              >
                {agents.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <div className="vd-modal-actions">
              <button
                type="button"
                className="vd-btn vd-btn-secondary px-3 py-1.5 text-sm"
                onClick={closeEdit}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="vd-btn vd-btn-primary px-3 py-1.5 text-sm"
                disabled={!editName.trim()}
              >
                Edit
              </button>
            </div>
          </form>
        </div>
      )}
      {message && <p className="text-xs text-text-muted">{message}</p>}
      {error && <p className="err">{error}</p>}
      {createOpen && (
        <div
          className="vd-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) closeCreate()
          }}
        >
          <form
            className="vd-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={createTitleId}
            onSubmit={(e) => {
              e.preventDefault()
              void createAgent()
            }}
          >
            <h3 id={createTitleId} className="vd-modal-title">
              Create agent
            </h3>
            <label className="field">
              <span>Agent name</span>
              <input
                ref={nameRef}
                value={nameDraft}
                placeholder="derman-docs"
                spellCheck={false}
                disabled={creating}
                onChange={(e) => setNameDraft(e.target.value)}
              />
            </label>
            {error && <p className="err">{error}</p>}
            <div className="vd-modal-actions">
              <button
                type="button"
                className="vd-btn vd-btn-secondary px-3 py-1.5 text-sm"
                disabled={creating}
                onClick={closeCreate}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="vd-btn vd-btn-primary px-3 py-1.5 text-sm"
                disabled={creating || !nameDraft.trim()}
              >
                {creating ? 'Checking…' : 'Create'}
              </button>
            </div>
          </form>
        </div>
      )}
      <ConfirmDialog
        open={existsName != null}
        title="Agent already exists"
        body={
          existsName
            ? `${existsName} is already in the list. Choose it on a mode, or use Edit agents.`
            : ''
        }
        confirmLabel="OK"
        onConfirm={() => setExistsName(null)}
        onCancel={() => setExistsName(null)}
      />
      {editor && (
        <div
          className="vd-modal-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget && !editor.saving) closeEditor()
          }}
        >
          <div
            className="vd-modal vd-modal-wide"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <h3 id={titleId} className="vd-modal-title">
              Agent text · {editor.name}
            </h3>
            <label className="field">
              <textarea
                ref={textRef}
                className="vd-agent-text"
                aria-label={`Agent text for ${editor.name}`}
                value={editor.text}
                disabled={editor.loading || editor.saving}
                autoFocus
                aria-invalid={editor.error ? true : undefined}
                onChange={(e) =>
                  setEditor({ ...editor, text: e.target.value, error: null })
                }
              />
            </label>
            {editor.loading && <p className="vd-modal-body">Loading agent text…</p>}
            {editor.error && <p className="err mt-2">{editor.error}</p>}
            <div className="vd-modal-actions">
              <button
                type="button"
                className="vd-btn vd-btn-secondary px-3 py-1.5 text-sm"
                disabled={editor.saving}
                onClick={closeEditor}
              >
                Cancel
              </button>
              <button
                type="button"
                className="vd-btn vd-btn-primary px-3 py-1.5 text-sm"
                disabled={editor.loading || editor.saving}
                onClick={() => saveText()}
              >
                {editor.saving ? 'Saving…' : 'Save agent text'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
