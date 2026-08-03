import { useState, type Dispatch, type SetStateAction } from 'react'
import {
  testGitlabConnection,
  testJiraConnection,
  type GitlabConnectionTestResult,
  type JiraConnectionTestResult,
} from '../api'
import type {
  DashboardPayload,
  GitlabHostCredentialDraft,
  ModelsPayload,
  SettingsPayload,
} from '../types'

/** Draft may include write-only secret fields not present on GET payload. */
export type SettingsDraft = Partial<SettingsPayload> & {
  jira_api_token?: string
  gitlab_pat?: string
  /** Editable GitLab host→PAT rows */
  gitlab_cred_rows?: GitlabHostCredentialDraft[]
}

export function SettingsPage({
  data,
  settingsDraft,
  setSettingsDraft,
  setSettingsDirty,
  modelsPayload,
  modelsLoading,
  modelsFetchError,
  loadModels,
  saving,
  onSave,
}: {
  data: DashboardPayload
  settingsDraft: SettingsDraft
  setSettingsDraft: Dispatch<SetStateAction<SettingsDraft>>
  setSettingsDirty: (v: boolean) => void
  modelsPayload: ModelsPayload | null
  modelsLoading: boolean
  modelsFetchError: string | null
  loadModels: (refresh?: boolean) => void
  saving: boolean
  onSave: () => void
}) {
  const mark = <K extends keyof SettingsDraft>(key: K, value: SettingsDraft[K]) => {
    setSettingsDirty(true)
    setSettingsDraft((s) => ({ ...s, [key]: value }))
  }

  const [testingIdx, setTestingIdx] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<
    Record<number, GitlabConnectionTestResult | { loading: true }>
  >({})
  const [jiraTesting, setJiraTesting] = useState(false)
  const [jiraTestResult, setJiraTestResult] = useState<
    JiraConnectionTestResult | { loading: true } | null
  >(null)

  const onTestJira = async () => {
    const host = (
      settingsDraft.jira_host ??
      data.settings.jira_host ??
      ''
    ).trim()
    const email = (
      settingsDraft.jira_email ??
      data.settings.jira_email ??
      ''
    ).trim()
    const token = (settingsDraft.jira_api_token ?? '').trim()
    if (!host) {
      setJiraTestResult({ ok: false, error: 'Jira host is required' })
      return
    }
    if (!token && !data.settings.jira_token_configured) {
      setJiraTestResult({
        ok: false,
        error: 'Paste an API token or save settings first',
      })
      return
    }
    setJiraTesting(true)
    setJiraTestResult({ loading: true })
    try {
      // email + token → Cloud Basic; token only → Bearer PAT
      const result = await testJiraConnection({
        host,
        email: email || undefined,
        api_token: token || undefined,
      })
      setJiraTestResult(result)
    } catch (e) {
      setJiraTestResult({
        ok: false,
        error: e instanceof Error ? e.message : 'Test failed',
      })
    } finally {
      setJiraTesting(false)
    }
  }

  const onTestGitlabRow = async (idx: number) => {
    const row = (settingsDraft.gitlab_cred_rows || [])[idx]
    if (!row) return
    const host = (row.host || '').trim()
    if (!host) {
      setTestResults((r) => ({
        ...r,
        [idx]: { ok: false, error: 'Enter a host first' },
      }))
      return
    }
    if (!(row.pat || '').trim() && !row.pat_configured) {
      setTestResults((r) => ({
        ...r,
        [idx]: {
          ok: false,
          error: 'Paste a PAT or save credentials before testing',
        },
      }))
      return
    }
    setTestingIdx(idx)
    setTestResults((r) => ({ ...r, [idx]: { loading: true } }))
    try {
      const result = await testGitlabConnection({
        host,
        pat: (row.pat || '').trim() || undefined,
      })
      setTestResults((r) => ({ ...r, [idx]: result }))
    } catch (e) {
      setTestResults((r) => ({
        ...r,
        [idx]: {
          ok: false,
          error: e instanceof Error ? e.message : 'Test failed',
        },
      }))
    } finally {
      setTestingIdx(null)
    }
  }

  return (
    <section className="max-w-xl space-y-4">
      <div>
        <h2 className="text-base font-semibold text-text">Settings</h2>
        <p className="text-xs text-text-muted">
          Runtime values only — not written to <code className="text-text-secondary">.env</code>.
          Token fields are write-only (leave blank to keep the current value). Secrets are
          never returned by the API.
        </p>
      </div>

      <div className="ops-card space-y-4 p-5">
        <div className="space-y-3 rounded border border-border bg-bg p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-text">OpenCode model</div>
              <p className="mt-1 text-xs text-text-muted">
                List from <code className="text-text-secondary">GET /api/models</code>.
                Saving updates runtime{' '}
                <code className="text-text-secondary">DEFAULT_MODEL</code>.
              </p>
            </div>
            <button
              type="button"
              disabled={modelsLoading}
              onClick={() => loadModels(true)}
              className="ops-btn ops-btn-secondary shrink-0 px-2.5 py-1 text-xs"
            >
              {modelsLoading ? 'Loading…' : 'Refresh list'}
            </button>
          </div>
          <label className="block text-sm">
            <span className="text-text-secondary">Default model</span>
            <select
              className="ops-input mt-1"
              disabled={modelsLoading && !modelsPayload}
              value={
                settingsDraft.default_model ?? data.settings.default_model ?? ''
              }
              onChange={(e) => mark('default_model', e.target.value)}
            >
              <option value="">
                {modelsLoading ? 'Loading models…' : '— select a model —'}
              </option>
              {(modelsPayload?.models ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label || m.id}
                </option>
              ))}
            </select>
          </label>
          <label className="block text-sm">
            <span className="text-text-secondary">Or type provider/model id</span>
            <input
              className="ops-input mt-1 font-mono text-sm"
              value={
                settingsDraft.default_model ?? data.settings.default_model ?? ''
              }
              placeholder="opencode/deepseek-v4-flash-free"
              onChange={(e) => mark('default_model', e.target.value)}
            />
          </label>
          {(modelsFetchError || modelsPayload?.error) && (
            <p className="text-xs text-warning-text">
              {modelsFetchError || modelsPayload?.error}
            </p>
          )}
          <div className="text-xs text-text-muted">
            Active:{' '}
            <span className="font-mono text-text-secondary">
              {settingsDraft.default_model ||
                data.settings.default_model ||
                modelsPayload?.default_model ||
                '(unset)'}
            </span>
            {modelsPayload != null && (
              <span> · {modelsPayload.models.length} from API</span>
            )}
          </div>
          <div className="break-all text-xs text-text-muted">
            {!modelsPayload ? (
              <>Model inventory loads when this tab opens.</>
            ) : modelsPayload.opencode_config_path ? (
              <>
                OpenCode config:{' '}
                <span className="font-mono text-text-secondary">
                  {modelsPayload.opencode_config_path}
                </span>
                {modelsPayload.opencode_config_model
                  ? ` · model key: ${modelsPayload.opencode_config_model}`
                  : ''}
              </>
            ) : (
              <>
                No opencode.json found (project root or ~/.config/opencode).
              </>
            )}
          </div>
        </div>

        <div className="space-y-3 rounded border border-border bg-bg p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-text">Jira connection</div>
              <p className="mt-1 text-xs text-text-muted">
                <strong>Prod / on-prem:</strong> host + PAT →{' '}
                <code className="text-text-secondary">Bearer</code> (leave email
                empty). <strong>Cloud API token (dev):</strong> host + email +
                token → HTTP Basic. Saving host/email/token rebuilds live Jira
                clients. Use <strong>Test</strong> for{' '}
                <code className="text-text-secondary">/myself</code> + projects.
              </p>
            </div>
            <button
              type="button"
              className="ops-btn ops-btn-secondary shrink-0 px-2.5 py-1 text-xs"
              disabled={jiraTesting}
              onClick={() => void onTestJira()}
            >
              {jiraTesting ? 'Testing…' : 'Test connection'}
            </button>
          </div>
          <label className="block text-sm">
            <span className="text-text-secondary">Jira host</span>
            <input
              className="ops-input mt-1 font-mono text-xs"
              value={settingsDraft.jira_host ?? data.settings.jira_host ?? ''}
              placeholder="https://jira.example.com"
              onChange={(e) => {
                mark('jira_host', e.target.value)
                setJiraTestResult(null)
              }}
              autoComplete="off"
            />
          </label>
          <label className="block text-sm">
            <span className="text-text-secondary">
              Jira email{' '}
              <span className="text-text-muted">
                (Cloud API token only; empty = Bearer PAT)
              </span>
            </span>
            <input
              type="email"
              className="ops-input mt-1"
              value={settingsDraft.jira_email ?? data.settings.jira_email ?? ''}
              placeholder="you@company.com"
              onChange={(e) => {
                mark('jira_email', e.target.value)
                setJiraTestResult(null)
              }}
              autoComplete="off"
            />
          </label>
          <label className="block text-sm">
            <span className="text-text-secondary">
              Jira API token / PAT{' '}
              <span className="text-text-muted">
                (
                {data.settings.jira_token_configured
                  ? 'configured — leave blank to keep'
                  : 'not set'}
                )
              </span>
            </span>
            <input
              type="password"
              className="ops-input mt-1 font-mono text-xs"
              value={settingsDraft.jira_api_token ?? ''}
              placeholder={
                data.settings.jira_token_configured
                  ? '•••••••• (unchanged if empty)'
                  : 'Paste token'
              }
              onChange={(e) => {
                mark('jira_api_token', e.target.value)
                setJiraTestResult(null)
              }}
              autoComplete="new-password"
            />
          </label>

          {jiraTestResult && 'loading' in jiraTestResult && jiraTestResult.loading && (
            <p className="text-xs text-text-muted">Contacting Jira API…</p>
          )}
          {jiraTestResult && !('loading' in jiraTestResult) && !jiraTestResult.ok && (
            <div role="alert" className="ops-alert ops-alert-danger text-xs">
              {jiraTestResult.error || 'Connection failed'}
            </div>
          )}
          {jiraTestResult && !('loading' in jiraTestResult) && jiraTestResult.ok && (
            <div className="space-y-2 rounded border border-border bg-bg p-3 text-xs">
              <div className="font-medium text-success-text">
                {jiraTestResult.message || 'Connection OK'}
              </div>
              <div className="text-text-secondary">
                Auth:{' '}
                <span className="font-mono text-text">
                  {jiraTestResult.auth_mode || '—'}
                </span>
                {jiraTestResult.is_cloud ? ' · Cloud' : ' · Server/DC'}
              </div>
              {jiraTestResult.user?.display_name && (
                <div className="text-text-secondary">
                  User:{' '}
                  <span className="text-text">
                    {jiraTestResult.user.display_name}
                  </span>
                  {jiraTestResult.user.email
                    ? ` · ${jiraTestResult.user.email}`
                    : ''}
                </div>
              )}
              {jiraTestResult.projects_error && (
                <div className="text-warning-text">
                  {jiraTestResult.projects_error}
                </div>
              )}
              {(jiraTestResult.projects || []).length > 0 && (
                <div>
                  <div className="mb-1 text-text-muted">
                    Projects this token can browse:
                  </div>
                  <ul className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-text-secondary">
                    {(jiraTestResult.projects || []).map((p) => (
                      <li key={String(p.id ?? p.key)}>
                        <span className="text-text">{p.key}</span>
                        {p.name ? ` — ${p.name}` : ''}
                        {p.project_type ? (
                          <span className="ml-1 text-text-muted">
                            · {p.project_type}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {(jiraTestResult.projects || []).length === 0 &&
                !jiraTestResult.projects_error && (
                  <div className="text-text-muted">
                    Auth OK, but no projects returned (permissions may be limited).
                  </div>
                )}
            </div>
          )}
        </div>

        <div className="space-y-3 rounded border border-border bg-bg p-4">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <div className="text-sm font-semibold text-text">
                GitLab credentials
              </div>
              <p className="mt-1 text-xs text-text-muted">
                Add one row per GitLab host. Clone/push/MR uses the PAT that matches
                the repository hostname (fail-closed). Leave PAT blank to keep the
                existing token for that host.
              </p>
            </div>
            <button
              type="button"
              className="ops-btn ops-btn-secondary shrink-0 px-2.5 py-1 text-xs"
              onClick={() => {
                setSettingsDirty(true)
                setSettingsDraft((s) => ({
                  ...s,
                  gitlab_cred_rows: [
                    ...(s.gitlab_cred_rows || []),
                    { host: '', pat: '', pat_configured: false },
                  ],
                }))
              }}
            >
              Add host
            </button>
          </div>

          {(settingsDraft.gitlab_cred_rows || []).length === 0 && (
            <p className="text-xs text-warning-text">
              No GitLab hosts configured. Add a host and PAT to enable authenticated
              clone/push.
            </p>
          )}

          <div className="space-y-3">
            {(settingsDraft.gitlab_cred_rows || []).map((row, idx) => {
              const tr = testResults[idx]
              const testing = testingIdx === idx
              return (
                <div
                  key={idx}
                  className="space-y-2 rounded border border-border bg-surface p-3"
                >
                  <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto_auto]">
                    <label className="block text-sm">
                      <span className="text-text-secondary">Host</span>
                      <input
                        className="ops-input mt-1 font-mono text-xs"
                        value={row.host}
                        placeholder="gitlab.com"
                        onChange={(e) => {
                          const v = e.target.value
                          setSettingsDirty(true)
                          setSettingsDraft((s) => {
                            const rows = [...(s.gitlab_cred_rows || [])]
                            rows[idx] = { ...rows[idx], host: v }
                            return { ...s, gitlab_cred_rows: rows }
                          })
                          setTestResults((r) => {
                            const next = { ...r }
                            delete next[idx]
                            return next
                          })
                        }}
                        autoComplete="off"
                      />
                    </label>
                    <label className="block text-sm">
                      <span className="text-text-secondary">
                        PAT{' '}
                        <span className="text-text-muted">
                          (
                          {row.pat_configured
                            ? 'configured — blank keeps it'
                            : 'required for new host'}
                          )
                        </span>
                      </span>
                      <input
                        type="password"
                        className="ops-input mt-1 font-mono text-xs"
                        value={row.pat}
                        placeholder={
                          row.pat_configured
                            ? '•••••••• (unchanged if empty)'
                            : 'glpat-…'
                        }
                        onChange={(e) => {
                          const v = e.target.value
                          setSettingsDirty(true)
                          setSettingsDraft((s) => {
                            const rows = [...(s.gitlab_cred_rows || [])]
                            rows[idx] = { ...rows[idx], pat: v }
                            return { ...s, gitlab_cred_rows: rows }
                          })
                        }}
                        autoComplete="new-password"
                      />
                    </label>
                    <div className="flex items-end">
                      <button
                        type="button"
                        className="ops-btn ops-btn-secondary px-2.5 py-1 text-xs"
                        disabled={testing}
                        onClick={() => void onTestGitlabRow(idx)}
                        title="Call GitLab API as this token and list reachable projects"
                      >
                        {testing ? 'Testing…' : 'Test'}
                      </button>
                    </div>
                    <div className="flex items-end">
                      <button
                        type="button"
                        className="ops-btn ops-btn-secondary px-2 py-1 text-xs text-danger-text"
                        onClick={() => {
                          setSettingsDirty(true)
                          setSettingsDraft((s) => ({
                            ...s,
                            gitlab_cred_rows: (s.gitlab_cred_rows || []).filter(
                              (_, i) => i !== idx,
                            ),
                          }))
                          setTestResults((r) => {
                            const next = { ...r }
                            delete next[idx]
                            return next
                          })
                        }}
                      >
                        Remove
                      </button>
                    </div>
                  </div>

                  {tr && 'loading' in tr && tr.loading && (
                    <p className="text-xs text-text-muted">
                      Contacting GitLab API…
                    </p>
                  )}
                  {tr && !('loading' in tr) && !tr.ok && (
                    <div
                      role="alert"
                      className="ops-alert ops-alert-danger text-xs"
                    >
                      {tr.error || 'Connection failed'}
                    </div>
                  )}
                  {tr && !('loading' in tr) && tr.ok && (
                    <div className="rounded border border-border bg-bg p-3 text-xs space-y-2">
                      <div className="text-success-text font-medium">
                        {tr.message || 'Connection OK'}
                      </div>
                      {tr.user?.username && (
                        <div className="text-text-secondary">
                          User:{' '}
                          <span className="font-mono text-text">
                            @{tr.user.username}
                          </span>
                          {tr.user.name ? ` (${tr.user.name})` : ''}
                        </div>
                      )}
                      {tr.projects_error && (
                        <div className="text-warning-text">
                          {tr.projects_error}
                        </div>
                      )}
                      {(tr.projects || []).length > 0 && (
                        <div>
                          <div className="mb-1 text-text-muted">
                            Reachable projects (membership, recent):
                          </div>
                          <ul className="max-h-40 space-y-0.5 overflow-y-auto font-mono text-[11px] text-text-secondary">
                            {(tr.projects || []).map((p) => (
                              <li key={String(p.id ?? p.path_with_namespace)}>
                                {p.web_url ? (
                                  <a
                                    href={p.web_url}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="text-accent-text hover:underline"
                                  >
                                    {p.path_with_namespace || p.name}
                                  </a>
                                ) : (
                                  p.path_with_namespace || p.name
                                )}
                                {p.visibility ? (
                                  <span className="ml-1 text-text-muted">
                                    · {p.visibility}
                                  </span>
                                ) : null}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {(tr.projects || []).length === 0 && !tr.projects_error && (
                        <div className="text-text-muted">
                          Auth OK, but no membership projects returned (token may
                          still work for direct clone URLs).
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        <label className="block text-sm">
          <span className="text-text-secondary">Board ID</span>
          <input
            className="ops-input mt-1"
            value={settingsDraft.jira_board_id ?? ''}
            onChange={(e) => mark('jira_board_id', e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="text-text-secondary">Poll interval (seconds)</span>
          <input
            type="number"
            min={5}
            max={3600}
            className="ops-input mt-1"
            value={settingsDraft.poll_interval_seconds ?? 30}
            onChange={(e) => mark('poll_interval_seconds', Number(e.target.value))}
          />
        </label>
        <label className="block text-sm">
          <span className="text-text-secondary">Trigger labels (comma-separated)</span>
          <input
            className="ops-input mt-1"
            value={settingsDraft.trigger_labels ?? ''}
            onChange={(e) => mark('trigger_labels', e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={Boolean(settingsDraft.trigger_on_assignment)}
            onChange={(e) => mark('trigger_on_assignment', e.target.checked)}
          />
          Trigger on bot assignment
        </label>
        <label className="block text-sm">
          <span className="text-text-secondary">Max concurrent jobs</span>
          <input
            type="number"
            min={1}
            max={32}
            className="ops-input mt-1"
            value={settingsDraft.max_concurrent_jobs ?? 3}
            onChange={(e) => mark('max_concurrent_jobs', Number(e.target.value))}
          />
        </label>
        <label className="block text-sm">
          <span className="text-text-secondary">
            Agent / OpenCode timeout (seconds)
          </span>
          <input
            type="number"
            min={30}
            max={86400}
            step={30}
            className="ops-input mt-1"
            value={
              settingsDraft.agent_task_timeout_seconds ??
              data.settings.agent_task_timeout_seconds ??
              1800
            }
            onChange={(e) =>
              mark('agent_task_timeout_seconds', Number(e.target.value))
            }
          />
          <p className="mt-1 text-xs text-text-muted">
            One wall-clock budget for both the agent runner and the OpenCode CLI
            process (default 1800 = 30 min). The orchestrator kills the process
            when this limit is hit. Runtime only — not written to{' '}
            <code className="text-text-secondary">.env</code>.
          </p>
        </label>

        <div className="grid grid-cols-2 gap-2 border-t border-border pt-3 text-xs text-text-muted">
          <div>
            Jira token:{' '}
            <span
              className={
                data.settings.jira_token_configured
                  ? 'text-success-text'
                  : 'text-warning-text'
              }
            >
              {data.settings.jira_token_configured ? 'configured' : 'missing'}
            </span>
          </div>
          <div>
            GitLab PAT:{' '}
            <span
              className={
                data.settings.gitlab_pat_configured
                  ? 'text-success-text'
                  : 'text-warning-text'
              }
            >
              {data.settings.gitlab_pat_configured ? 'configured' : 'missing'}
            </span>
          </div>
          <div>Base branch: {data.settings.default_branch}</div>
          <div>
            Dashboard: {data.settings.dashboard_host}:{data.settings.dashboard_port}
          </div>
        </div>

        <button
          type="button"
          disabled={saving}
          onClick={onSave}
          className="ops-btn ops-btn-primary"
        >
          {saving ? 'Saving…' : 'Save settings'}
        </button>
      </div>
    </section>
  )
}
