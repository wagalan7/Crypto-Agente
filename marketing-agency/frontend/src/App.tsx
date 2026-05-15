import { Routes, Route, useParams, Outlet } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { ClientsPage } from './pages/ClientsPage'
import { DashboardPage } from './pages/DashboardPage'
import { CalendarPage } from './pages/CalendarPage'
import { ContentPage } from './pages/ContentPage'
import { AgentsPage } from './pages/AgentsPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { Sidebar } from './components/Sidebar'
import { api } from './services/api'

function ClientLayout() {
  const { clientId } = useParams<{ clientId: string }>()
  const [clientName, setClientName] = useState<string>()

  useEffect(() => {
    if (clientId) {
      api.clients.get(Number(clientId)).then((c: any) => setClientName(c.name))
    }
  }, [clientId])

  return (
    <div className="flex min-h-screen">
      <Sidebar clientName={clientName} />
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<ClientsPage />} />
      <Route path="/client/:clientId" element={<ClientLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="content" element={<ContentPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
      </Route>
    </Routes>
  )
}
