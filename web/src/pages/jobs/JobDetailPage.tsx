import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { cancelTask, deleteJob, fetchJobArtifacts, fetchJobById } from '../../api/client'
import type { JobItem, SystemLogLine, TextArtifact } from '../../api/types'
import { forgetJob, peekJob, rememberJob } from '../../app/entityCache'
import { useLive } from '../../app/live'
import {
  acceptJobArtifactsResponse,
  artifactsHaveContent,
  jobArtifactPathSignature,
  shouldRefetchJobArtifacts,
} from '../../util/artifacts'
import { IN_FLIGHT_STATUSES, jobIsCancellable, jobIsDeletable } from '../../util/status'
import { useElapsedLabel } from '../../util/time'
import { ConfirmDialog } from '../../ui/ConfirmDialog'
import { Spinner } from '../../ui/Spinner'
import { LiveDot } from '../../ui/LiveDot'
import { StatusBadge } from '../../ui/StatusBadge'
import { Tabs } from '../../ui/Tabs'
import { JobOverview } from './JobOverview'
import { JobPromptTab, JobSessionTab } from './JobArtifacts'
import { JobChatTab } from './JobChatTab'

type JobTab = 'overview' | 'prompt' | 'chat' | 'opencode' | 'logs'

export function JobDetailPage() {
  const { jobId = '' } = useParams()
  const navigate = useNavigate()
  const live = useLive()
  const cached = peekJob(jobId.trim())
  const [job, setJob] = useState<JobItem | null>(cached)
  const [prompts, setPrompts] = useState<TextArtifact[]>([])
  const [sessionLogs, setSessionLogs] = useState<TextArtifact[]>([])
  const [systemLogs, setSystemLogs] = useState<SystemLogLine[]>([])
  const [loading, setLoading] = useState(!cached)
  const [artsLoading, setArtsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [tab, setTab] = useState<JobTab>('overview')
  const [confirm, setConfirm] = useState<'cancel' | 'delete' | null>(null)
  const [busy, setBusy] = useState(false)
  const reqId = useRef(0)
  const lastSoft = useRef(0)
  const artsFor = useRef('')
  const artsSig = useRef('')
  const artsHad = useRef(false)
  const artsInFlight = useRef(false)
  const artsGen = useRef(0)
  const jobIdRef = useRef(jobId)
  jobIdRef.current = jobId

  const loadArtifacts = useCallback(async (id: string, force = false, sig = '') => {
    if (!id) return
    if (artsInFlight.current && !force) return
    const nextSig = sig || artsSig.current
    if (
      !shouldRefetchJobArtifacts({
        jobId: id,
        lastJobId: artsFor.current,
        force,
        live: false,
        pathSignature: nextSig,
        lastPathSignature: artsSig.current,
        lastHadContent: artsHad.current,
      })
    ) {
      return
    }
    const gen = artsGen.current
    artsInFlight.current = true
    setArtsLoading(true)
    try {
      const arts = await fetchJobArtifacts(id)
      if (gen !== artsGen.current) return
      if (!acceptJobArtifactsResponse(id, jobIdRef.current)) return
      artsFor.current = id
      if (sig) artsSig.current = sig
      const nextPrompts = arts.prompts || []
      const nextLogs = arts.session_logs || []
      artsHad.current = artifactsHaveContent(nextPrompts, nextLogs)
      setPrompts(nextPrompts)
      setSessionLogs(nextLogs)
    } catch {
      /* tab shows its own empty/warning */
    } finally {
      if (gen === artsGen.current) {
        artsInFlight.current = false
        setArtsLoading(false)
      }
    }
  }, [])

  const load = useCallback(
    async (soft = false) => {
      const id = jobId.trim()
      if (!id) return
      const req = ++reqId.current
      const haveRow = Boolean(peekJob(id))
      if (!soft && !haveRow) {
        setLoading(true)
        setError(null)
      }
      try {
        const body = await fetchJobById(id)
        if (req !== reqId.current) return
        if (!body.job.job_id) throw new Error(`Job ${id} not found`)
        rememberJob(body.job)
        setJob(body.job)
        setSystemLogs(Array.isArray(body.system_logs) ? body.system_logs : [])
        setStale(false)
        void loadArtifacts(id, !soft, jobArtifactPathSignature(body.job))
      } catch (e) {
        if (req !== reqId.current) return
        if (soft || haveRow) setStale(true)
        else {
          setJob(null)
          setError(e instanceof Error ? e.message : 'Failed to load job')
        }
      } finally {
        if (req === reqId.current) setLoading(false)
      }
    },
    [jobId, loadArtifacts],
  )

  useEffect(() => {
    setTab('overview')
    artsGen.current += 1
    artsInFlight.current = false
    artsFor.current = ''
    artsSig.current = ''
    artsHad.current = false
    lastSoft.current = Date.now()
    setPrompts([])
    setSessionLogs([])
    const seed = peekJob(jobId.trim())
    setJob(seed)
    setSystemLogs([])
    setLoading(!seed)
    void load(Boolean(seed))
  }, [jobId]) // eslint-disable-line react-hooks/exhaustive-deps — remount seed per id

  useEffect(() => {
    const now = Date.now()
    if (now - lastSoft.current < 4000) return
    lastSoft.current = now
    void load(true)
  }, [live.generation, load])

  const liveRun =
    Boolean(job?.live) || IN_FLIGHT_STATUSES.has((job?.status || '').toLowerCase())
  const artifactSig = job ? jobArtifactPathSignature(job) : ''

  // Chat polls OpenCode on its own. Output reads session files — refetch when
  // the path appears after the first empty snapshot, or while the log grows.
  useEffect(() => {
    const id = job?.job_id
    if (!id || id !== jobId.trim()) return
    if (artifactSig !== artsSig.current || (!artsHad.current && artifactSig)) {
      void loadArtifacts(id, true, artifactSig)
    }
  }, [artifactSig, job?.job_id, jobId, loadArtifacts])

  useEffect(() => {
    const id = job?.job_id
    if (!id || !liveRun) return
    const tick = () => {
      void loadArtifacts(id, true, artifactSig)
    }
    const timer = window.setInterval(tick, 2000)
    return () => window.clearInterval(timer)
  }, [artifactSig, job?.job_id, liveRun, loadArtifacts])

  const elapsed = useElapsedLabel(
    job?.started_at,
    job?.completed_at,
    job?.status || '',
    Boolean(job?.live),
  )
  const canCancel =
    Boolean(job?.issue_key) && jobIsCancellable(job?.status || '', Boolean(job?.live))
  const canDelete = Boolean(job) && jobIsDeletable(job!.status || '', Boolean(job!.live))

  const onCancel = async () => {
    if (!job?.issue_key || job.job_id !== jobId.trim()) return
    setBusy(true)
    setError(null)
    try {
      await cancelTask(job.issue_key)
      setConfirm(null)
      await load(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Cancel failed')
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    if (!job?.job_id || job.job_id !== jobId.trim()) return
    setBusy(true)
    setError(null)
    try {
      await deleteJob(job.job_id, { deleteArtifacts: true })
      forgetJob(job.job_id)
      navigate('/jobs')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
      setBusy(false)
    }
  }

  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <Link to="/jobs" className="vd-btn-ghost mb-3 inline-block text-sm">
            ← Jobs
          </Link>
          <div className="flex flex-wrap items-center gap-2">
            {job?.issue_key && (
              <span className="font-mono text-lg font-semibold text-text">
                {job.issue_key}
              </span>
            )}
            {job && <StatusBadge status={job.status} />}
            {job?.live && <LiveDot />}
            {elapsed !== '—' && (
              <span className="font-mono text-sm text-text-secondary">{elapsed}</span>
            )}
          </div>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">
            {job?.summary || (loading ? 'Loading…' : 'Job')}
          </h1>
          <p className="mt-1 font-mono text-xs text-text-muted">
            {job?.job_id}
            {job?.workflow_type ? ` · ${job.workflow_type}` : ''}
            {job?.agent ? ` · ${job.agent}` : ''}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canCancel && (
            <button
              type="button"
              className="vd-btn vd-btn-danger"
              disabled={busy}
              onClick={() => setConfirm('cancel')}
            >
              Stop work
            </button>
          )}
          <button
            type="button"
            onClick={() => void load(false)}
            className="vd-btn vd-btn-secondary"
            disabled={loading}
          >
            {loading ? (
              <>
                <Spinner /> Refreshing…
              </>
            ) : (
              'Refresh'
            )}
          </button>
          <button
            type="button"
            className="vd-btn vd-btn-secondary"
            disabled={!canDelete || busy}
            onClick={() => setConfirm('delete')}
          >
            Delete
          </button>
        </div>
      </div>

      <Tabs
        tabs={[
          { id: 'overview', label: 'Details' },
          { id: 'prompt', label: 'Prompt' },
          { id: 'chat', label: 'Chat' },
          { id: 'opencode', label: 'Output' },
          { id: 'logs', label: 'Daemon', count: systemLogs.length },
        ]}
        value={tab}
        onChange={setTab}
      />

      <div className="vd-panel min-h-[50vh] p-5">
        {loading && !job && <p className="text-sm text-text-muted">Loading job…</p>}
        {error && <p className="text-sm text-danger-text">{error}</p>}
        {stale && !error && (
          <p className="mb-3 text-sm text-warning-text">May be stale — refresh if this looks wrong.</p>
        )}
        {job && tab === 'overview' && (
          <div key="overview" className="vd-fade">
            <JobOverview job={job} elapsedLabel={elapsed} />
          </div>
        )}
        {job && tab === 'prompt' && (
          <div key="prompt" className="vd-fade">
            {artsLoading && prompts.length === 0 && (
              <p className="mb-3 text-sm text-text-muted">Loading prompt…</p>
            )}
            <JobPromptTab job={job} prompts={prompts} />
          </div>
        )}
        {job && tab === 'chat' && (
          <div key="chat" className="vd-fade">
            <JobChatTab
              key={jobId.trim()}
              jobId={jobId.trim()}
              liveRun={liveRun && job.job_id === jobId.trim()}
            />
          </div>
        )}
        {job && tab === 'opencode' && (
          <div key="opencode" className="vd-fade">
            {artsLoading && sessionLogs.length === 0 && (
              <p className="mb-3 text-sm text-text-muted">Loading output…</p>
            )}
            <JobSessionTab job={job} sessionLogs={sessionLogs} />
          </div>
        )}
        {job && tab === 'logs' && (
          <div key="logs" className="vd-fade space-y-2 text-sm">
            <p className="text-xs text-text-muted">
              Daemon lines for <span className="font-mono">{job.job_id}</span>.
            </p>
            {systemLogs.length === 0 && (
              <p className="text-text-muted">No system log lines for this job.</p>
            )}
            <div className="max-h-[70vh] overflow-auto rounded border border-border bg-bg p-4 font-mono text-[11px] leading-relaxed text-text-secondary">
              {systemLogs.map((line, i) => (
                <div key={`${line.timestamp}-${i}`} className="border-b border-border/50 py-0.5">
                  <span className="text-text-muted">{line.timestamp} </span>
                  {line.message}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirm === 'cancel'}
        title={`Cancel work for ${job?.issue_key}?`}
        body={`Stops in-flight agent work for this issue.${job?.job_id ? `\n\nJob: ${job.job_id}` : ''}`}
        confirmLabel="Stop work"
        danger
        busy={busy}
        onConfirm={() => void onCancel()}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === 'delete'}
        title={`Delete job ${job?.job_id}?`}
        body="Removes the job history record and linked session/prompt files under .jira-agent. Does not change the Jira issue."
        confirmLabel="Delete job"
        danger
        busy={busy}
        onConfirm={() => void onDelete()}
        onCancel={() => setConfirm(null)}
      />
    </section>
  )
}
