import { useEffect, useState } from 'react'
import {
  fetchModels,
  fetchSettings,
  patchSettings,
  testGitlabConnection,
  testJiraConnection,
} from '../../api/client'
import type {
  GitlabConnectionTestResult,
  GitlabHostCredentialDraft,
  JiraConnectionTestResult,
  ModelsPayload,
  SettingsPayload,
} from '../../api/types'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

type Draft = {
  jira_host: string
  jira_email: string
  jira_api_token: string
  jira_board_id: string
  poll_interval_seconds: number
  trigger_labels: string
  trigger_on_assignment: boolean
  max_concurrent_jobs: number
  agent_task_timeout_seconds: number
  agent_task_max_retries: number
  agent_task_max_incomplete_retries: number
  opencode_serve_max_compact_continues: number
  default_model: string
  gitlab_cred_rows: GitlabHostCredentialDraft[]
}

function fromSettings(s: SettingsPayload): Draft {
  return {
    jira_host: s.jira_host,
    jira_email: s.jira_email ?? '',
    jira_api_token: '',
    jira_board_id: s.jira_board_id,
    poll_interval_seconds: s.poll_interval_seconds,
    trigger_labels: s.trigger_labels,
    trigger_on_assignment: s.trigger_on_assignment,
    max_concurrent_jobs: s.max_concurrent_jobs,
    agent_task_timeout_seconds: s.agent_task_timeout_seconds,
    agent_task_max_retries: s.agent_task_max_retries ?? 3,
    agent_task_max_incomplete_retries: s.agent_task_max_incomplete_retries ?? 256,
    opencode_serve_max_compact_continues:
      s.opencode_serve_max_compact_continues ?? 256,
    default_model: s.default_model,
    gitlab_cred_rows: (s.gitlab_credentials ?? []).map((c) => ({
      host: c.host,
      pat: '',
      pat_configured: Boolean(c.pat_configured),
      original_host: c.host,
    })),
  }
}

export function SettingsPage() {
  const [settings, setSettings] = useState<SettingsPayload | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [models, setModels] = useState<ModelsPayload | null>(null)
  const [section, setSection] = useState<'jira' | 'gitlab' | 'model' | 'runtime'>('jira')
  const [jiraResult, setJiraResult] = useState<JiraConnectionTestResult | null>(null)
  const [gitlabResults, setGitlabResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [modelsLoading, setModelsLoading] = useState(false)
  const [jiraTesting, setJiraTesting] = useState(false)
  const [gitlabTestingIdx, setGitlabTestingIdx] = useState<number | null>(null)
  const [saved, setSaved] = useState(false)

  const mark = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDirty(true)
    setDraft((d) => (d ? { ...d, [key]: value } : d))
  }

  const loadModels = async (refresh: boolean) => {
    setModelsLoading(true)
    try {
      setModels(await fetchModels(refresh))
    } catch {
      /* list stays empty; operator can type an id */
    } finally {
      setModelsLoading(false)
    }
  }

  useEffect(() => {
    void fetchSettings()
      .then((s) => {
        setSettings(s)
        setDraft(fromSettings(s))
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    if (section !== 'model') return
    void loadModels(false)
  }, [section])

  const onSave = async () => {
    if (!draft) return
    setSaving(true)
    setError(null)
    try {
      if (!draft.jira_board_id.trim()) throw new Error('Board ID is required')
      for (const r of draft.gitlab_cred_rows) {
        if (r.host.trim() && !r.pat_configured && !r.pat.trim()) {
          throw new Error(`GitLab host "${r.host}" needs a PAT`)
        }
      }
      const body: Parameters<typeof patchSettings>[0] = {
        jira_host: draft.jira_host.trim(),
        jira_email: draft.jira_email.trim(),
        jira_board_id: draft.jira_board_id.trim(),
        poll_interval_seconds: Number(draft.poll_interval_seconds),
        trigger_labels: draft.trigger_labels,
        trigger_on_assignment: draft.trigger_on_assignment,
        max_concurrent_jobs: Number(draft.max_concurrent_jobs),
        agent_task_timeout_seconds: Number(draft.agent_task_timeout_seconds),
        agent_task_max_retries: Number(draft.agent_task_max_retries),
        agent_task_max_incomplete_retries: Number(draft.agent_task_max_incomplete_retries),
        opencode_serve_max_compact_continues: Number(
          draft.opencode_serve_max_compact_continues,
        ),
        default_model: draft.default_model.trim(),
        gitlab_credentials: draft.gitlab_cred_rows
          .map((r) => {
            const host = r.host.trim()
            const prev = (r.original_host || '').trim()
            const row: { host: string; pat?: string; previous_host?: string } = { host }
            if (r.pat.trim()) row.pat = r.pat.trim()
            if (prev && prev.toLowerCase() !== host.toLowerCase()) {
              row.previous_host = prev
            }
            return row
          })
          .filter((r) => r.host),
      }
      if (draft.jira_api_token.trim()) body.jira_api_token = draft.jira_api_token.trim()
      const updated = await patchSettings(body)
      setSettings(updated)
      setDraft(fromSettings(updated))
      setDirty(false)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1800)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  if (!settings || !draft) {
    return <p className="text-sm text-text-muted">{error || 'Loading settings…'}</p>
  }

  return (
    <section className="max-w-2xl space-y-5">
      <PageHeader
        kicker="Runtime"
        title="Settings"
        description="Saved here, not to .env. Leave secret fields blank to keep the current value."
      />

      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
        {(
          [
            ['jira', 'Jira'],
            ['gitlab', 'GitLab'],
            ['model', 'Model'],
            ['runtime', 'Runtime'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setSection(id)}
            className={`rounded-full px-3.5 py-1.5 text-sm font-medium transition-transform duration-150 active:scale-95 ${
              section === id ? 'bg-accent text-[#1a0d08]' : 'text-text-muted hover:text-text'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="vd-panel space-y-5 p-5">
      {error && <p className="err">{error}</p>}

      {section === 'jira' && (
      <div key="jira" className="vd-fade space-y-3">
      <div className="text-sm font-semibold text-text">Jira connection</div>
      <p className="text-xs text-text-muted">
        Host + API token / PAT. Test uses the values below; a blank token tests
        the last saved token. Cloud needs email (Basic); on-prem PAT leaves email empty.
      </p>
      <label className="field">
        <span>Host</span>
        <input value={draft.jira_host} onChange={(e) => mark('jira_host', e.target.value)} />
      </label>
      <label className="field">
        <span>
          Email {settings.jira_email_configured ? '(saved)' : '(optional — Cloud Basic)'}
        </span>
        <input
          type="email"
          autoComplete="off"
          value={draft.jira_email}
          onChange={(e) => mark('jira_email', e.target.value)}
          placeholder="Cloud: account email · on-prem: leave empty"
        />
      </label>
      <label className="field">
        <span>Board ID</span>
        <input
          inputMode="numeric"
          value={draft.jira_board_id}
          onChange={(e) => mark('jira_board_id', e.target.value)}
          placeholder="1"
        />
        <span className="text-xs text-text-muted">
          Numeric Agile id from the board URL (/jira/software/projects/…/boards/<strong>1</strong>)
        </span>
      </label>
      <label className="field">
        <span>
          API token {settings.jira_token_configured ? '(set — blank keeps it)' : '(missing)'}
        </span>
        <input
          type="password"
          value={draft.jira_api_token}
          autoComplete="new-password"
          onChange={(e) => mark('jira_api_token', e.target.value)}
        />
      </label>
      <p className="actions">
        <button
          type="button"
          disabled={jiraTesting}
          onClick={() => {
            setJiraTesting(true)
            const host = draft.jira_host.trim()
            const email = draft.jira_email.trim()
            const token = draft.jira_api_token.trim()
            // Omit blank fields so the API uses the last saved host/email/token.
            void testJiraConnection({
              ...(host ? { host } : {}),
              ...(email ? { email } : {}),
              ...(token ? { api_token: token } : {}),
            })
              .then(setJiraResult)
              .catch((e: Error) => setJiraResult({ ok: false, error: e.message }))
              .finally(() => setJiraTesting(false))
          }}
        >
          {jiraTesting ? 'Testing…' : 'Test Jira'}
        </button>
      </p>
      {jiraResult && (
        <p className={jiraResult.ok ? 'quiet' : 'err'}>
          {jiraResult.ok
            ? `${jiraResult.message || 'OK'} · ${jiraResult.auth_mode || ''}`
            : jiraResult.error}
        </p>
      )}
      {jiraResult?.ok && (jiraResult.projects || []).length > 0 && (
        <ul className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-text-secondary">
          {(jiraResult.projects || []).map((p) => (
            <li key={String(p.id ?? p.key)}>
              <span className="text-text">{p.key}</span>
              {p.name ? ` — ${p.name}` : ''}
            </li>
          ))}
        </ul>
      )}
      </div>
      )}

      {section === 'gitlab' && (
      <div key="gitlab" className="vd-fade space-y-3">
      <div className="rounded border border-border bg-bg px-4 py-3 text-sm">
        <div className="text-sm font-semibold text-text">MR comment webhook</div>
        <p className="mt-1 text-xs text-text-muted">
          Project hook (GitLab.com / CE / EE). Event: Comments only. Secret is{' '}
          <span className="font-mono">X-Gitlab-Token</span>.
        </p>
        <dl className="mt-2 grid gap-1 font-mono text-[11px] text-text-secondary">
          <div>
            Enabled:{' '}
            {settings?.gitlab_webhook_enabled === false ? 'no' : 'yes'}
          </div>
          <div>
            Mentions: {settings?.gitlab_bot_mentions || '(none)'}
          </div>
          <div>
            Secret:{' '}
            {settings?.gitlab_webhook_secret_configured ? 'configured' : 'empty (dev)'}
          </div>
          <div>
            URL: http://&lt;host&gt;:{settings?.dashboard_port ?? 8080}
            {settings?.gitlab_webhook_path || '/webhooks/gitlab'}
          </div>
        </dl>
      </div>
      <div className="text-sm font-semibold text-text">GitLab credentials</div>
      <p className="text-xs text-text-muted">
        One row per host. Empty PAT keeps the stored token (including after a host rename).
      </p>
      {draft.gitlab_cred_rows.map((row, idx) => (
        <div key={idx}>
          <label className="field">
            <span>Host {row.pat_configured ? '(PAT stored)' : ''}</span>
            <input
              value={row.host}
              onChange={(e) => {
                setDirty(true)
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.gitlab_cred_rows]
                  rows[idx] = { ...rows[idx], host: e.target.value }
                  return { ...d, gitlab_cred_rows: rows }
                })
              }}
            />
          </label>
          <label className="field">
            <span>PAT</span>
            <input
              type="password"
              value={row.pat}
              autoComplete="new-password"
              onChange={(e) => {
                setDirty(true)
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.gitlab_cred_rows]
                  rows[idx] = { ...rows[idx], pat: e.target.value }
                  return { ...d, gitlab_cred_rows: rows }
                })
              }}
            />
          </label>
          <p className="actions">
            <button
              type="button"
              disabled={gitlabTestingIdx === idx}
              onClick={() => {
                setGitlabTestingIdx(idx)
                void (async () => {
                  try {
                    const r = await testGitlabConnection({
                      host: row.host.trim(),
                      pat: row.pat.trim() || undefined,
                    })
                    setGitlabResults((m) => ({ ...m, [row.host.trim()]: r }))
                  } catch (e) {
                    setGitlabResults((m) => ({
                      ...m,
                      [row.host.trim()]: {
                        ok: false,
                        error: e instanceof Error ? e.message : 'Test failed',
                      },
                    }))
                  } finally {
                    setGitlabTestingIdx(null)
                  }
                })()
              }}
            >
              {gitlabTestingIdx === idx ? 'Testing…' : 'Test'}
            </button>
            <button
              type="button"
              className="bad"
              onClick={() => {
                setDirty(true)
                setDraft((d) =>
                  d
                    ? {
                        ...d,
                        gitlab_cred_rows: d.gitlab_cred_rows.filter((_, i) => i !== idx),
                      }
                    : d,
                )
              }}
            >
              Remove host
            </button>
          </p>
          {gitlabResults[row.host.trim()] && (
            <p className={gitlabResults[row.host.trim()].ok ? 'quiet' : 'err'}>
              {gitlabResults[row.host.trim()].message ||
                gitlabResults[row.host.trim()].error ||
                ''}
            </p>
          )}
        </div>
      ))}
      <p className="actions">
        <button
          type="button"
          onClick={() => {
            setDirty(true)
            setDraft((d) =>
              d
                ? {
                    ...d,
                    gitlab_cred_rows: [
                      ...d.gitlab_cred_rows,
                      { host: '', pat: '', pat_configured: false, original_host: '' },
                    ],
                  }
                : d,
            )
          }}
        >
          Add GitLab host
        </button>
      </p>
      </div>
      )}

      {section === 'model' && (
      <div key="model" className="vd-fade space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-text">OpenCode model</div>
          <p className="mt-1 text-xs text-text-muted">
            Inventory from GET /api/models. Saving updates runtime DEFAULT_MODEL.
          </p>
        </div>
        <button
          type="button"
          disabled={modelsLoading}
          onClick={() => void loadModels(true)}
          className="vd-btn vd-btn-secondary shrink-0 px-2.5 py-1 text-xs"
        >
          {modelsLoading ? (
            <>
              <Spinner /> Loading…
            </>
          ) : (
            'Refresh list'
          )}
        </button>
      </div>
      <label className="field">
        <span>Default model</span>
        <select
          value={draft.default_model}
          onChange={(e) => mark('default_model', e.target.value)}
        >
          <option value="">{modelsLoading ? 'Loading models…' : '— select a model —'}</option>
          {(models?.models ?? []).map((m) => (
            <option key={m.id} value={m.id}>
              {m.label || m.id}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Or type an id</span>
        <input
          value={draft.default_model}
          onChange={(e) => mark('default_model', e.target.value)}
        />
      </label>
      <div className="text-xs text-text-muted">
        Active:{' '}
        <span className="font-mono text-text-secondary">
          {draft.default_model || models?.default_model || '(unset)'}
        </span>
        {models != null && <span> · {models.models.length} from API</span>}
      </div>
      {models?.error && <p className="text-xs text-warning-text">{models.error}</p>}
      </div>
      )}

      {section === 'runtime' && (
      <div key="runtime" className="vd-fade space-y-3">
      <label className="field">
        <span>Board ID</span>
        <input
          inputMode="numeric"
          value={draft.jira_board_id}
          onChange={(e) => mark('jira_board_id', e.target.value)}
          placeholder="1"
        />
      </label>
      <label className="field">
        <span>Poll interval (seconds)</span>
        <input
          type="number"
          value={draft.poll_interval_seconds}
          onChange={(e) => mark('poll_interval_seconds', Number(e.target.value))}
        />
      </label>
      <label className="field">
        <span>Trigger labels</span>
        <input value={draft.trigger_labels} onChange={(e) => mark('trigger_labels', e.target.value)} />
      </label>
      <label className="field" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <input
          type="checkbox"
          checked={draft.trigger_on_assignment}
          onChange={(e) => mark('trigger_on_assignment', e.target.checked)}
        />
        <span style={{ margin: 0 }}>Trigger on bot assignment</span>
      </label>
      <label className="field">
        <span>Max concurrent jobs</span>
        <input
          type="number"
          value={draft.max_concurrent_jobs}
          onChange={(e) => mark('max_concurrent_jobs', Number(e.target.value))}
        />
      </label>
      <label className="field">
        <span>Agent timeout (seconds)</span>
        <input
          type="number"
          min={30}
          max={86400}
          value={draft.agent_task_timeout_seconds}
          onChange={(e) => mark('agent_task_timeout_seconds', Number(e.target.value))}
        />
      </label>
      <div className="text-sm font-semibold text-text">Retries &amp; compaction</div>
      <p className="text-xs text-text-muted">
        Context compaction is a resume, not a crash. Raise these if long jobs stop
        after compacting. Next job uses the saved values.
      </p>
      <label className="field">
        <span>Error / timeout retries</span>
        <input
          type="number"
          min={0}
          max={64}
          value={draft.agent_task_max_retries}
          onChange={(e) => mark('agent_task_max_retries', Number(e.target.value))}
        />
        <span className="text-xs text-text-muted">
          Extra attempts after a hard error or timeout (0 = no retry). Default 3.
        </span>
      </label>
      <label className="field">
        <span>Compact resume retries (CLI)</span>
        <input
          type="number"
          min={0}
          max={256}
          value={draft.agent_task_max_incomplete_retries}
          onChange={(e) =>
            mark('agent_task_max_incomplete_retries', Number(e.target.value))
          }
        />
        <span className="text-xs text-text-muted">
          Resume the same OpenCode session after compact-then-stop. Independent of
          error retries. Default 256.
        </span>
      </label>
      <label className="field">
        <span>Serve compact continues</span>
        <input
          type="number"
          min={0}
          max={256}
          value={draft.opencode_serve_max_compact_continues}
          onChange={(e) =>
            mark('opencode_serve_max_compact_continues', Number(e.target.value))
          }
        />
        <span className="text-xs text-text-muted">
          Continue prompts on the same session when OPENCODE_RUN_MODE=serve. Default
          256.
        </span>
      </label>

      <p className="quiet">
        Jira token {settings.jira_token_configured ? 'set' : 'missing'} · GitLab{' '}
        {settings.gitlab_pat_configured ? 'set' : 'missing'} · dashboard{' '}
        {settings.dashboard_host}:{settings.dashboard_port}
      </p>
      </div>
      )}

      <p>
        <button
          type="button"
          className="go"
          disabled={saving || (!dirty && !saved)}
          onClick={() => void onSave()}
        >
          {saving ? (
            <>
              <Spinner /> Saving…
            </>
          ) : saved ? (
            'Saved'
          ) : (
            'Save'
          )}
        </button>
      </p>
      </div>
    </section>
  )
}
