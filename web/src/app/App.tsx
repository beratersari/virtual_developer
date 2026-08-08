import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { IssueDetailPage } from '../pages/issues/IssueDetailPage'
import { JobDetailPage } from '../pages/jobs/JobDetailPage'
import { JobsPage } from '../pages/jobs/JobsPage'
import { PollPage } from '../pages/poll/PollPage'
import { SchedulesPage } from '../pages/schedules/SchedulesPage'
import { SessionsPage } from '../pages/sessions/SessionsPage'
import { SettingsPage } from '../pages/settings/SettingsPage'
import { LiveProvider } from './LiveProvider'
import { Shell } from './Shell'

export default function App() {
  return (
    <BrowserRouter>
      <LiveProvider>
        <Routes>
          <Route element={<Shell />}>
            <Route path="/" element={<Navigate to="/jobs" replace />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/jobs/:jobId" element={<JobDetailPage />} />
            <Route path="/tasks/:issueKey" element={<IssueDetailPage />} />
            <Route path="/poll" element={<PollPage />} />
            <Route path="/scheduled" element={<SchedulesPage />} />
            <Route path="/schedules" element={<Navigate to="/scheduled" replace />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/jobs" replace />} />
          </Route>
        </Routes>
      </LiveProvider>
    </BrowserRouter>
  )
}
