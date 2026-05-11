import { useState, useEffect, useCallback } from 'react'

interface User { user: string; role: string; name: string }

interface Props {
  authHeaders: Record<string, string>
  currentUser: string
}

export function UsersPanel({ authHeaders, currentUser }: Props) {
  const [users, setUsers]     = useState<User[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState('')
  const [success, setSuccess] = useState('')
  const [editing, setEditing] = useState<string | null>(null)

  const [newUser, setNewUser] = useState({ username: '', password: '', name: '', role: 'user' })
  const [editForm, setEditForm] = useState<Record<string, {
    name: string; new_username: string; new_password: string
  }>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch('/auth/users', { headers: authHeaders })
      if (res.ok) {
        const data: User[] = await res.json()
        setUsers(data)
        const forms: typeof editForm = {}
        data.forEach(u => {
          forms[u.user] = { name: u.name || '', new_username: u.user, new_password: '' }
        })
        setEditForm(forms)
      }
    } catch { /* ignore */ }
    setLoading(false)
  }, [authHeaders])

  useEffect(() => { load() }, [load])

  const flash = (msg: string, isErr = false) => {
    if (isErr) { setError(msg); setTimeout(() => setError(''), 4000) }
    else       { setSuccess(msg); setTimeout(() => setSuccess(''), 3000) }
  }

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newUser.username || !newUser.password) return
    try {
      const res = await fetch('/auth/users', {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({
          username: newUser.username, password: newUser.password,
          role: newUser.role, name: newUser.name,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail)
      setNewUser({ username: '', password: '', name: '', role: 'user' })
      flash('Usuário criado com sucesso')
      load()
    } catch (err: unknown) {
      flash(err instanceof Error ? err.message : 'Erro', true)
    }
  }

  const handleSaveEdit = async (originalUsername: string) => {
    const form = editForm[originalUsername]
    if (!form) return
    const original = users.find(u => u.user === originalUsername)
    try {
      const body: Record<string, string> = {}
      if (form.name !== (original?.name || ''))  body.name         = form.name
      if (form.new_username !== originalUsername) body.new_username = form.new_username
      if (form.new_password)                      body.new_password = form.new_password
      if (Object.keys(body).length === 0) { setEditing(null); return }

      const res = await fetch(`/auth/users/${encodeURIComponent(originalUsername)}`, {
        method: 'PATCH', headers: authHeaders, body: JSON.stringify(body),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail)
      flash('Usuário atualizado')
      setEditing(null)
      load()
    } catch (err: unknown) {
      flash(err instanceof Error ? err.message : 'Erro', true)
    }
  }

  const handleDelete = async (username: string) => {
    if (!confirm(`Remover ${username}?`)) return
    try {
      const res = await fetch(`/auth/users/${encodeURIComponent(username)}`, {
        method: 'DELETE', headers: authHeaders,
      })
      if (!res.ok) throw new Error((await res.json()).detail)
      flash('Usuário removido')
      load()
    } catch (err: unknown) {
      flash(err instanceof Error ? err.message : 'Erro', true)
    }
  }

  const initials = (u: User) => {
    const n = u.name || u.user
    return n.split(/[\s@]/).map((w: string) => w[0]).join('').toUpperCase().slice(0, 2)
  }

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl p-5 space-y-5">
      <div className="flex items-center gap-2">
        <div className="w-1.5 h-4 rounded-sm bg-gradient-to-b from-violet-500 to-blue-500" />
        <h2 className="text-sm font-bold text-gray-200 tracking-wide">USUÁRIOS</h2>
      </div>

      {error   && <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">{error}</div>}
      {success && <div className="px-3 py-2 bg-emerald-900/30 border border-emerald-800 rounded-lg text-xs text-emerald-400">{success}</div>}

      {/* Lista */}
      <div className="space-y-2">
        {loading ? (
          <p className="text-xs text-gray-600">Carregando...</p>
        ) : users.map(u => (
          <div key={u.user} className="border border-gray-700 rounded-lg overflow-hidden">

            {/* Linha */}
            <div className="flex items-center justify-between px-3 py-2.5 bg-gray-800/50">
              <div className="flex items-center gap-2.5">
                <div className="w-9 h-9 rounded-full flex-shrink-0 flex items-center justify-center
                                text-white text-xs font-bold bg-gradient-to-br from-[#3A3591] to-violet-500">
                  {initials(u)}
                </div>
                <div>
                  {u.name && <p className="text-xs font-semibold text-gray-100">{u.name}</p>}
                  <p className={u.name ? 'text-[10px] text-gray-500' : 'text-xs text-gray-200 font-medium'}>
                    {u.user}
                  </p>
                  <p className="text-[9px] text-gray-600 mt-0.5">
                    {u.role === 'admin' ? '⭐ admin' : 'usuário'}
                  </p>
                </div>
              </div>

              <div className="flex items-center gap-1.5">
                {u.user === currentUser && (
                  <span className="text-[9px] text-gray-600 mr-1">você</span>
                )}
                <button
                  onClick={() => setEditing(editing === u.user ? null : u.user)}
                  className={`text-[10px] px-2 py-1 rounded border transition-colors
                    ${editing === u.user
                      ? 'text-violet-300 bg-violet-900/30 border-violet-800'
                      : 'text-gray-500 border-gray-700 hover:text-gray-300 hover:border-gray-600'}`}
                >
                  {editing === u.user ? '▲ fechar' : '✎ editar'}
                </button>
                {u.user !== currentUser && (
                  <button
                    onClick={() => handleDelete(u.user)}
                    className="text-[10px] text-red-500 hover:text-red-400 px-2 py-1 rounded
                               border border-transparent hover:border-red-900/50 hover:bg-red-900/20 transition-colors"
                  >
                    remover
                  </button>
                )}
              </div>
            </div>

            {/* Edição inline */}
            {editing === u.user && editForm[u.user] && (
              <div className="border-t border-gray-700 px-3 py-3 bg-gray-900/60 space-y-3">
                <p className="text-[9px] text-gray-600 uppercase tracking-widest">Editar dados</p>

                <div>
                  <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">Nome de exibição</label>
                  <input
                    className="w-full bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5 text-[11px] text-gray-200
                               placeholder-gray-600 focus:outline-none focus:border-violet-500"
                    placeholder="Ex: João Silva"
                    value={editForm[u.user].name}
                    onChange={e => setEditForm(f => ({ ...f, [u.user]: { ...f[u.user], name: e.target.value } }))}
                  />
                </div>

                <div>
                  <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">Usuário (login)</label>
                  <input
                    className="w-full bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5 text-[11px] text-gray-200
                               placeholder-gray-600 focus:outline-none focus:border-violet-500"
                    placeholder="email ou usuário"
                    value={editForm[u.user].new_username}
                    onChange={e => setEditForm(f => ({ ...f, [u.user]: { ...f[u.user], new_username: e.target.value } }))}
                  />
                </div>

                <div>
                  <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">
                    Nova senha <span className="text-gray-700 normal-case">(vazio = não alterar)</span>
                  </label>
                  <input
                    type="password"
                    className="w-full bg-gray-800 border border-gray-700 rounded-md px-2.5 py-1.5 text-[11px] text-gray-200
                               placeholder-gray-600 focus:outline-none focus:border-violet-500"
                    placeholder="••••••••"
                    value={editForm[u.user].new_password}
                    onChange={e => setEditForm(f => ({ ...f, [u.user]: { ...f[u.user], new_password: e.target.value } }))}
                  />
                </div>

                <div className="flex gap-2">
                  <button
                    onClick={() => handleSaveEdit(u.user)}
                    className="flex-1 py-1.5 rounded-md text-[11px] font-semibold text-white transition-all
                      bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600"
                  >
                    Salvar alterações
                  </button>
                  <button
                    onClick={() => setEditing(null)}
                    className="px-3 py-1.5 rounded-md text-[11px] text-gray-500 hover:text-gray-300
                      border border-gray-700 hover:border-gray-600 transition-all"
                  >
                    Cancelar
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Novo usuário */}
      <form onSubmit={handleAdd} className="border-t border-gray-800 pt-4 space-y-3">
        <p className="text-[10px] text-gray-500 tracking-widest uppercase">Adicionar Usuário</p>

        <div>
          <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">Nome de exibição</label>
          <input
            className="input-field text-xs"
            placeholder="Ex: João Silva"
            value={newUser.name}
            onChange={e => setNewUser(n => ({ ...n, name: e.target.value }))}
          />
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">Usuário (login)</label>
            <input
              className="input-field text-xs"
              placeholder="email ou usuário"
              value={newUser.username}
              onChange={e => setNewUser(n => ({ ...n, username: e.target.value }))}
            />
          </div>
          <div>
            <label className="block text-[9px] text-gray-600 mb-0.5 uppercase tracking-wider">Senha</label>
            <input
              type="password"
              className="input-field text-xs"
              placeholder="senha"
              value={newUser.password}
              onChange={e => setNewUser(n => ({ ...n, password: e.target.value }))}
            />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <select
            className="input-field text-xs flex-1"
            value={newUser.role}
            onChange={e => setNewUser(n => ({ ...n, role: e.target.value }))}
          >
            <option value="user">Usuário</option>
            <option value="admin">Admin</option>
          </select>
          <button
            type="submit"
            disabled={!newUser.username || !newUser.password}
            className="px-4 py-2 rounded-lg text-xs font-semibold
              bg-gradient-to-r from-violet-600 to-blue-600 hover:from-violet-500 hover:to-blue-500
              disabled:from-gray-800 disabled:to-gray-800 disabled:text-gray-600
              text-white transition-all"
          >
            + Criar
          </button>
        </div>
      </form>
    </div>
  )
}
