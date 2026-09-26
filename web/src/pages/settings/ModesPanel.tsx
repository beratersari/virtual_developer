import { useEffect, useState } from 'react'
import {
  createOpencodeAgent,
  fetchOpencodeAgent,
  fetchOpencodeAgents,
  saveOpencodeAgent,
} from '../../api/client'
import type { WorkMode } from '../../api/types'

type Props = {
  modes: WorkMode[]
  onChange: (modes: WorkMode[]) => void
}

export function ModesPanel({ modes, onChange }: Props) {
  const [agents, setAgents] = useState<string[]>([])
  const [editorName, setEditorName] = useState('')
  const [editorText, setEditorText] = useState('')
  const [newName, setNewName] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function reloadAgents() {
    const payload = await fetchOpencodeAgents()
    setAgents(payload.agents)
  }

  useEffect(() => {
    reloadAgents().catch((e: unknown) =>
      setError(e instanceof Error ? e.message : 'Could not list agents'),
    )
  }, [])

  function update(index: number, patch: Partial<WorkMode>) {
    onChange(modes.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  async function openAgent(name: string) {
    const trimmed = name.trim()
    if (!trimmed) return
    setError(null)
    setMessage(null)
    try {
      const row = await fetchOpencodeAgent(trimmed)
      setEditorName(row.name)
      setEditorText(row.text)
    } catch (e) {
      setEditorName(trimmed)
      setEditorText('')
      setError(e instanceof Error ? e.message : 'Could not read agent')
    }
  }

  async function saveText() {
    setError(null)
    setMessage(null)
    try {
      await saveOpencodeAgent(editorName, editorText)
      setMessage(`Saved ${editorName}`)
      await reloadAgents()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save agent')
    }
  }

  async function createAgent(linkIndex: number | null) {
    const name = newName.trim()
    if (!name) return
    setError(null)
    setMessage(null)
    try {
      const created = await createOpencodeAgent(name)
      setNewName('')
      setEditorName(created.name)
      setEditorText(created.text)
      await reloadAgents()
      if (linkIndex != null) update(linkIndex, { agent: created.name })
      setMessage(`Created ${created.name}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not create agent')
    }
  }

  return (
    <div className="space-y-3">
      <div className="text-sm font-semibold text-text">Modes</div>
      <p className="text-xs text-text-muted">
        Pick an agent from the list. Create one below if it is missing, then
        choose it. Plan stops without a push. Build and test push and open a
        merge request. A mode you add follows build. Write <span className="font-mono">Mode: name</span> in
        the issue params. Agent text is saved in ~/.opencode/agents and is
        separate from Save settings.
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
              {(row.agent && !agents.includes(row.agent) ? [row.agent, ...agents] : agents).map(
                (name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ),
              )}
            </select>
          </label>
          <div className="flex flex-wrap gap-2 sm:col-span-2">
            <button
              type="button"
              className="vd-btn vd-btn-secondary"
              onClick={() => openAgent(row.agent)}
            >
              Edit agent text
            </button>
            {!row.builtin && (
              <button
                type="button"
                className="vd-btn vd-btn-secondary text-danger-text"
                onClick={() => onChange(modes.filter((_, i) => i !== index))}
              >
                Remove mode
              </button>
            )}
          </div>
        </div>
      ))}
      <p>
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
      </p>
      <div className="grid gap-2 sm:grid-cols-[1fr_auto]">
        <label className="field">
          <span>New agent name</span>
          <input
            value={newName}
            placeholder="derman-docs"
            onChange={(e) => setNewName(e.target.value)}
          />
        </label>
        <div className="flex items-end">
          <button type="button" className="vd-btn vd-btn-secondary" onClick={() => createAgent(null)}>
            Create agent
          </button>
        </div>
      </div>
      {editorName && (
        <label className="field">
          <span>Agent text · {editorName}</span>
          <textarea
            className="min-h-64 font-mono text-xs"
            value={editorText}
            onChange={(e) => setEditorText(e.target.value)}
          />
          <button type="button" className="vd-btn vd-btn-primary mt-2" onClick={saveText}>
            Save agent text
          </button>
        </label>
      )}
      {message && <p className="text-xs text-text-muted">{message}</p>}
      {error && <p className="err">{error}</p>}
    </div>
  )
}
