import { useState } from 'react'
import { MagaLogo } from './MagaLogo'

interface Props {
  onLogin: (token: string) => void
}

export function LoginPage({ onLogin }: Props) {
  const [user, setUser]       = useState('')
  const [pass, setPass]       = useState('')
  const [error, setError]     = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user, password: pass }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Erro ao entrar')
      onLogin(data.token)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Erro desconhecido')
    }
    setLoading(false)
  }

  return (
    <div className="min-h-screen bg-[#070711] flex items-center justify-center px-4">
      {/* Background glow */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className="absolute top-1/3 left-1/2 -translate-x-1/2 w-[500px] h-[500px] rounded-full bg-[#3A3591]/15 blur-3xl" />
        <div className="absolute bottom-1/4 left-1/4 w-64 h-64 rounded-full bg-violet-900/10 blur-3xl" />
      </div>

      <div className="relative w-full max-w-sm">
        {/* Logo + nome */}
        <div className="text-center mb-8">
          <div className="flex justify-center mb-4">
            <MagaLogo size={72} className="rounded-2xl shadow-2xl shadow-[#3A3591]/40" />
          </div>
          <h1 className="text-2xl font-bold text-white tracking-tight">Maga One</h1>
          <p className="text-xs text-gray-500 mt-1">Marketing Intelligence · Acesso restrito</p>
        </div>

        {/* Formulário */}
        <form
          onSubmit={handleSubmit}
          className="bg-gray-900/60 border border-gray-800 rounded-2xl p-6 space-y-4 backdrop-blur"
        >
          <div>
            <label className="block text-[10px] text-gray-500 mb-1.5 tracking-widest uppercase">
              Usuário
            </label>
            <input
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm text-gray-100
                         placeholder-gray-600 focus:outline-none focus:border-[#3A3591] focus:ring-1 focus:ring-[#3A3591]/60 transition-colors"
              placeholder="seu@email.com"
              value={user}
              onChange={e => setUser(e.target.value)}
              autoFocus
              disabled={loading}
            />
          </div>

          <div>
            <label className="block text-[10px] text-gray-500 mb-1.5 tracking-widest uppercase">
              Senha
            </label>
            <input
              type="password"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-sm text-gray-100
                         placeholder-gray-600 focus:outline-none focus:border-[#3A3591] focus:ring-1 focus:ring-[#3A3591]/60 transition-colors"
              placeholder="••••••••"
              value={pass}
              onChange={e => setPass(e.target.value)}
              disabled={loading}
            />
          </div>

          {error && (
            <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading || !user || !pass}
            className="w-full py-3 rounded-xl font-semibold text-sm transition-all mt-2
              text-white shadow-lg"
            style={{
              background: loading || !user || !pass
                ? '#1f2937'
                : 'linear-gradient(135deg, #3A3591 0%, #5b4fcf 100%)',
              color: loading || !user || !pass ? '#4b5563' : '#fff',
              boxShadow: (!loading && user && pass) ? '0 4px 24px rgba(58,53,145,0.4)' : 'none',
            }}
          >
            {loading
              ? (
                <span className="flex items-center justify-center gap-2">
                  <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  Entrando...
                </span>
              )
              : 'Entrar'}
          </button>
        </form>

        <p className="text-center text-[10px] text-gray-700 mt-4">
          Acesso autorizado apenas para usuários cadastrados
        </p>
      </div>
    </div>
  )
}
