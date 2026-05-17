import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { saveSession, clearSession, getToken, getStoredUser, type AuthUser } from '../services/auth'
import { api } from '../services/api'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  signup: (email: string, password: string, name?: string) => Promise<void>
  refreshUser: () => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue>(null!)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(getStoredUser)
  const [loading, setLoading] = useState(!!getToken())

  useEffect(() => {
    if (getToken() && !user) {
      api.auth.me().then((u: any) => {
        setUser(u)
        saveSession(getToken()!, u)
      }).catch((err) => {
        // Only clear session on auth failure (401 throws "Não autenticado").
        // Other errors (network, server) shouldn't kick the user out.
        if (err?.message === 'Não autenticado') {
          clearSession()
          setUser(null)
        } else {
          console.error('auth.me failed:', err)
        }
      }).finally(() => setLoading(false))
    } else {
      setLoading(false)
    }

    const onLogout = () => { clearSession(); setUser(null) }
    window.addEventListener('auth:logout', onLogout)
    return () => window.removeEventListener('auth:logout', onLogout)
  }, [])

  async function login(email: string, password: string) {
    const res: any = await api.auth.login(email, password)
    saveSession(res.access_token, res.user)
    setUser(res.user)
  }

  async function signup(email: string, password: string, name?: string) {
    const res: any = await api.auth.signup(email, password, name)
    saveSession(res.access_token, res.user)
    setUser(res.user)
  }

  async function refreshUser() {
    const u: any = await api.auth.me()
    setUser(u)
    if (getToken()) saveSession(getToken()!, u)
  }

  function logout() {
    clearSession()
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, refreshUser, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
