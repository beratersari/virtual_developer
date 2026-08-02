import type { Dispatch, SetStateAction } from 'react'
import type { DashboardPayload, ModelsPayload, SettingsPayload } from '../types'

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
  settingsDraft: Partial<SettingsPayload>
  setSettingsDraft: Dispatch<SetStateAction<Partial<SettingsPayload>>>
  setSettingsDirty: (v: boolean) => void
  modelsPayload: ModelsPayload | null
  modelsLoading: boolean
  modelsFetchError: string | null
  loadModels: (refresh?: boolean) => void
  saving: boolean
  onSave: () => void
}) {
  const mark = <K extends keyof SettingsPayload>(key: K, value: SettingsPayload[K]) => {
    setSettingsDirty(true)
    setSettingsDraft((s) => ({ ...s, [key]: value }))
  }

  return (
    <section className="max-w-xl space-y-4">
      <div>
        <h2 className="text-base font-semibold text-text">Settings</h2>
        <p className="text-xs text-text-muted">
          Runtime values only. Secrets are never shown. Changes apply until process
          restart (unless also set in .env).
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

        <label className="block text-sm">
          <span className="text-text-muted">Jira host (read-only)</span>
          <input
            disabled
            className="ops-input mt-1"
            value={data.settings.jira_host}
          />
        </label>
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

        <div className="grid grid-cols-2 gap-2 border-t border-border pt-3 text-xs text-text-muted">
          <div>
            Jira token:{' '}
            {data.settings.jira_token_configured ? 'configured' : 'missing'}
          </div>
          <div>
            GitLab PAT:{' '}
            {data.settings.gitlab_pat_configured ? 'configured' : 'missing'}
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
