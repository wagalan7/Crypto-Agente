import { useState, useEffect, useCallback } from 'react'
import { X, Sparkles, TrendingUp, TrendingDown, RefreshCw, AlertTriangle } from 'lucide-react'
import { api, fetchBinanceOHLCV } from '../services/api'
import type { Recommendation, RecommendationTier } from '../types'

interface Props {
  onClose: () => void
  onSelectSymbol: (symbol: string, timeframe: string) => void
}

const TIER_CONFIG: Record<RecommendationTier, { label: string; bg: string; border: string; text: string; ring: string }> = {
  'A+': {
    label: 'A+',
    bg: 'bg-gradient-to-r from-emerald-500/20 to-green-500/20',
    border: 'border-emerald-400/60',
    text: 'text-emerald-300',
    ring: 'ring-2 ring-emerald-400/40',
  },
  'A': {
    label: 'A',
    bg: 'bg-green-500/10',
    border: 'border-green-500/40',
    text: 'text-green-400',
    ring: '',
  },
  'B': {
    label: 'B',
    bg: 'bg-blue-500/10',
    border: 'border-blue-500/40',
    text: 'text-blue-400',
    ring: '',
  },
}

const TIER_DESC: Record<RecommendationTier, string> = {
  'A+': 'Setup premium — confluência alta + MTF alinhado + R:R ≥ 2.5',
  'A':  'Setup forte — confluência boa + MTF a favor + R:R ≥ 2.0',
  'B':  'Setup aceitável — confluência média, validar entrada',
}

function fmt(n: number) {
  if (n >= 1000) return n.toFixed(2)
  if (n >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

function symbolBase(s: string): string {
  return s.split('/')[0]
}

export default function RecommendationsPanel({ onClose, onSelectSymbol }: Props) {
  const [recs, setRecs] = useState<Recommendation[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  const [filter, setFilter] = useState<RecommendationTier | 'all'>('all')
  const [progress, setProgress] = useState<string>('')

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setProgress('Buscando top 30 perpétuos…')
    try {
      // 1) Top símbolos por volume (Binance Futures, IP do browser — não bloqueado)
      const symbols = await api.fetchTopBinanceSymbols(30)
      if (symbols.length === 0) throw new Error('Nenhum símbolo encontrado')

      // 2) Baixa candles de 15m/1h/4h para cada símbolo (em paralelo, com limite)
      const TFS = ['15m', '1h', '4h'] as const
      setProgress(`Baixando candles (${symbols.length} símbolos × ${TFS.length} TFs)…`)

      const tasks: Promise<{ symbol: string; timeframe: string; candles: Awaited<ReturnType<typeof fetchBinanceOHLCV>> } | null>[] = []
      for (const symbol of symbols) {
        for (const tf of TFS) {
          tasks.push(
            fetchBinanceOHLCV(symbol, tf, 200)
              .then(candles => ({ symbol, timeframe: tf, candles }))
              .catch(() => null),
          )
        }
      }
      // Concorrência limitada (lotes de 12)
      const results: ({ symbol: string; timeframe: string; candles: unknown[] } | null)[] = []
      const BATCH = 12
      for (let i = 0; i < tasks.length; i += BATCH) {
        const slice = await Promise.all(tasks.slice(i, i + BATCH))
        results.push(...slice)
        setProgress(`Baixando candles… ${Math.min(i + BATCH, tasks.length)}/${tasks.length}`)
      }
      const items = results.filter((x): x is { symbol: string; timeframe: string; candles: { timestamp: number; open: number; high: number; low: number; close: number; volume: number }[] } => x !== null && x.candles.length >= 80)

      if (items.length === 0) throw new Error('Falha ao baixar candles')

      // 3) Envia em lote pro backend pra analisar + classificar
      setProgress(`Analisando ${items.length} (símbolo × TF)…`)
      const res = await api.recommendationsBatch(items)
      setRecs(res.recommendations)
      setLastUpdate(new Date())
      setProgress('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro ao carregar')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 120_000)   // refresh a cada 2 min
    return () => clearInterval(id)
  }, [load])

  const counts = {
    'A+': recs.filter(r => r.tier === 'A+').length,
    'A':  recs.filter(r => r.tier === 'A').length,
    'B':  recs.filter(r => r.tier === 'B').length,
  }
  const filtered = filter === 'all' ? recs : recs.filter(r => r.tier === filter)

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-5xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-yellow-400" />
            <h2 className="text-base font-bold text-white">Trades Recomendados</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">
              · varredura dos top 30 perpétuos por volume
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              <span className="hidden sm:inline">Atualizar</span>
            </button>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>
        </div>

        {/* Filtros / contagem */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-800 overflow-x-auto">
          {(['all', 'A+', 'A', 'B'] as const).map(tier => {
            const cfg = tier === 'all' ? null : TIER_CONFIG[tier]
            const active = filter === tier
            const count = tier === 'all' ? recs.length : counts[tier]
            return (
              <button
                key={tier}
                onClick={() => setFilter(tier)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs font-semibold whitespace-nowrap transition-colors ${
                  active
                    ? cfg ? `${cfg.bg} ${cfg.border} ${cfg.text}` : 'bg-white/10 border-white/30 text-white'
                    : 'bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200'
                }`}
              >
                <span>{tier === 'all' ? 'Todos' : `Tier ${tier}`}</span>
                <span className="text-slate-500">({count})</span>
              </button>
            )
          })}
          {lastUpdate && (
            <span className="ml-auto text-[10px] text-slate-600 whitespace-nowrap">
              últ. {lastUpdate.toLocaleTimeString('pt-BR')}
            </span>
          )}
        </div>

        {/* Lista */}
        <div className="flex-1 overflow-y-auto">
          {loading && recs.length === 0 && (
            <div className="flex flex-col items-center justify-center py-20 gap-3">
              <div className="w-8 h-8 border-2 border-yellow-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-slate-500">{progress || 'Varredura em andamento…'}</span>
            </div>
          )}

          {error && (
            <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">
              ⚠ {error}
            </div>
          )}

          {!loading && !error && filtered.length === 0 && (
            <div className="flex flex-col items-center justify-center py-20 gap-3 px-4 text-center">
              <span className="text-4xl">🧘</span>
              <p className="text-sm text-slate-300 font-semibold">
                Nenhum setup {filter === 'all' ? '' : `Tier ${filter}`} no momento
              </p>
              <p className="text-xs text-slate-500 max-w-md">
                Esperar é a operação correta. Forçar trade em mercado lateral é perda garantida —
                o sistema só recomenda quando há real confluência.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-2 p-3">
            {filtered.map((r) => {
              const cfg = TIER_CONFIG[r.tier]
              const isLong = r.direction === 'long'
              const DirIcon = isLong ? TrendingUp : TrendingDown
              const dirColor = isLong ? 'text-green-400' : 'text-red-400'
              return (
                <button
                  key={`${r.symbol}-${r.timeframe}`}
                  onClick={() => onSelectSymbol(r.symbol, r.timeframe)}
                  className={`w-full text-left p-3 rounded-lg border ${cfg.bg} ${cfg.border} ${cfg.ring} hover:bg-slate-800/40 transition-colors`}
                >
                  <div className="flex items-center gap-3">
                    {/* Tier badge */}
                    <div className={`flex-shrink-0 w-10 h-10 rounded-lg flex items-center justify-center font-black text-base border ${cfg.border} ${cfg.text}`}>
                      {cfg.label}
                    </div>

                    {/* Symbol + direction */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-bold text-white">{symbolBase(r.symbol)}</span>
                        <span className="text-[10px] text-slate-500 font-mono">{r.timeframe}</span>
                        <DirIcon className={`w-3.5 h-3.5 ${dirColor}`} />
                        <span className={`text-xs font-bold ${dirColor}`}>
                          {isLong ? 'LONG' : 'SHORT'}
                        </span>
                      </div>
                      <p className="text-[11px] text-slate-400 mt-0.5 leading-tight">{r.summary}</p>
                      {r.warnings.length > 0 && (
                        <div className="flex items-center gap-1 mt-1">
                          <AlertTriangle className="w-3 h-3 text-yellow-400 flex-shrink-0" />
                          <span className="text-[10px] text-yellow-300/80 truncate">{r.warnings[0]}</span>
                        </div>
                      )}
                    </div>

                    {/* Score + R:R + entry */}
                    <div className="flex-shrink-0 text-right">
                      <div className="text-sm font-bold text-white">{r.score.toFixed(0)}</div>
                      <div className="text-[10px] text-slate-500">score</div>
                      <div className="text-[10px] text-emerald-300 mt-1 font-mono">1:{r.risk_reward}</div>
                    </div>
                  </div>

                  {/* Levels resumido */}
                  <div className="grid grid-cols-3 gap-2 mt-2 pt-2 border-t border-slate-800/60 text-[11px]">
                    <div>
                      <div className="text-slate-600">Entrada</div>
                      <div className="font-mono text-yellow-300">{fmt(r.entry)}</div>
                    </div>
                    <div>
                      <div className="text-slate-600">Stop</div>
                      <div className="font-mono text-red-300">{fmt(r.stop_loss)}</div>
                    </div>
                    <div>
                      <div className="text-slate-600">TP2</div>
                      <div className="font-mono text-green-300">{fmt(r.tp2)}</div>
                    </div>
                  </div>
                </button>
              )
            })}
          </div>
        </div>

        {/* Footer legend */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">Tiers:</strong>{' '}
          <span className="text-emerald-300">A+</span> {TIER_DESC['A+']} ·{' '}
          <span className="text-green-400">A</span> {TIER_DESC['A']} ·{' '}
          <span className="text-blue-400">B</span> {TIER_DESC['B']}
          <br />
          Atualiza automaticamente a cada 2 min · Cache backend 90s
        </div>
      </div>
    </div>
  )
}
