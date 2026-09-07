import { useEffect, useRef, useState } from 'react'
import { fetchModels } from '../api/client'
import type { ModelsPayload } from '../api/types'
import { CUSTOM_MODEL, modelSelectValue, showCustomModelId } from '../util/modelPicker'
import { Spinner } from './Spinner'

export function ModelField({
  value,
  onChange,
  fallback = '',
  backend = '',
  allowEmpty = true,
  showRefresh = false,
  label = 'Model',
  onLoadingChange,
}: {
  value: string
  onChange: (v: string) => void
  fallback?: string
  backend?: string
  allowEmpty?: boolean
  showRefresh?: boolean
  label?: string
  onLoadingChange?: (loading: boolean) => void
}) {
  const [inventory, setInventory] = useState<ModelsPayload | null>(null)
  const [loadedWorker, setLoadedWorker] = useState<string | null>(null)
  const [fetching, setFetching] = useState(true)
  const [custom, setCustom] = useState(false)
  const loadGen = useRef(0)
  const worker = (backend || '').trim().toLowerCase() || 'opencode'
  const isCodex = worker === 'codex'
  const loading = fetching || loadedWorker !== worker

  const load = async (refresh: boolean) => {
    const req = ++loadGen.current
    setFetching(true)
    try {
      const inv = await fetchModels(refresh, worker)
      if (req !== loadGen.current) return
      setInventory(inv)
    } catch {
      if (req !== loadGen.current) return
      setInventory(null)
    } finally {
      if (req === loadGen.current) {
        setLoadedWorker(worker)
        setFetching(false)
      }
    }
  }

  useEffect(() => {
    void load(false)
    return () => {
      loadGen.current += 1
    }
  }, [worker])

  useEffect(() => {
    onLoadingChange?.(loading)
  }, [loading, onLoadingChange])

  useEffect(() => {
    return () => onLoadingChange?.(false)
  }, [onLoadingChange])

  const options = inventory?.models ?? []
  const known = new Set(options.map((m) => m.id))

  useEffect(() => {
    if (!inventory) return
    if (value && !known.has(value)) setCustom(true)
    else if (value && known.has(value)) setCustom(false)
  }, [value, inventory])

  const selectValue = modelSelectValue(value, known, custom)
  const showInput = showCustomModelId(selectValue)

  return (
    <div className="space-y-2">
      {showRefresh && (
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <div className="text-sm font-semibold text-text">{label}</div>
          </div>
          <button
            type="button"
            disabled={loading}
            onClick={() => void load(true)}
            className="vd-btn vd-btn-secondary shrink-0 px-2.5 py-1 text-xs"
          >
            {loading ? (
              <>
                <Spinner /> Loading…
              </>
            ) : (
              'Refresh list'
            )}
          </button>
        </div>
      )}
      <label className="field">
        <span>{showRefresh ? 'Choose' : label}</span>
        <select
          value={selectValue}
          disabled={loading}
          aria-busy={loading}
          onChange={(e) => {
            const v = e.target.value
            if (v === CUSTOM_MODEL) {
              setCustom(true)
              if (value && known.has(value)) onChange('')
              return
            }
            setCustom(false)
            onChange(v)
          }}
        >
          {allowEmpty ? (
            <option value="">
              Settings default{fallback ? ` (${fallback})` : ''}
            </option>
          ) : (
            <option value="">{loading ? 'Loading models…' : '— select a model —'}</option>
          )}
          {options.map((m) => (
            <option key={m.id} value={m.id}>
              {m.label || m.id}
            </option>
          ))}
          <option value={CUSTOM_MODEL}>Other id…</option>
        </select>
      </label>
      {showInput && (
        <label className="field">
          <span>Model id</span>
          <input
            type="text"
            autoComplete="off"
            value={value}
            disabled={loading}
            onChange={(e) => {
              setCustom(true)
              onChange(e.target.value)
            }}
            placeholder={isCodex ? 'model id Codex accepts' : 'provider/model-id'}
          />
        </label>
      )}
      <span className="mt-1 block text-xs text-text-muted">
        {loading
          ? 'Loading models…'
          : showInput
            ? isCodex
              ? 'Type any id Codex accepts.'
              : 'Type a provider/model id.'
            : isCodex
              ? allowEmpty
                ? 'This job only. List is from ~/.codex/config.toml. Choose Other id… to type a custom model.'
                : 'Ids from ~/.codex/config.toml. Choose Other id… to type a custom model.'
              : allowEmpty
                ? 'This job only. Leave default to use Settings. Choose Other id… to type a custom model.'
                : 'Inventory from OpenCode. Choose Other id… to type a custom model.'}
      </span>
      {inventory?.error && <p className="text-xs text-warning-text">{inventory.error}</p>}
    </div>
  )
}
