import { createContext, useContext, useState, useEffect, ReactNode } from 'react'
import { saveSession, clearSession, getToken, getStoredUser, type AuthUser } from '../services/auth'
import { api } from '../services/api'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
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
      }).catch(() => {
        clearSession()
        setUser(null)
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

  function logout() {
    clearSession()
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
