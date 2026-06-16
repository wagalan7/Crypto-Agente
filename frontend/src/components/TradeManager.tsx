import { useState, useEffect, useCallback, useRef } from 'react'
import { X, Plus, TrendingUp, TrendingDown, Bell, Bot, User, RefreshCw } from 'lucide-react'
import { api } from '../services/api'
import { roundRR } from '../utils/rr'
import type { TradeSignal, RealTradeRow } from '../types'

// ─── Helpers ────────────────────────────────────────────────────────────────

// R:R da posição a partir dos níveis (entry/SL/TP1). null se faltar dado.
function rrFromLevels(
  side: string,
  entry: number | null | undefined,
  sl: number | null | undefined,
  tp1: number | null | undefined,
): number | null {
  if (!entry || sl == null || tp1 == null) return null
  const risk = side === 'long' ? entry - sl : sl - entry
  const reward = side === 'long' ? tp1 - entry : entry - tp1
  if (risk <= 0 || reward <= 0) return null
  return reward / risk
}

function toBinance(symbol: string): string {
  return symbol.split(':')[0].replace('/', '')  // 'BTC/USDT:USDT' → 'BTCUSDT'
}

function baseAsset(symbol: string): string {
  return symbol.split('/')[0].split(':')[0]
}

function fmtPrice(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return '—'
  if (p >= 1000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 2 })
  if (p >= 1) return p.toFixed(4)
  return p.toFixed(6)
}

function pnlPct(side: string, entry: number, current: number | undefined): number | null {
  if (!current || !entry) return null
  const diff = side === 'long' ? (current - entry) / entry : (entry - current) / entry
  return diff * 100
}

// Trades do próprio bot (origem automática).
const isBot = (source: string) => source === 'auto' || source === 'shadow'
// Entrada SUA que o bot gerencia (coloca bracket + move BE pós-TP1).
const isManaged = (source: string) => source === 'managed'
// Gerido pelo bot (read-only no painel) — cobre auto e managed.
const isBotManaged = (source: string) => isBot(source) || isManaged(source)

interface Props {
  onClose: () => void
  onSelectSymbol?: (symbol: string) => void
  initialSignal?: TradeSignal | null
}

export default function TradeManager({ onClose, onSelectSymbol, initialSignal }: Props) {
  const [trades, setTrades] = useState<RealTradeRow[]>([])
  const [prices, setPrices] = useState<Record<string, number>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showAddForm, setShowAddForm] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [toast, setToast] = useState<{ msg: string; level: 'ok' | 'err' } | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Form: confirmar entrada manual (níveis da rec/chart; qty opcional = automático)
  const [form, setForm] = useState({
    symbol: 'BTC/USDT:USDT', timeframe: '1h', direction: 'long',
    entry: '', qty: '', leverage: '', stopLoss: '', tp1: '', tp2: '',
  })

  const flash = useCallback((msg: string, level: 'ok' | 'err' = 'ok') => {
    setToast({ msg, level })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 4000)
  }, [])

  // ── Carrega operações ativas (status=open) do backend, a cada 10s ──────────
  const load = useCallback(async () => {
    try {
      const { trades: rows } = await api.listRealTrades({ status: 'open', days: 90 })
      setTrades(rows)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'falha ao carregar')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 10_000)
    return () => clearInterval(id)
  }, [load])

  // ── Poll de preço (Binance fapi, IP do browser) só pros símbolos ativos ────
  useEffect(() => {
    const symbols = Array.from(new Set(trades.map(t => t.symbol)))
    if (symbols.length === 0) return
    const poll = async () => {
      const updates: Record<string, number> = {}
      await Promise.all(symbols.map(async sym => {
        try {
          const r = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${toBinance(sym)}`, {
            signal: AbortSignal.timeout(8000),
          })
          const data = await r.json()
          const price = parseFloat(data.price)
          if (price) updates[sym] = price
        } catch { /* ignora símbolo */ }
      }))
      if (Object.keys(updates).length) setPrices(prev => ({ ...prev, ...updates }))
    }
    poll()
    const id = setInterval(poll, 8000)
    return () => clearInterval(id)
  }, [trades])

  // ── Pré-preenche o form quando vem sinal do chart ──────────────────────────
  useEffect(() => {
    if (!initialSignal) return
    setForm({
      symbol: initialSignal.symbol,
      timeframe: initialSignal.timeframe,
      direction: initialSignal.direction === 'long' ? 'long' : 'short',
      entry: initialSignal.entry?.toString() ?? '',
      qty: '',
      leverage: '',
      stopLoss: initialSignal.stop_loss?.toString() ?? '',
      tp1: initialSignal.tp1?.toString() ?? '',
      tp2: initialSignal.tp2?.toString() ?? '',
    })
    setShowAddForm(true)
  }, [initialSignal])

  // ── Confirmar entrada (POST /real-trades/from-recommendation) ──────────────
  const submitEntry = async () => {
    const entry = parseFloat(form.entry)
    if (!entry || !form.symbol) {
      flash('Informe ao menos símbolo e preço de entrada', 'err')
      return
    }
    setSubmitting(true)
    try {
      const res = await api.confirmEntry({
        symbol: form.symbol,
        side: form.direction,
        entry_price: entry,
        qty: form.qty ? parseFloat(form.qty) : null,
        timeframe: form.timeframe || undefined,
        leverage: form.leverage ? parseInt(form.leverage, 10) : null,
        planned_stop: form.stopLoss ? parseFloat(form.stopLoss) : null,
        planned_tp1: form.tp1 ? parseFloat(form.tp1) : null,
        planned_tp2: form.tp2 ? parseFloat(form.tp2) : null,
      })
      const prot = res.protection
      const protMsg = prot?.placed
        ? '· bot colocou SL+TP1+TP2'
        : prot?.error
          ? `· ⚠ bracket não criado (${prot.error})`
          : '· ⚠ bracket pendente (auto-cura vai tentar)'
      flash(`Entrada confirmada · qty ${res.qty_source} (${fmtPrice(res.qty)}) ${protMsg}`, prot?.placed ? 'ok' : 'err')
      setShowAddForm(false)
      setForm(f => ({ ...f, entry: '', qty: '' }))
      await load()
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'erro'
      flash(msg.includes('422') ? 'Sem posição aberta na conta — abra na corretora ou informe o qty' : `Falha: ${msg}`, 'err')
    } finally {
      setSubmitting(false)
    }
  }

  // ── Fechar manualmente (só trades manuais — auto é gerido pelo bot) ─────────
  const closeManual = async (t: RealTradeRow) => {
    const current = prices[t.symbol]
    const exit = current || t.entry_price
    setTrades(prev => prev.filter(x => x.id !== t.id))  // otimista
    try {
      await api.closeRealTrade(t.id, { exit_price: exit, status: 'closed_manual', notes: 'fechado pelo painel' })
      flash(`${baseAsset(t.symbol)} fechado @ ${fmtPrice(exit)}`, 'ok')
    } catch (e) {
      flash(`Falha ao fechar: ${e instanceof Error ? e.message : 'erro'}`, 'err')
      await load()  // reverte otimismo
    }
  }

  const activeCount = trades.length
  const botCount = trades.filter(t => isBot(t.source)).length
  const manualCount = activeCount - botCount

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex justify-end">
      <div className="w-full max-w-md bg-[#0d1320] border-l border-slate-700 flex flex-col h-full">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <Bell className="w-4 h-4 text-yellow-400" />
            <span className="font-bold text-white text-sm">Operações Ativas</span>
            <span className="text-xs bg-slate-700 text-slate-300 px-1.5 py-0.5 rounded">
              {activeCount} {activeCount === 1 ? 'aberta' : 'abertas'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              className="p-1 bg-slate-800 hover:bg-slate-700 rounded text-slate-400"
              title="Atualizar"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={() => setShowAddForm(v => !v)}
              className="flex items-center gap-1 px-2 py-1 bg-blue-600 hover:bg-blue-500 rounded text-xs font-semibold"
            >
              <Plus className="w-3 h-3" /> Confirmar entrada
            </button>
            <button onClick={onClose} className="p-1 bg-slate-800 hover:bg-slate-700 rounded">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Sub-header: contagem manual/bot */}
        {activeCount > 0 && (
          <div className="flex items-center gap-3 px-4 py-1.5 border-b border-slate-800/60 text-[11px]">
            <span className="flex items-center gap-1 text-cyan-300"><User className="w-3 h-3" /> {manualCount} você</span>
            <span className="flex items-center gap-1 text-violet-300"><Bot className="w-3 h-3" /> {botCount} bot</span>
            <span className="ml-auto text-slate-600">só ativas · finalizadas vão pro Dashboard</span>
          </div>
        )}

        {/* Toast */}
        {toast && (
          <div className={`px-4 py-2 text-xs border-b ${
            toast.level === 'ok'
              ? 'bg-emerald-900/30 border-emerald-700/40 text-emerald-300'
              : 'bg-red-900/30 border-red-700/40 text-red-300'
          }`}>
            {toast.msg}
          </div>
        )}

        {/* Add / confirm form */}
        {showAddForm && (
          <div className="px-4 py-3 border-b border-slate-800 bg-slate-800/30">
            <p className="text-xs text-slate-400 font-semibold mb-2">CONFIRMAR ENTRADA (BOT GERENCIA)</p>
            <p className="text-[10px] text-slate-500 mb-2 leading-snug">
              Você abre a posição na corretora; ao confirmar, o <strong>bot coloca SL + TP1 (parcial) + TP2</strong> e
              move o stop pro break-even após o TP1 — sozinho. Deixe o <strong>qty</strong> vazio pra ler automático da posição.
            </p>
            <div className="grid grid-cols-2 gap-2">
              <input
                className="col-span-2 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-200 placeholder-slate-500"
                placeholder="Símbolo (ex: BTC/USDT:USDT)"
                value={form.symbol}
                onChange={e => setForm(f => ({ ...f, symbol: e.target.value.toUpperCase() }))}
              />
              <select
                className="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-200"
                value={form.timeframe}
                onChange={e => setForm(f => ({ ...f, timeframe: e.target.value }))}
              >
                {['5m','15m','30m','1h','4h','6h','8h','12h','1d','3d'].map(tf => (
                  <option key={tf} value={tf}>{tf}</option>
                ))}
              </select>
              <select
                className="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-200"
                value={form.direction}
                onChange={e => setForm(f => ({ ...f, direction: e.target.value }))}
              >
                <option value="long">LONG (Compra)</option>
                <option value="short">SHORT (Venda)</option>
              </select>
              {[
                { key: 'entry', label: 'Preço de entrada *' },
                { key: 'qty', label: 'Qty (vazio = auto)' },
                { key: 'stopLoss', label: 'Stop Loss' },
                { key: 'leverage', label: 'Alavancagem' },
                { key: 'tp1', label: 'TP1' },
                { key: 'tp2', label: 'TP2' },
              ].map(({ key, label }) => (
                <input
                  key={key}
                  className="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-200 placeholder-slate-500"
                  placeholder={label}
                  type="number"
                  step="any"
                  value={(form as Record<string, string>)[key]}
                  onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
                />
              ))}
            </div>
            <button
              onClick={submitEntry}
              disabled={submitting}
              className="w-full mt-2 py-1.5 bg-green-600 hover:bg-green-500 disabled:opacity-50 rounded text-xs font-bold"
            >
              {submitting ? 'Registrando…' : '✅ Confirmar entrada'}
            </button>
          </div>
        )}

        {/* Lista de operações ativas */}
        <div className="flex-1 overflow-y-auto">
          {loading && trades.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 text-slate-600">
              <RefreshCw className="w-6 h-6 mb-2 animate-spin" />
              <p className="text-sm">Carregando operações…</p>
            </div>
          )}

          {error && !loading && (
            <div className="m-3 p-3 bg-red-500/10 border border-red-500/40 rounded text-xs text-red-300">
              ⚠ {error}
            </div>
          )}

          {!loading && !error && trades.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 text-slate-600 px-6 text-center">
              <Bell className="w-8 h-8 mb-2" />
              <p className="text-sm">Nenhuma operação ativa</p>
              <p className="text-xs mt-1">Trades do bot aparecem aqui automaticamente. Os seus, clique em "Confirmar entrada".</p>
            </div>
          )}

          {trades.map(t => {
            const bot = isBot(t.source)
            const managed = isManaged(t.source)
            const botManaged = isBotManaged(t.source)
            const current = prices[t.symbol]
            const pnl = pnlPct(t.side, t.entry_price, current)
            const postTp1 = t.phase === 'post_tp1'
            return (
              <div key={t.id} className="border-b border-slate-800/60 p-3">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-2 min-w-0">
                    {t.side === 'long'
                      ? <TrendingUp className="w-4 h-4 text-green-400 flex-shrink-0" />
                      : <TrendingDown className="w-4 h-4 text-red-400 flex-shrink-0" />}
                    <div className="min-w-0">
                      <button
                        className="font-bold text-sm text-white hover:text-blue-400"
                        onClick={() => onSelectSymbol?.(t.symbol)}
                      >
                        {baseAsset(t.symbol)}/USDT
                      </button>
                      <span className={`text-xs font-bold ml-1 ${t.side === 'long' ? 'text-green-400' : 'text-red-400'}`}>
                        {t.side.toUpperCase()}
                      </span>
                    </div>
                    {/* Origem */}
                    <span className={`flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded border font-bold ${
                      bot ? 'bg-violet-500/15 text-violet-300 border-violet-500/40'
                          : 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40'
                    }`}>
                      {bot ? <Bot className="w-2.5 h-2.5" /> : <User className="w-2.5 h-2.5" />}
                      {bot ? 'BOT' : 'VOCÊ'}
                    </span>
                    {managed && (
                      <span className="flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded border bg-violet-500/15 text-violet-300 border-violet-500/40 font-semibold" title="Sua posição, mas o bot coloca o bracket e gerencia o breakeven">
                        <Bot className="w-2.5 h-2.5" /> gerenciado
                      </span>
                    )}
                    {postTp1 && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded border bg-blue-500/15 text-blue-300 border-blue-500/40 font-semibold">
                        🎯 pós-TP1 (BE)
                      </span>
                    )}
                  </div>
                  {/* Controle: fechar só nos manuais puros; geridos pelo bot são read-only */}
                  {!botManaged ? (
                    <button
                      onClick={() => closeManual(t)}
                      className="text-[10px] px-1.5 py-0.5 bg-slate-700 hover:bg-slate-600 rounded text-slate-300 flex-shrink-0"
                      title="Marcar como fechada"
                    >
                      🚪 fechar
                    </button>
                  ) : (
                    <span className="text-[9px] text-violet-400/70 flex-shrink-0" title={managed ? 'O bot coloca SL/TP e gerencia. Pra sair, feche na corretora.' : 'O bot gerencia esta posição'}>
                      {managed ? '🤖 bot gerencia' : '🤖 auto'}
                    </span>
                  )}
                </div>

                <div className="mt-2 grid grid-cols-3 gap-x-3 text-xs">
                  <div>
                    <span className="text-slate-500">Entrada</span>
                    <div className="text-yellow-400 font-mono">{fmtPrice(t.entry_price)}</div>
                  </div>
                  <div>
                    <span className="text-slate-500">Atual</span>
                    <div className={`font-mono ${pnl == null ? 'text-slate-400' : pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {fmtPrice(current)}
                    </div>
                  </div>
                  <div>
                    <span className="text-slate-500">PnL</span>
                    <div className={`font-bold ${pnl == null ? 'text-slate-500' : pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pnl == null ? '—' : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%`}
                    </div>
                  </div>
                </div>

                <div className="mt-1.5 flex items-center gap-2 text-xs flex-wrap">
                  <span className="text-red-400">🛑 {fmtPrice(postTp1 ? (t.sl_current_price ?? t.planned_stop) : t.planned_stop)}</span>
                  <span className="text-slate-600">|</span>
                  {t.planned_tp1 != null && <span className="text-emerald-400">🎯 {fmtPrice(t.planned_tp1)}</span>}
                  {t.planned_tp2 != null && <span className="text-green-500">🎯 {fmtPrice(t.planned_tp2)}</span>}
                  {(() => {
                    const rr = rrFromLevels(t.side, t.entry_price, t.planned_stop, t.planned_tp1)
                    if (rr == null) return null
                    return (
                      <span className="text-emerald-300 font-mono" title="Risco/Retorno até o TP1 (calculado de entry/SL/TP1).">
                        R:R 1:{roundRR(rr)}
                      </span>
                    )
                  })()}
                  {t.leverage != null && <span className="ml-auto text-orange-300 font-mono">{t.leverage}x</span>}
                </div>
              </div>
            )
          })}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 text-[10px] text-slate-600 leading-relaxed">
          Operações <strong className="text-slate-400">ativas em tempo real</strong> — suas (gerenciadas pelo bot) + automáticas.
          O bot coloca SL/TP1/TP2 e move o BE pós-TP1. Ao fechar, a operação sai daqui e fica no Dashboard.
        </div>
      </div>
    </div>
  )
}
