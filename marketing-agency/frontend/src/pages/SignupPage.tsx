import { useState, FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export function SignupPage() {
  const { signup } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPass, setShowPass] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (password.length < 8) { setError('Senha precisa de ao menos 8 caracteres'); return }
    setLoading(true)
    try {
      await signup(email.trim(), password, name.trim() || undefined)
      navigate('/onboarding', { replace: true })
    } catch (err: any) {
      try { const body = JSON.parse(err.message); setError(body.detail || 'Erro ao cadastrar') }
      catch { setError(err.message || 'Erro ao cadastrar') }
    } finally { setLoading(false) }
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-14 h-14 rounded-2xl bg-violet-600 flex items-center justify-center text-white font-bold text-2xl mb-3 shadow-lg shadow-violet-900/40">A</div>
          <h1 className="text-xl font-bold text-white">Comece grátis</h1>
          <p className="text-sm text-gray-500 mt-1">7 dias do plano Pro liberados na hora</p>
        </div>

        <form onSubmit={submit} className="card space-y-4">
          <div>
            <label className="text-xs text-gray-400 mb-1.5 block">Nome (opcional)</label>
            <input type="text" className="input-field" placeholder="Seu nome"
              value={name} onChange={e => setName(e.target.value)} autoComplete="name" />
          </div>
          <div>
            <label className="text-xs text-gray-400 mb-1.5 block">Email</label>
            <input type="email" className="input-field" placeholder="seu@email.com"
              value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" required />
          </div>
          <div>
            <label className="text-xs text-gray-400 mb-1.5 block">Senha (mín. 8 caracteres)</label>
            <div className="relative">
              <input type={showPass ? 'text' : 'password'} className="input-field pr-10"
                placeholder="••••••••" value={password} onChange={e => setPassword(e.target.value)}
                autoComplete="new-password" required minLength={8} />
              <button type="button" onClick={() => setShowPass(v => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 text-xs">
                {showPass ? 'ocultar' : 'ver'}
              </button>
            </div>
          </div>

          {error && (
            <div className="bg-red-900/30 border border-red-700 rounded-lg px-3 py-2">
              <p className="text-xs text-red-400">{error}</p>
            </div>
          )}

          <button type="submit" disabled={loading || !email || !password}
            className="btn-primary w-full py-3 text-sm font-semibold">
            {loading ? 'Criando...' : 'Criar conta grátis'}
          </button>
        </form>
        <p className="text-center text-xs text-gray-500 mt-4">
          Já tem conta? <Link to="/login" className="text-violet-400 hover:text-violet-300">Entrar</Link>
        </p>
      </div>
    </div>
  )
}
