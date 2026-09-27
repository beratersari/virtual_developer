import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AnalyticsPage } from '../pages/analytics/AnalyticsPage'
import { AnalyticsReviewsPage } from '../pages/analytics/AnalyticsReviewsPage'
import { IssueDetailPage } from '../pages/issues/IssueDetailPage'
import { JobsAtJobOrPage, JobsPage } from '../pages/jobs/JobsPage'
import { PollPage } from '../pages/poll/PollPage'
import { SchedulesPage } from '../pages/schedules/SchedulesPage'
import { SessionsAtId, SessionsPage } from '../pages/sessions/SessionsPage'
import { SettingsPage } from '../pages/settings/SettingsPage'
import { StoragePage } from '../pages/storage/StoragePage'
import { DashboardAuthGate } from '../auth/DashboardAuthGate'
import { LiveProvider } from './LiveProvider'
import { Shell } from './Shell'

export default function App() {
  return (
    <DashboardAuthGate>
    <BrowserRouter>
      <LiveProvider>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/" element={<Navigate to="/jobs" replace />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/in-flight/:page?" element={<JobsPage />} />
            <Route path="/jobs/queue/:page?" element={<JobsPage />} />
            <Route path="/jobs/error/:page?" element={<JobsPage />} />
            <Route path="/jobs/completed/:page?" element={<JobsPage />} />
            <Route path="/jobs/cancelled/:page?" element={<JobsPage />} />
            <Route path="/jobs/plan-ready/:page?" element={<JobsPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/analytics/reviews/:page?" element={<AnalyticsReviewsPage />} />
            <Route path="/analytics/:period" element={<AnalyticsPage />} />
            {/* Queue is shown on Jobs; keep old path as redirect */}
            <Route path="/queue" element={<Navigate to="/jobs" replace />} />
            <Route path="/jobs/:jobId/:section?" element={<JobsAtJobOrPage />} />
            <Route path="/tasks/:issueKey/:section?" element={<IssueDetailPage />} />
            <Route path="/poll" element={<PollPage />} />
            <Route path="/scheduled/:mode?/:tracker?/:page?" element={<SchedulesPage />} />
            <Route path="/schedules" element={<Navigate to="/scheduled/jira" replace />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/sessions/:workspaceId" element={<SessionsAtId />} />
            <Route path="/storage" element={<StoragePage />} />
            <Route path="/settings/:section?" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Route>
        </Routes>
      </LiveProvider>
    </BrowserRouter>
    </DashboardAuthGate>
  )
}
