import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { SocialAccount } from '../types'

type Platform = 'instagram' | 'facebook'

const PLATFORM_LABELS: Record<Platform, string> = {
  instagram: 'Instagram',
  facebook: 'Facebook',
}

const PLATFORM_HINTS: Record<Platform, string> = {
  instagram: 'IG Business Account ID (numérico, ex: 17841405...). Token: Page Access Token de longa duração.',
  facebook: 'Facebook Page ID (numérico). Token: Page Access Token de longa duração.',
}

export function SocialAccountsPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [accounts, setAccounts] = useState<SocialAccount[]>([])
  const [editing, setEditing] = useState<Platform | null>(null)
  const [form, setForm] = useState({ account_id: '', account_name: '', access_token: '' })
  const [saving, setSaving] = useState(false)
  const [testResult, setTestResult] = useState<Record<number, { ok: boolean; msg: string }>>({})

  async function load() {
    const data: any = await api.social.list(id)
    setAccounts(data)
  }

  useEffect(() => { load() }, [id])

  function startEdit(platform: Platform) {
    const existing = accounts.find(a => a.platform === platform)
    setEditing(platform)
    setForm({
      account_id: existing?.account_id || '',
      account_name: existing?.account_name || '',
      access_token: '',
    })
  }

  async function save() {
    if (!editing) return
    if (!form.account_id || !form.access_token) {
      alert('Account ID e Access Token são obrigatórios')
      return
    }
    setSaving(true)
    try {
      await api.social.upsert({
        client_id: id,
        platform: editing,
        account_id: form.account_id.trim(),
        account_name: form.account_name.trim() || undefined,
        access_token: form.access_token.trim(),
      })
      setEditing(null)
      setForm({ account_id: '', account_name: '', access_token: '' })
      await load()
    } catch (e: any) {
      alert('Erro ao salvar: ' + e.message)
    } finally {
      setSaving(false)
    }
  }

  async function testConnection(accountId: number) {
    setTestResult(prev => ({ ...prev, [accountId]: { ok: false, msg: 'Testando...' } }))
    try {
      const res: any = await api.social.test(accountId)
      setTestResult(prev => ({ ...prev, [accountId]: { ok: res.ok, msg: res.message } }))
      await load()
    } catch (e: any) {
      setTestResult(prev => ({ ...prev, [accountId]: { ok: false, msg: e.message } }))
    }
  }

  async function disconnect(accountId: number) {
    if (!confirm('Remover esta conexão? Você precisará reconfigurar o token para publicar novamente.')) return
    await api.social.remove(accountId)
    await load()
  }

  return (
    <div className="p-4 md:p-6 max-w-3xl">
      <h1 className="text-lg font-bold text-white mb-1">Contas Sociais</h1>
      <p className="text-xs text-gray-500 mb-6">
        Conecte as contas de cada cliente para publicação automática. Os tokens são armazenados criptografados.
      </p>

      <div className="space-y-3">
        {(['instagram', 'facebook'] as Platform[]).map(platform => {
          const acc = accounts.find(a => a.platform === platform)
          const isEditing = editing === platform
          const result = acc ? testResult[acc.id] : null

          return (
            <div key={platform} className="card">
              <div className="flex items-start justify-between gap-2 mb-2">
                <div>
                  <h2 className="font-semibold text-white text-sm">{PLATFORM_LABELS[platform]}</h2>
                  <p className="text-[11px] text-gray-500 mt-0.5">{PLATFORM_HINTS[platform]}</p>
                </div>
                {acc && !isEditing && (
                  <span className={`badge text-[10px] ${acc.is_active ? 'bg-green-900/40 text-green-300' : 'bg-gray-700 text-gray-400'}`}>
                    {acc.is_active ? 'Conectado' : 'Desativado'}
                  </span>
                )}
              </div>

              {acc && !isEditing && (
                <div className="space-y-1.5 mb-3 text-xs">
                  <div className="flex gap-2">
                    <span className="text-gray-500">Account ID:</span>
                    <span className="text-gray-300 font-mono">{acc.account_id}</span>
                  </div>
                  {acc.account_name && (
                    <div className="flex gap-2">
                      <span className="text-gray-500">Nome:</span>
                      <span className="text-gray-300">{acc.account_name}</span>
                    </div>
                  )}
                  <div className="flex gap-2">
                    <span className="text-gray-500">Token:</span>
                    <span className="text-gray-400 font-mono">{acc.access_token_preview}</span>
                  </div>
                  {acc.last_error && (
                    <p className="text-red-400 text-[11px] mt-2">⚠ {acc.last_error}</p>
                  )}
                  {result && (
                    <p className={`text-[11px] mt-2 ${result.ok ? 'text-green-400' : 'text-red-400'}`}>
                      {result.ok ? '✓' : '✗'} {result.msg}
                    </p>
                  )}
                </div>
              )}

              {isEditing && (
                <div className="space-y-2 mb-3">
                  <div>
                    <label className="text-[11px] text-gray-400 mb-1 block">Account ID *</label>
                    <input
                      className="input-field font-mono text-xs"
                      value={form.account_id}
                      onChange={e => setForm(p => ({ ...p, account_id: e.target.value }))}
                      placeholder="17841405..."
                    />
                  </div>
                  <div>
                    <label className="text-[11px] text-gray-400 mb-1 block">Nome para identificação</label>
                    <input
                      className="input-field text-xs"
                      value={form.account_name}
                      onChange={e => setForm(p => ({ ...p, account_name: e.target.value }))}
                      placeholder="@thiago.fitness"
                    />
                  </div>
                  <div>
                    <label className="text-[11px] text-gray-400 mb-1 block">Access Token *</label>
                    <textarea
                      className="input-field font-mono text-[10px] min-h-[80px]"
                      value={form.access_token}
                      onChange={e => setForm(p => ({ ...p, access_token: e.target.value }))}
                      placeholder="EAAG..."
                    />
                  </div>
                </div>
              )}

              <div className="flex gap-2 flex-wrap">
                {!isEditing ? (
                  <>
                    <button onClick={() => startEdit(platform)} className="btn-primary w-auto px-4 py-1.5 text-xs">
                      {acc ? 'Atualizar token' : 'Conectar'}
                    </button>
                    {acc && (
                      <>
                        <button onClick={() => testConnection(acc.id)} className="btn-secondary px-4 py-1.5 text-xs">
                          Testar conexão
                        </button>
                        <button onClick={() => disconnect(acc.id)} className="btn-secondary px-4 py-1.5 text-xs text-red-400 border-red-900/50">
                          Desconectar
                        </button>
                      </>
                    )}
                  </>
                ) : (
                  <>
                    <button onClick={save} disabled={saving} className="btn-primary w-auto px-4 py-1.5 text-xs">
                      {saving ? 'Salvando...' : 'Salvar'}
                    </button>
                    <button onClick={() => setEditing(null)} className="btn-secondary px-4 py-1.5 text-xs">
                      Cancelar
                    </button>
                  </>
                )}
              </div>
            </div>
          )
        })}
      </div>

      <div className="card mt-6 bg-violet-900/10 border-violet-800/40">
        <h3 className="font-semibold text-violet-300 text-sm mb-2">Como obter o token</h3>
        <ol className="text-xs text-gray-400 space-y-1.5 list-decimal list-inside">
          <li>Acesse <a href="https://developers.facebook.com/tools/explorer/" target="_blank" rel="noreferrer" className="text-violet-400 underline">Graph API Explorer</a></li>
          <li>Selecione sua App + Página → gere Page Access Token</li>
          <li>Em <a href="https://developers.facebook.com/tools/debug/accesstoken/" target="_blank" rel="noreferrer" className="text-violet-400 underline">Access Token Debugger</a>, troque por um token de longa duração (60 dias)</li>
          <li>O <strong>Account ID</strong> do Instagram aparece em <code className="text-violet-300">/me/accounts?fields=instagram_business_account</code></li>
        </ol>
      </div>
    </div>
  )
}
