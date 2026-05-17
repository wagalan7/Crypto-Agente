import { Routes, Route, Navigate, useParams, Outlet } from 'react-router-dom'
import { useState, useEffect } from 'react'
import { useAuth } from './context/AuthContext'
import { LoginPage } from './pages/LoginPage'
import { SignupPage } from './pages/SignupPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { BillingPage } from './pages/BillingPage'
import { ClientsPage } from './pages/ClientsPage'
import { DashboardPage } from './pages/DashboardPage'
import { CalendarPage } from './pages/CalendarPage'
import { ContentPage } from './pages/ContentPage'
import { AgentsPage } from './pages/AgentsPage'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { SocialAccountsPage } from './pages/SocialAccountsPage'
import { PersonaPage } from './pages/PersonaPage'
import { InspirationsPage } from './pages/InspirationsPage'
import { ProductsPage } from './pages/ProductsPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { CentralEstrategicaPage } from './pages/CentralEstrategicaPage'
import { Sidebar } from './components/Sidebar'
import { api } from './services/api'

function RequireAuth({ children, skipOnboardingCheck = false }: { children: JSX.Element; skipOnboardingCheck?: boolean }) {
  const { user, loading } = useAuth()
  if (loading) return <div className="min-h-screen bg-gray-950 flex items-center justify-center text-gray-400 text-sm">Carregando...</div>
  if (!user) return <Navigate to="/login" replace />
  if (!skipOnboardingCheck && user.onboarding_completed === false) {
    return <Navigate to="/onboarding" replace />
  }
  return children
}

function ClientLayout() {
  const { clientId } = useParams<{ clientId: string }>()
  const [clientName, setClientName] = useState<string>()

  useEffect(() => {
    if (clientId) {
      api.clients.get(Number(clientId)).then((c: any) => setClientName(c.name)).catch(() => {})
    }
  }, [clientId])

  return (
    <div className="flex min-h-screen">
      <Sidebar clientName={clientName} />
      <main className="flex-1 overflow-auto pt-12 pb-20 md:pt-0 md:pb-0">
        <Outlet />
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/signup" element={<SignupPage />} />
      <Route path="/onboarding" element={<RequireAuth skipOnboardingCheck><OnboardingPage /></RequireAuth>} />
      <Route path="/billing" element={<RequireAuth skipOnboardingCheck><BillingPage /></RequireAuth>} />
      <Route path="/" element={<RequireAuth><ClientsPage /></RequireAuth>} />
      <Route path="/client/:clientId" element={<RequireAuth><ClientLayout /></RequireAuth>}>
        <Route index element={<DashboardPage />} />
        <Route path="calendar" element={<CalendarPage />} />
        <Route path="content" element={<ContentPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="analytics" element={<AnalyticsPage />} />
        <Route path="social" element={<SocialAccountsPage />} />
        <Route path="persona" element={<PersonaPage />} />
        <Route path="inspirations" element={<InspirationsPage />} />
        <Route path="products" element={<ProductsPage />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route path="strategy" element={<CentralEstrategicaPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
