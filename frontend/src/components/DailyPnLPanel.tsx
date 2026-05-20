import { useState, useEffect, useCallback } from 'react'
import { X, BarChart3, TrendingUp, TrendingDown, Clock, RefreshCw, Calendar } from 'lucide-react'

interface DailyPnL {
  enabled: boolean
  message?: string
  date?: string
  summary?: {
    total_trades: number
    wins: number
    losses: number
    win_rate_pct: number
    total_r: number
    still_open: number
  }
  trades?: {
    symbol: string
    timeframe: string
    tier: string
    direction: 'long' | 'short'
    entry: number
    stop_loss: number
    tp2: number
    leverage: number
    status: string
    realized_r: number | null
    risk_pct: number
    created_at: string
    outcome_at: string | null
  }[]
}

interface Props {
  onClose: () => void
}

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

function fmtPct(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`
}

function fmt(n: number) {
  if (n >= 1000) return n.toFixed(2)
  if (n >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  won_tp2: { label: 'TP2 ✓', cls: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40' },
  won_tp1: { label: 'TP1 ✓', cls: 'bg-green-500/20 text-green-300 border-green-500/40' },
  lost:    { label: 'STOP ✗', cls: 'bg-red-500/20 text-red-300 border-red-500/40' },
  open:    { label: 'aberto', cls: 'bg-slate-500/20 text-slate-300 border-slate-500/40' },
  expired: { label: 'expirado', cls: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/40' },
}

export default function DailyPnLPanel({ onClose }: Props) {
  const [date, setDate] = useState(todayISO())
  const [data, setData] = useState<DailyPnL | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async (d: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${BACKEND}/api/daily-pnl?date=${d}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(date) }, [load, date])

  const s = data?.summary
  const totalR = s?.total_r ?? 0
  const rColor = totalR > 0 ? 'text-emerald-300' : totalR < 0 ? 'text-red-300' : 'text-slate-300'

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-5xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2">
            <BarChart3 className="w-5 h-5 text-emerald-400" />
            <h2 className="text-base font-bold text-white">Resultado do Dia</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">· P&L das recomendações</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded px-2 py-1">
              <Calendar className="w-3.5 h-3.5 text-slate-400" />
              <input
                type="date"
                value={date}
                onChange={e => setDate(e.target.value)}
                className="bg-transparent text-xs text-slate-200 outline-none"
                max={todayISO()}
              />
            </div>
            <button onClick={() => load(date)} disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50">
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>
        </div>

        {data && !data.enabled && (
          <div className="m-4 p-4 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200">
            ⚠ {data.message || 'Banco de dados não configurado. Configure DATABASE_URL no Railway.'}
          </div>
        )}

        {error && (
          <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">⚠ {error}</div>
        )}

        {/* Resumo cards */}
        {s && (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 p-3 border-b border-slate-800">
            <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase">Total R</div>
              <div className={`text-xl font-bold font-mono ${rColor}`}>
                {totalR > 0 ? '+' : ''}{totalR.toFixed(2)}R
              </div>
              <div className="text-[10px] text-slate-600 mt-0.5">
                ≈ {fmtPct(totalR * (data?.trades?.[0]?.risk_pct ?? 1))} da banca
              </div>
            </div>
            <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase">Win Rate</div>
              <div className="text-xl font-bold text-white font-mono">{s.win_rate_pct.toFixed(1)}%</div>
              <div className="text-[10px] text-slate-600 mt-0.5">{s.wins}W · {s.losses}L</div>
            </div>
            <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase">Trades</div>
              <div className="text-xl font-bold text-white font-mono">{s.total_trades}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">resolvidos</div>
            </div>
            <div className="bg-emerald-500/5 border border-emerald-500/30 rounded-lg p-3">
              <div className="text-[10px] text-emerald-400/70 uppercase">Vencedores</div>
              <div className="text-xl font-bold text-emerald-300 font-mono">{s.wins}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">TP atingido</div>
            </div>
            <div className="bg-red-500/5 border border-red-500/30 rounded-lg p-3">
              <div className="text-[10px] text-red-400/70 uppercase">Perdedores</div>
              <div className="text-xl font-bold text-red-300 font-mono">{s.losses}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">stop bateu</div>
            </div>
            <div className="bg-slate-900/60 border border-slate-800 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase">Abertos</div>
              <div className="text-xl font-bold text-slate-300 font-mono">{s.still_open}</div>
              <div className="text-[10px] text-slate-600 mt-0.5">aguardando</div>
            </div>
          </div>
        )}

        {/* Lista */}
        <div className="flex-1 overflow-y-auto">
          {loading && !data && (
            <div className="flex items-center justify-center py-20">
              <div className="w-8 h-8 border-2 border-emerald-500 border-t-transparent rounded-full animate-spin" />
            </div>
          )}

          {data?.enabled && (!data.trades || data.trades.length === 0) && (
            <div className="flex flex-col items-center justify-center py-16 px-4 text-center gap-2">
              <Clock className="w-10 h-10 text-slate-600" />
              <p className="text-sm text-slate-300 font-semibold">Nenhum trade resolvido neste dia</p>
              <p className="text-xs text-slate-500 max-w-md">
                Recomendações aparecem aqui depois que o preço bater o TP ou o stop.
                Cheque amanhã ou abra o painel ✨ Recomendados pra gerar novos snapshots.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-1 p-3">
            {data?.trades?.map((t, i) => {
              const badge = STATUS_BADGE[t.status] || STATUS_BADGE.open
              const isLong = t.direction === 'long'
              const DirIcon = isLong ? TrendingUp : TrendingDown
              const r = t.realized_r ?? 0
              const rText = r > 0 ? `+${r.toFixed(1)}R` : r < 0 ? `${r.toFixed(1)}R` : '–'
              return (
                <div key={i} className="flex items-center gap-3 p-2.5 rounded-lg border border-slate-800 bg-slate-900/40 hover:bg-slate-900/80 transition-colors">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${badge.cls}`}>{badge.label}</span>
                  <DirIcon className={`w-4 h-4 ${isLong ? 'text-green-400' : 'text-red-400'}`} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-bold text-white">{t.symbol.split('/')[0]}</span>
                      <span className="text-[10px] text-slate-500 font-mono">{t.timeframe}</span>
                      <span className="text-[10px] text-orange-300 font-mono">{t.leverage}x</span>
                      <span className="text-[10px] text-slate-500">tier {t.tier}</span>
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono mt-0.5">
                      entry {fmt(t.entry)} → stop {fmt(t.stop_loss)} · tp2 {fmt(t.tp2)}
                    </div>
                  </div>
                  <div className={`text-right font-mono text-sm font-bold ${r > 0 ? 'text-emerald-300' : r < 0 ? 'text-red-300' : 'text-slate-400'}`}>
                    {rText}
                    <div className="text-[10px] text-slate-600 font-sans font-normal">
                      {fmtPct(r * t.risk_pct)}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">R:</strong> múltiplo de risco. +2R = atingiu TP2 (2× o risco). −1R = stop bateu.
          A coluna de <strong>% da banca</strong> assume o risco_pct do tier (A+ 1.5% / A 1% / B 0.5%).
          <br />
          Tracker verifica preço a cada 5 min · snapshots expiram em 48h se não resolverem
        </div>
      </div>
    </div>
  )
}
