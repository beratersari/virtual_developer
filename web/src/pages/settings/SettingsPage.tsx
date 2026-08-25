import { useEffect, useState } from 'react'
import {
  fetchSettings,
  patchSettings,
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
import { ModelField } from '../../ui/ModelField'
import { PageHeader } from '../../ui/PageHeader'
import { Spinner } from '../../ui/Spinner'

type Draft = {
  jira_host: string
  jira_api_token: string
  jira_board_id: string
  poll_interval_seconds: number
  trigger_labels: string
  trigger_on_assignment: boolean
  jira_intake_mode: string
  jira_webhook_secret: string
  trigger_mentions: string
  trigger_assignee_names: string
  max_concurrent_jobs: number
  agent_task_timeout_seconds: number
  agent_task_max_retries: number
  agent_task_max_incomplete_retries: number
  default_model: string
  agent_backend: string
  gitlab_cred_rows: GitlabHostCredentialDraft[]
  project_repositories: ProjectRepository[]
}

function fromSettings(s: SettingsPayload): Draft {
  return {
    jira_host: s.jira_host,
    jira_api_token: '',
    jira_board_id: s.jira_board_id,
    poll_interval_seconds: s.poll_interval_seconds,
    trigger_labels: s.trigger_labels,
    trigger_on_assignment: s.trigger_on_assignment,
    jira_intake_mode: s.jira_intake_mode === 'webhook' ? 'webhook' : 'poll',
    jira_webhook_secret: '',
    trigger_mentions: s.trigger_mentions ?? '',
    trigger_assignee_names: s.trigger_assignee_names ?? '',
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
    project_repositories: (s.project_repositories ?? []).map((p) => ({
      label: p.label || '',
      url: p.url || '',
      target_branch: p.target_branch || '',
      source_branch: p.source_branch || '',
    })),
  }
}

export function SettingsPage() {
  const [settings, setSettings] = useState<SettingsPayload | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [section, setSection] = useState<'jira' | 'gitlab' | 'projects' | 'model' | 'runtime'>('jira')
  const [jiraResult, setJiraResult] = useState<JiraConnectionTestResult | null>(null)
  const [gitlabResults, setGitlabResults] = useState<Record<string, GitlabConnectionTestResult>>(
    {},
  )
  const [jiraTesting, setJiraTesting] = useState(false)
  const [gitlabTestingIdx, setGitlabTestingIdx] = useState<number | null>(null)
  const [saved, setSaved] = useState(false)

  const mark = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDirty(true)
    setDraft((d) => (d ? { ...d, [key]: value } : d))
  }

  useEffect(() => {
    void fetchSettings()
      .then((s) => {
        setSettings(s)
        setDraft(fromSettings(s))
      })
      .catch((e: Error) => setError(e.message))
  }, [])

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
        jira_board_id: draft.jira_board_id.trim(),
        poll_interval_seconds: Number(draft.poll_interval_seconds),
        trigger_labels: draft.trigger_labels,
        trigger_on_assignment: draft.trigger_on_assignment,
        jira_intake_mode: draft.jira_intake_mode === 'webhook' ? 'webhook' : 'poll',
        trigger_mentions: draft.trigger_mentions,
        trigger_assignee_names: draft.trigger_assignee_names,
        max_concurrent_jobs: Number(draft.max_concurrent_jobs),
        agent_task_timeout_seconds: Number(draft.agent_task_timeout_seconds),
        agent_task_max_retries: Number(draft.agent_task_max_retries),
        agent_task_max_incomplete_retries: Number(draft.agent_task_max_incomplete_retries),
        default_model: draft.default_model.trim(),
        agent_backend: draft.agent_backend,
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
        project_repositories: draft.project_repositories
          .map((p) => ({
            label: p.label.trim(),
            url: p.url.trim(),
            target_branch: (p.target_branch || '').trim(),
            source_branch: (p.source_branch || '').trim(),
          }))
          .filter((p) => p.url),
      }
      if (draft.jira_api_token.trim()) body.jira_api_token = draft.jira_api_token.trim()
      if (draft.jira_webhook_secret.trim()) {
        body.jira_webhook_secret = draft.jira_webhook_secret.trim()
      }
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
        description="Non-secret fields stay in runtime settings. Jira host/token and GitLab PATs are also written to .env so the next start uses them. Leave secret fields blank to keep the current value. Saving Settings clears JIRA_EMAIL so later auth is token-only."
      />

      <div className="flex w-fit flex-wrap gap-1 rounded-full border border-border bg-bg-elevated p-1">
        {(
          [
            ['jira', 'Jira'],
            ['gitlab', 'GitLab'],
            ['projects', 'Projects'],
            ['model', 'Agent'],
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
        the last saved token. If JIRA_EMAIL is set in .env it is used until you
        save Settings; a save clears it and later auth is token-only.
      </p>
      <label className="field">
        <span>Host</span>
        <input value={draft.jira_host} onChange={(e) => mark('jira_host', e.target.value)} />
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
            const token = draft.jira_api_token.trim()
            // Omit blank fields so the API uses the last saved host/token.
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

      {section === 'projects' && (
      <div key="projects" className="vd-fade space-y-3">
        <div>
          <div className="text-sm font-semibold text-text">Saved projects</div>
          <p className="mt-1 text-xs text-text-muted">
            Pick these by name on Scheduled → New issue instead of pasting the
            git URL every time.
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
                  setDirty(true)
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
                  setDirty(true)
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
                    setDirty(true)
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
                    setDirty(true)
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
                  setDirty(true)
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
              setDirty(true)
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
          onChange={(e) => mark('agent_backend', e.target.value)}
        >
          <option value="opencode">OpenCode</option>
          <option value="codex">Codex</option>
        </select>
        <span className="mt-1 block text-xs text-text-muted">
          Same unattended job contract. Per-issue {'{params}'} Backend: overrides this.
          Provider auth and endpoints stay in each tool&apos;s own config
          (OpenCode: opencode.json · Codex: ~/.codex/config.toml).
        </span>
      </label>
      <p className="text-xs text-text-muted">
        One model id for both OpenCode and Codex jobs. The list follows the
        worker above. Other id stays typed.
      </p>
      <ModelField
        label="Default model"
        value={draft.default_model}
        onChange={(v) => mark('default_model', v)}
        backend={draft.agent_backend}
        allowEmpty={false}
        showRefresh
      />
      </div>
      )}

      {section === 'runtime' && (
      <div key="runtime" className="vd-fade space-y-3">
      <div className="text-sm font-semibold text-text">Jira intake</div>
      <p className="text-xs text-text-muted">
        Poll reads the board on an interval. Webhook waits for Jira to POST
        assignment-to-bot or a comment that mentions the bot. Default comes
        from <span className="font-mono">JIRA_INTAKE_MODE</span> in .env.
      </p>
      <label className="field">
        <span>Intake mode</span>
        <select
          value={draft.jira_intake_mode}
          onChange={(e) => mark('jira_intake_mode', e.target.value)}
        >
          <option value="poll">Poll (board / sprint)</option>
          <option value="webhook">Webhook (assignment + mention)</option>
        </select>
      </label>
      {draft.jira_intake_mode === 'webhook' && (
        <div className="space-y-3 rounded border border-border bg-bg px-4 py-3">
          <p className="text-xs text-text-muted">
            Jira Server 9.4: System → WebHooks. Events: Issue created, Issue
            updated, Comment created. URL includes the token (no HMAC on Server).
          </p>
          <dl className="grid gap-1 font-mono text-[11px] text-text-secondary">
            <div>
              Secret:{' '}
              {settings.jira_webhook_secret_configured ? 'configured' : 'missing (required)'}
            </div>
            <div>
              URL: http://&lt;this-host&gt;:{settings.dashboard_port}
              {settings.jira_webhook_path || '/webhooks/jira'}
              ?token=&lt;secret&gt;
            </div>
          </dl>
          <label className="field">
            <span>
              Webhook secret{' '}
              {settings.jira_webhook_secret_configured
                ? '(set — blank keeps it)'
                : '(required)'}
            </span>
            <input
              type="password"
              autoComplete="new-password"
              value={draft.jira_webhook_secret}
              onChange={(e) => mark('jira_webhook_secret', e.target.value)}
            />
          </label>
        </div>
      )}
      <label className="field">
        <span>Mention tokens</span>
        <input
          value={draft.trigger_mentions}
          onChange={(e) => mark('trigger_mentions', e.target.value)}
          placeholder="@DevBot,@AI"
        />
        <span className="text-xs text-text-muted">
          Comment must mention one of these (or wiki [~user] matching the bot names).
        </span>
      </label>
      <label className="field">
        <span>Bot assignee names</span>
        <input
          value={draft.trigger_assignee_names}
          onChange={(e) => mark('trigger_assignee_names', e.target.value)}
          placeholder="devbot,jira ai bot"
        />
        <span className="text-xs text-text-muted">
          Assignment to a matching user starts a job. Unassign does not.
        </span>
      </label>
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
        OpenCode auto-compacts in-session. The orchestrator waits for that to
        finish — it does not inject a Continue user message. Next job uses the
        saved values.
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
          Extra serve attempts only when the session is incomplete for a
          non-compact reason. Compact wait is unbounded (same session until
          the agent timeout). Default 256.
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
