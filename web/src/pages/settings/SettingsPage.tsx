import { useEffect, useRef, useState } from 'react'
import {
  fetchSettings,
  patchSettings,
  testAzureConnection,
  testGitlabConnection,
  testJiraConnection,
} from '../../api/client'
import type {
  GitlabConnectionTestResult,
  GitlabHostCredentialDraft,
  JiraConnectionTestResult,
  ProjectRepository,
  SettingsPayload,
} from '../../api/types'
import { useLive } from '../../app/live'
import { ModelField } from '../../ui/ModelField'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

type Draft = {
  jira_host: string
  jira_api_token: string
  jira_board_id: string
  jira_projects: string
  poll_interval_seconds: number
  jira_trigger_user: string
  jira_trigger_label: string
  gitlab_trigger_user: string
  azure_trigger_user: string
  gitlab_webhook_enabled: boolean
  gitlab_webhook_secret: string
  azure_webhook_enabled: boolean
  max_concurrent_jobs: number
  agent_task_timeout_seconds: number
  agent_task_max_retries: number
  agent_task_max_incomplete_retries: number
  default_model: string
  agent_backend: string
  gitlab_cred_rows: GitlabHostCredentialDraft[]
  azure_cred_rows: GitlabHostCredentialDraft[]
  project_repositories: ProjectRepository[]
}

function fromSettings(s: SettingsPayload): Draft {
  return {
    jira_host: s.jira_host,
    jira_api_token: '',
    jira_board_id: s.jira_board_id,
    jira_projects: s.jira_projects ?? '',
    poll_interval_seconds: s.poll_interval_seconds,
    jira_trigger_user: s.jira_trigger_user ?? s.trigger_assignee_names ?? '',
    jira_trigger_label: s.jira_trigger_label ?? s.trigger_labels ?? '',
    gitlab_trigger_user: s.gitlab_trigger_user ?? s.gitlab_bot_mentions ?? '',
    azure_trigger_user: s.azure_trigger_user ?? s.azure_bot_mentions ?? '',
    gitlab_webhook_enabled: s.gitlab_webhook_enabled !== false,
    gitlab_webhook_secret: '',
    azure_webhook_enabled: s.azure_webhook_enabled === true,
    max_concurrent_jobs: s.max_concurrent_jobs,
    agent_task_timeout_seconds: s.agent_task_timeout_seconds,
    agent_task_max_retries: s.agent_task_max_retries ?? 3,
    agent_task_max_incomplete_retries: s.agent_task_max_incomplete_retries ?? 256,
    default_model: s.default_model,
    agent_backend: s.agent_backend || 'opencode',
    gitlab_cred_rows: (s.gitlab_credentials ?? []).map((c) => ({
      host: c.host,
      pat: '',
      pat_configured: Boolean(c.pat_configured),
      original_host: c.host,
    })),
    azure_cred_rows: (s.azure_credentials ?? []).map((c) => ({
      host: c.host,
      pat: '',
      pat_configured: Boolean(c.pat_configured),
      original_host: c.host,
    })),
    project_repositories: (s.project_repositories ?? []).map((p) => ({
      label: p.label || '',
      url: p.url || '',
      target_branch: p.target_branch || '',
      source_branch: p.source_branch || '',
    })),
  }
}

export function SettingsPage() {
  const live = useLive()
  const [settings, setSettings] = useState<SettingsPayload | null>(
    () => live.settings,
  )
  const [draft, setDraft] = useState<Draft | null>(() =>
    live.settings ? fromSettings(live.settings) : null,
  )
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [section, setSection] = useState<
    'jira' | 'gitlab' | 'azure' | 'projects' | 'model' | 'runtime'
  >('jira')
  const [jiraResult, setJiraResult] = useState<JiraConnectionTestResult | null>(null)
  const [gitlabResults, setGitlabResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [jiraTesting, setJiraTesting] = useState(false)
  const [gitlabTestingIdx, setGitlabTestingIdx] = useState<number | null>(null)
  const [azureResults, setAzureResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [azureTestingIdx, setAzureTestingIdx] = useState<number | null>(null)
  const [saved, setSaved] = useState(false)
  const [modelsLoading, setModelsLoading] = useState(false)
  const [dirtyKeys, setDirtyKeys] = useState<Set<keyof Draft>>(new Set())
  const dirtyRef = useRef(false)
  dirtyRef.current = dirty

  const touch = (key: keyof Draft) => {
    setDirty(true)
    setDirtyKeys((prev) => {
      const next = new Set(prev)
      next.add(key)
      return next
    })
  }

  const mark = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    touch(key)
    setDraft((d) => (d ? { ...d, [key]: value } : d))
  }

  useEffect(() => {
    if (settings || !live.settings) return
    setSettings(live.settings)
    setDraft(fromSettings(live.settings))
  }, [live.settings, settings])

  useEffect(() => {
    void fetchSettings()
      .then((s) => {
        setSettings(s)
        if (!dirtyRef.current) setDraft(fromSettings(s))
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const onSave = async () => {
    if (!draft || modelsLoading) return
    setSaving(true)
    setError(null)
    try {
      if (!draft.jira_board_id.trim()) throw new Error('Board ID is required')
      for (const r of draft.gitlab_cred_rows) {
        if (r.host.trim() && !r.pat_configured && !r.pat.trim()) {
          throw new Error(`GitLab host "${r.host}" needs a PAT`)
        }
      }
      for (const r of draft.azure_cred_rows) {
        if (r.host.trim() && !r.pat_configured && !r.pat.trim()) {
          throw new Error(`Azure DevOps host "${r.host}" needs a PAT`)
        }
      }
      const body: Parameters<typeof patchSettings>[0] = {}
      if (dirtyKeys.has('jira_host')) body.jira_host = draft.jira_host.trim()
      if (dirtyKeys.has('jira_board_id')) body.jira_board_id = draft.jira_board_id.trim()
      if (dirtyKeys.has('jira_projects')) body.jira_projects = draft.jira_projects.trim()
      if (dirtyKeys.has('poll_interval_seconds')) {
        body.poll_interval_seconds = Number(draft.poll_interval_seconds)
      }
      if (dirtyKeys.has('jira_trigger_user')) {
        body.jira_trigger_user = draft.jira_trigger_user
      }
      if (dirtyKeys.has('jira_trigger_label')) {
        body.jira_trigger_label = draft.jira_trigger_label
      }
      if (dirtyKeys.has('gitlab_trigger_user')) {
        body.gitlab_trigger_user = draft.gitlab_trigger_user
      }
      if (dirtyKeys.has('azure_trigger_user')) {
        body.azure_trigger_user = draft.azure_trigger_user
      }
      if (dirtyKeys.has('gitlab_webhook_enabled')) {
        body.gitlab_webhook_enabled = draft.gitlab_webhook_enabled
      }
      if (dirtyKeys.has('azure_webhook_enabled')) {
        body.azure_webhook_enabled = draft.azure_webhook_enabled
      }
      if (dirtyKeys.has('gitlab_webhook_secret') && draft.gitlab_webhook_secret.trim()) {
        body.gitlab_webhook_secret = draft.gitlab_webhook_secret.trim()
      }

      if (dirtyKeys.has('max_concurrent_jobs')) {
        body.max_concurrent_jobs = Number(draft.max_concurrent_jobs)
      }
      if (dirtyKeys.has('agent_task_timeout_seconds')) {
        body.agent_task_timeout_seconds = Number(draft.agent_task_timeout_seconds)
      }
      if (dirtyKeys.has('agent_task_max_retries')) {
        body.agent_task_max_retries = Number(draft.agent_task_max_retries)
      }
      if (dirtyKeys.has('agent_task_max_incomplete_retries')) {
        body.agent_task_max_incomplete_retries = Number(
          draft.agent_task_max_incomplete_retries,
        )
      }
      if (dirtyKeys.has('default_model')) body.default_model = draft.default_model.trim()
      if (dirtyKeys.has('agent_backend')) body.agent_backend = draft.agent_backend
      if (dirtyKeys.has('gitlab_cred_rows')) {
        body.gitlab_credentials = draft.gitlab_cred_rows
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
          .filter((r) => r.host)
      }
      if (dirtyKeys.has('azure_cred_rows')) {
        body.azure_credentials = draft.azure_cred_rows
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
          .filter((r) => r.host)
      }
      if (dirtyKeys.has('project_repositories')) {
        body.project_repositories = draft.project_repositories
          .map((p) => ({
            label: p.label.trim(),
            url: p.url.trim(),
            target_branch: (p.target_branch || '').trim(),
            source_branch: (p.source_branch || '').trim(),
          }))
          .filter((p) => p.url)
      }
      if (dirtyKeys.has('jira_api_token') && draft.jira_api_token.trim()) {
        body.jira_api_token = draft.jira_api_token.trim()
      }
      const updated = await patchSettings(body)
      setSettings(updated)
      setDraft(fromSettings(updated))
      setDirty(false)
      setDirtyKeys(new Set())
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
        kicker="Configuration"
        title="Settings"
        description="Save writes only the fields you changed. After restart, a .env key is used unless you later save that same field here. Leave secret fields blank to keep the current value."
      />

      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
        {(
          [
            ['jira', 'Jira'],
            ['gitlab', 'GitLab'],
            ['azure', 'Azure'],
            ['projects', 'Projects'],
            ['model', 'Agent'],
            ['runtime', 'Runtime'],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => {
              if (id === 'model' && section !== 'model') setModelsLoading(true)
              setSection(id)
            }}
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
      <div className="text-sm font-semibold text-text">Connection</div>
      <p className="text-xs text-text-muted">
        Site URL and API token. A blank token keeps the saved value.
      </p>
      <label className="field">
        <span>Host</span>
        <input value={draft.jira_host} onChange={(e) => mark('jira_host', e.target.value)} />
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
            const token = draft.jira_api_token.trim()
            void testJiraConnection({
              ...(host ? { host } : {}),
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

      <div className="text-sm font-semibold text-text">Board</div>
      <label className="field">
        <span>Board ID</span>
        <input
          inputMode="numeric"
          value={draft.jira_board_id}
          onChange={(e) => mark('jira_board_id', e.target.value)}
          placeholder="1"
        />
        <span className="text-xs text-text-muted">
          Numeric Agile board id from the board URL.
        </span>
      </label>
      <label className="field">
        <span>Project keys (JIRA_PROJECTS)</span>
        <input
          value={draft.jira_projects}
          onChange={(e) => mark('jira_projects', e.target.value)}
          placeholder="KAN, PLATFORM"
        />
        <span className="text-xs text-text-muted">
          Comma-separated Jira keys used to bind GitLab MR and Azure PR titles
          (feat(KAN-12): …) to a ticket. Empty falls back to PROJ.
        </span>
      </label>
      <label className="field">
        <span>Poll interval (seconds)</span>
        <input
          type="number"
          value={draft.poll_interval_seconds}
          onChange={(e) => mark('poll_interval_seconds', Number(e.target.value))}
        />
        <span className="text-xs text-text-muted">
          How often the poller reads the board.
        </span>
      </label>

      <div className="text-sm font-semibold text-text">Intake</div>
      <label className="field">
        <span>Trigger user (JIRA_TRIGGER_USER)</span>
        <input
          value={draft.jira_trigger_user}
          onChange={(e) => mark('jira_trigger_user', e.target.value)}
          placeholder="Beratersari, jira ai bot"
        />
        <span className="text-xs text-text-muted">
          Jira display name or username, no @. To Do issues assigned to this
          name are accepted. Comments that mention the same name tag the bot.
          Comma-separated if there is more than one.
        </span>
      </label>
      <label className="field">
        <span>Trigger label (JIRA_TRIGGER_LABEL)</span>
        <input
          value={draft.jira_trigger_label}
          onChange={(e) => mark('jira_trigger_label', e.target.value)}
          placeholder="bot, ai-assist"
        />
        <span className="text-xs text-text-muted">
          Optional. When set, To Do intake needs the trigger user AND one of
          these labels. Leave empty to accept any To Do ticket assigned to the
          bot. Comma-separated if there is more than one.
        </span>
      </label>
      <p className="text-xs text-text-muted">
        Optional later: set JIRA_EMAIL in .env for Cloud HTTP Basic. Daily
        use is host + token (Bearer).
      </p>
      </div>
      )}

      {section === 'gitlab' && (
      <div key="gitlab" className="vd-fade space-y-3">
      <div className="text-sm font-semibold text-text">Credentials</div>
      <p className="text-xs text-text-muted">
        One personal access token per GitLab host. A host with a PAT is
        allowed — there is no separate host list. Leave PAT blank to keep
        the stored token.
      </p>
      {draft.gitlab_cred_rows.map((row, idx) => (
        <div key={idx}>
          <label className="field">
            <span>Host {row.pat_configured ? '(PAT stored)' : ''}</span>
            <input
              value={row.host}
              onChange={(e) => {
                touch('gitlab_cred_rows')
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
                touch('gitlab_cred_rows')
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
                touch('gitlab_cred_rows')
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
            touch('gitlab_cred_rows')
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

      <div className="text-sm font-semibold text-text">Trigger username</div>
      <label className="field">
        <span>Trigger user (GITLAB_TRIGGER_USER)</span>
        <input
          value={draft.gitlab_trigger_user}
          onChange={(e) => mark('gitlab_trigger_user', e.target.value)}
          placeholder="berat_ai, yaver"
        />
        <span className="text-xs text-text-muted">
          GitLab username, no @. Start a job with @name /yaver on a
          merge-request comment. Mention without /yaver gets a usage note
          in the thread. Comments from this user are ignored. Comma-separated
          if there is more than one.
        </span>
      </label>

      <div className="rounded border border-border bg-bg px-4 py-3 text-sm">
        <div className="text-sm font-semibold text-text">Project webhook</div>
        <p className="mt-1 text-xs text-text-muted">
          Register a project hook for comments and merge-request events. Merged
          or closed merge requests delete the matching temp clone. The secret
          is sent as X-Gitlab-Token.
        </p>
        <label className="field mt-2">
          <span>Enabled</span>
          <input
            type="checkbox"
            checked={draft.gitlab_webhook_enabled}
            onChange={(e) => mark('gitlab_webhook_enabled', e.target.checked)}
          />
        </label>
        <label className="field">
          <span>
            Secret{' '}
            {settings?.gitlab_webhook_secret_configured ? '(stored)' : ''}
          </span>
          <input
            type="password"
            value={draft.gitlab_webhook_secret}
            autoComplete="new-password"
            onChange={(e) => mark('gitlab_webhook_secret', e.target.value)}
            placeholder="leave blank to keep current"
          />
        </label>
        <p className="mt-2 font-mono text-[11px] text-text-secondary">
          URL: http://&lt;host&gt;:{settings?.dashboard_port ?? 8080}
          {settings?.gitlab_webhook_path || '/yaver/webhook/gitlab'}
        </p>
      </div>
      </div>
      )}

      {section === 'azure' && (
      <div key="azure" className="vd-fade space-y-3">
      <div className="text-sm font-semibold text-text">Credentials</div>
      <p className="text-xs text-text-muted">
        One personal access token per Azure DevOps Server host. Auth is
        Basic pat:PAT — username is sent as pat automatically. Clone,
        push, and PR use this PAT. Leave PAT blank to keep the stored
        token. Host can be tfs.example.com or tfs.example.com/tfs.
        Test authenticates at /tfs/_apis/connectionData (Creasy 0.9.1),
        not /tfs/YourCollection.
      </p>
      {draft.azure_cred_rows.map((row, idx) => (
        <div key={idx}>
          <label className="field">
            <span>Host {row.pat_configured ? '(PAT stored)' : ''}</span>
            <input
              value={row.host}
              onChange={(e) => {
                touch('azure_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.azure_cred_rows]
                  rows[idx] = { ...rows[idx], host: e.target.value }
                  return { ...d, azure_cred_rows: rows }
                })
              }}
              placeholder="tfs.example.com"
            />
          </label>
          <label className="field">
            <span>PAT</span>
            <input
              type="password"
              value={row.pat}
              autoComplete="new-password"
              onChange={(e) => {
                touch('azure_cred_rows')
                setDraft((d) => {
                  if (!d) return d
                  const rows = [...d.azure_cred_rows]
                  rows[idx] = { ...rows[idx], pat: e.target.value }
                  return { ...d, azure_cred_rows: rows }
                })
              }}
            />
          </label>
          <p className="actions">
            <button
              type="button"
              disabled={azureTestingIdx === idx}
              onClick={() => {
                setAzureTestingIdx(idx)
                void (async () => {
                  try {
                    const r = await testAzureConnection({
                      host: row.host.trim(),
                      pat: row.pat.trim() || undefined,
                    })
                    setAzureResults((m) => ({ ...m, [row.host.trim()]: r }))
                  } catch (e) {
                    setAzureResults((m) => ({
                      ...m,
                      [row.host.trim()]: {
                        ok: false,
                        error: e instanceof Error ? e.message : 'Test failed',
                      },
                    }))
                  } finally {
                    setAzureTestingIdx(null)
                  }
                })()
              }}
            >
              {azureTestingIdx === idx ? 'Testing…' : 'Test'}
            </button>
            <button
              type="button"
              className="bad"
              onClick={() => {
                touch('azure_cred_rows')
                setDraft((d) =>
                  d
                    ? {
                        ...d,
                        azure_cred_rows: d.azure_cred_rows.filter((_, i) => i !== idx),
                      }
                    : d,
                )
              }}
            >
              Remove host
            </button>
          </p>
          {azureResults[row.host.trim()] && (
            <p className={azureResults[row.host.trim()].ok ? 'quiet' : 'err'}>
              {azureResults[row.host.trim()].message ||
                azureResults[row.host.trim()].error ||
                ''}
            </p>
          )}
        </div>
      ))}
      <p className="actions">
        <button
          type="button"
          onClick={() => {
            touch('azure_cred_rows')
            setDraft((d) =>
              d
                ? {
                    ...d,
                    azure_cred_rows: [
                      ...d.azure_cred_rows,
                      { host: '', pat: '', pat_configured: false, original_host: '' },
                    ],
                  }
                : d,
            )
          }}
        >
          Add Azure host
        </button>
      </p>

      <div className="text-sm font-semibold text-text">Trigger username</div>
      <label className="field">
        <span>Trigger user (AZURE_TRIGGER_USER)</span>
        <input
          value={draft.azure_trigger_user}
          onChange={(e) => mark('azure_trigger_user', e.target.value)}
          placeholder="yaver, Yaver Bot"
        />
        <span className="text-xs text-text-muted">
          Azure DevOps display name or unique name, no @. Start a job with
          @name /yaver on a pull-request comment. Mention without /yaver
          gets a usage note in the thread. Comments from this user are
          ignored. Comma-separated if there is more than one.
        </span>
      </label>

      <div className="rounded border border-border bg-bg px-4 py-3 text-sm">
        <div className="text-sm font-semibold text-text">Service hook</div>
        <p className="mt-1 text-xs text-text-muted">
          Register a project Web Hook for pull-request commented and
          pull-request updated / merged / abandoned. Completed or abandoned
          pull requests delete the matching temp clone. No webhook secret.
        </p>
        <label className="field mt-2">
          <span>Enabled</span>
          <input
            type="checkbox"
            checked={draft.azure_webhook_enabled}
            onChange={(e) => mark('azure_webhook_enabled', e.target.checked)}
          />
        </label>
        <p className="mt-2 font-mono text-[11px] text-text-secondary">
          URL: http://&lt;host&gt;:{settings?.dashboard_port ?? 8080}
          {settings?.azure_webhook_path || '/yaver/webhook/azure'}
        </p>
      </div>
      </div>
      )}

      {section === 'projects' && (
      <div key="projects" className="vd-fade space-y-3">
        <div>
          <div className="text-sm font-semibold text-text">Saved projects</div>
          <p className="mt-1 text-xs text-text-muted">
            Named remotes for Scheduled → New issue.
          </p>
        </div>
        {draft.project_repositories.map((row, idx) => (
          <div key={idx} className="space-y-2 rounded-lg border border-border p-3">
            <label className="field">
              <span>Label</span>
              <input
                value={row.label}
                placeholder="demo"
                onChange={(e) => {
                  const label = e.target.value
                  touch('project_repositories')
                  setDraft((d) => {
                    if (!d) return d
                    const next = d.project_repositories.slice()
                    next[idx] = { ...next[idx], label }
                    return { ...d, project_repositories: next }
                  })
                }}
              />
            </label>
            <label className="field">
              <span>Git URL</span>
              <input
                value={row.url}
                placeholder="https://gitlab.com/group/repo.git"
                onChange={(e) => {
                  const url = e.target.value
                  touch('project_repositories')
                  setDraft((d) => {
                    if (!d) return d
                    const next = d.project_repositories.slice()
                    next[idx] = { ...next[idx], url }
                    return { ...d, project_repositories: next }
                  })
                }}
              />
            </label>
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="field">
                <span>Default target</span>
                <input
                  value={row.target_branch || ''}
                  placeholder="develop"
                  onChange={(e) => {
                    const target_branch = e.target.value
                    touch('project_repositories')
                    setDraft((d) => {
                      if (!d) return d
                      const next = d.project_repositories.slice()
                      next[idx] = { ...next[idx], target_branch }
                      return { ...d, project_repositories: next }
                    })
                  }}
                />
              </label>
              <label className="field">
                <span>Default source</span>
                <input
                  value={row.source_branch || ''}
                  placeholder="optional"
                  onChange={(e) => {
                    const source_branch = e.target.value
                    touch('project_repositories')
                    setDraft((d) => {
                      if (!d) return d
                      const next = d.project_repositories.slice()
                      next[idx] = { ...next[idx], source_branch }
                      return { ...d, project_repositories: next }
                    })
                  }}
                />
              </label>
            </div>
            <p className="actions">
              <button
                type="button"
                className="vd-btn-ghost text-danger-text"
                onClick={() => {
                  touch('project_repositories')
                  setDraft((d) =>
                    d
                      ? {
                          ...d,
                          project_repositories: d.project_repositories.filter(
                            (_, i) => i !== idx,
                          ),
                        }
                      : d,
                  )
                }}
              >
                Remove
              </button>
            </p>
          </div>
        ))}
        <p className="actions">
          <button
            type="button"
            onClick={() => {
              touch('project_repositories')
              setDraft((d) =>
                d
                  ? {
                      ...d,
                      project_repositories: [
                        ...d.project_repositories,
                        { label: '', url: '', target_branch: 'develop', source_branch: '' },
                      ],
                    }
                  : d,
              )
            }}
          >
            Add project
          </button>
        </p>
      </div>
      )}

      {section === 'model' && (
      <div key="model" className="vd-fade space-y-3">
      <label className="field">
        <span>Worker</span>
        <select
          value={draft.agent_backend}
          onChange={(e) => {
            setModelsLoading(true)
            mark('agent_backend', e.target.value)
          }}
        >
          <option value="opencode">OpenCode</option>
          <option value="codex">Codex</option>
        </select>
        <span className="mt-1 block text-xs text-text-muted">
          Default worker for new jobs. An issue {'{params}'} Backend field
          overrides this. Provider credentials stay in each tool&apos;s own
          config (OpenCode: opencode.json · Codex: ~/.codex/config.toml).
        </span>
      </label>
      <p className="text-xs text-text-muted">
        Default model for new jobs. The list follows the selected worker.
      </p>
      <ModelField
        label="Default model"
        value={draft.default_model}
        onChange={(v) => mark('default_model', v)}
        backend={draft.agent_backend}
        allowEmpty={false}
        showRefresh
        onLoadingChange={setModelsLoading}
      />
      </div>
      )}

      {section === 'runtime' && (
      <div key="runtime" className="vd-fade space-y-3">
      <div className="text-sm font-semibold text-text">Jobs</div>
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
        <span className="text-xs text-text-muted">
          Seconds allowed for one agent attempt. A saved change applies
          immediately, including a running job.
        </span>
      </label>

      <div className="text-sm font-semibold text-text">Retries</div>
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
          Maximum extra attempts after a timeout or error. 0 means no retry.
        </span>
      </label>
      <label className="field">
        <span>Incomplete-session retries</span>
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
          Extra attempts when a session ends incomplete. Compaction on the
          same session is not counted against this limit.
        </span>
      </label>

      <div className="text-sm font-semibold text-text">Data locations</div>
      <p className="text-xs text-text-muted">
        Change <span className="font-mono">YAVER_DATA_DIR</span> and{' '}
        <span className="font-mono">TEMP_DIR_BASE</span> in .env.
      </p>
      <dl className="space-y-1 font-mono text-[11px] text-text-secondary">
        <div>
          Sessions / jobs: {settings.data_dir || '(default)'}
        </div>
        <div>
          Clones: {settings.temp_dir_base || '(default)'}
        </div>
      </dl>

      <p className="quiet">
        Jira token {settings.jira_token_configured ? 'set' : 'missing'} · GitLab{' '}
        {settings.gitlab_pat_configured ? 'set' : 'missing'} · Azure{' '}
        {settings.azure_pat_configured ? 'set' : 'missing'} · dashboard{' '}
        {settings.dashboard_host}:{settings.dashboard_port}
      </p>
      </div>
      )}

      <p>
        <button
          type="button"
          className="go"
          disabled={saving || modelsLoading || (!dirty && !saved)}
          onClick={() => void onSave()}
        >
          {saving ? (
            <>
              <Spinner /> Saving…
            </>
          ) : modelsLoading ? (
            <>
              <Spinner /> Loading models…
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
