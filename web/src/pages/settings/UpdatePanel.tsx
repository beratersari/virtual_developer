import { useEffect, useState } from 'react'
import { applyUpdate, checkUpdate, fetchUpdateStatus } from '../../api/client'
import type { UpdateStatus } from '../../api/types'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Spinner } from '../../ui/Spinner'

function bytesLabel(done: number, total: number): string {
  const mb = (value: number) => `${Math.max(0, value / (1024 * 1024)).toFixed(1)} MB`
  if (total > 0) return `${mb(done)} of ${mb(total)}`
  if (done > 0) return mb(done)
  return ''
}

export function UpdatePanel({
  host,
  port,
  onChange,
  onPersisted,
}: {
  host: string
  port: number
  onChange: (host: string, port: number) => void
  onPersisted: (host: string, port: number) => void
}) {
  const [status, setStatus] = useState<UpdateStatus | null>(null)
  const [busy, setBusy] = useState<'check' | 'apply' | null>(null)
  const [ask, setAsk] = useState(false)
  const [offline, setOffline] = useState(false)
  const [localError, setLocalError] = useState('')

  useEffect(() => {
    const ac = new AbortController()
    void fetchUpdateStatus(ac.signal)
      .then((next) => {
        if (!ac.signal.aborted) setStatus(next)
      })
      .catch(() => {})
    return () => ac.abort()
  }, [])

  const phase = status?.phase || 'idle'
  useEffect(() => {
    if (phase !== 'downloading' && phase !== 'verifying' && phase !== 'restarting') return
    const timer = window.setInterval(() => {
      void fetchUpdateStatus()
        .then((next) => {
          setStatus(next)
          setOffline(false)
        })
        .catch(() => {
          if (phase === 'restarting') setOffline(true)
        })
    }, 1000)
    return () => window.clearInterval(timer)
  }, [phase])

  const portValue = port > 0 ? String(port) : ''
  const progress = status ? bytesLabel(status.bytes_done, status.bytes_total) : ''
  const message = offline
    ? 'Yaver is restarting. Open this page again in a moment.'
    : localError || status?.message || 'Set the release server address and port, then check.'
  const checkedHost = (status?.release_host || '').trim()
  const checkedPort = Number(status?.release_port) || 0
  const addressMatches =
    status != null && checkedHost === host.trim() && checkedPort === (Number(port) || 0)
  const canUpdate =
    Boolean(status?.can_apply) && addressMatches && busy == null && !offline

  const runCheck = async () => {
    setBusy('check')
    setLocalError('')
    setOffline(false)
    try {
      const next = await checkUpdate({
        release_host: host.trim(),
        release_port: Number(port) || 0,
      })
      setStatus(next)
      onPersisted(next.release_host, next.release_port)
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : 'Could not reach the release server.')
      try {
        const next = await fetchUpdateStatus()
        setStatus(next)
      } catch {
        setStatus((cur) => (cur ? { ...cur, can_apply: false } : cur))
      }
    } finally {
      setBusy(null)
    }
  }

  const runApply = async () => {
    setAsk(false)
    setBusy('apply')
    setLocalError('')
    try {
      const next = await applyUpdate({
        release_host: host.trim(),
        release_port: Number(port) || 0,
      })
      setStatus(next)
      onPersisted(next.release_host, next.release_port)
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : 'Could not start the update.')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="grid gap-3">
      <label className="field">
        <span>Release server address</span>
        <input
          value={host}
          autoComplete="off"
          spellCheck={false}
          placeholder="192.168.1.20"
          onChange={(e) => onChange(e.target.value, port)}
        />
      </label>
      <label className="field">
        <span>Release server port</span>
        <input
          value={portValue}
          inputMode="numeric"
          placeholder="8090"
          onChange={(e) => {
            const text = e.target.value.replace(/\D/g, '').slice(0, 5)
            onChange(host, text ? Number(text) : 0)
          }}
        />
      </label>
      <p className="text-xs text-text-muted">
        {status?.current_version
          ? `This install is ${status.current_version}${
              status.platform_label ? ` for ${status.platform_label}` : ''
            }.`
          : 'This install asks that address for the latest package.'}
      </p>
      <p className="text-sm text-text" role="status">
        {message}
        {progress && phase === 'downloading' ? ` ${progress}` : ''}
      </p>
      {status?.remote_notes ? (
        <p className="whitespace-pre-wrap text-xs text-text-muted">{status.remote_notes}</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className="vd-btn vd-btn-secondary text-xs"
          disabled={busy != null || phase === 'downloading' || phase === 'restarting'}
          onClick={() => void runCheck()}
        >
          {busy === 'check' ? (
            <>
              <Spinner /> Checking…
            </>
          ) : (
            'Check'
          )}
        </button>
        <button
          type="button"
          className="go text-xs"
          disabled={!canUpdate}
          onClick={() => setAsk(true)}
        >
          {phase === 'downloading' || phase === 'verifying' || busy === 'apply' ? (
            <>
              <Spinner /> Updating…
            </>
          ) : (
            'Update'
          )}
        </button>
      </div>
      <p className="text-xs text-text-muted">
        Update closes Yaver, installs the package for this computer, and opens
        Yaver again. Settings in .env stay, and the data folder stays.
      </p>
      <ConfirmDialog
        open={ask}
        title="Update Yaver"
        body={
          status?.remote_version
            ? `Yaver will close, install ${status.remote_version}, and open again. Running jobs stop. Settings in .env stay, and the data folder stays.`
            : 'Yaver will close, install the published package, and open again. Running jobs stop. Settings in .env stay.'
        }
        confirmLabel="Update"
        onConfirm={() => void runApply()}
        onCancel={() => setAsk(false)}
      />
    </div>
  )
}
