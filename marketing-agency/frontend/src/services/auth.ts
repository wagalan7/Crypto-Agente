const TOKEN_KEY = 'contentai_token'
const USER_KEY = 'contentai_user'

export interface PlanStatus {
  tier: 'free' | 'pro' | 'agency'
  label: string
  price_brl_cents: number
  limits: { max_clients: number; max_posts_per_month: number }
  features: { auto_publish: boolean; pdf_report: boolean; voice_scorer: boolean; trends: boolean }
  trialing: boolean
  trial_ends_at: string | null
  stripe_customer_id: string | null
  status: string
}

export interface AuthUser {
  id: number
  email: string
  name: string
  role: 'master' | 'admin' | 'user'
  onboarding_completed?: boolean
  plan?: PlanStatus
}

export function saveSession(token: string, user: AuthUser) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getStoredUser(): AuthUser | null {
  const raw = localStorage.getItem(USER_KEY)
  return raw ? JSON.parse(raw) : null
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}
