import { useState, useEffect, useCallback } from 'react'
import { X, Plus, TrendingUp, TrendingDown, Bell, Trash2 } from 'lucide-react'

interface Trade {
  id: string
  symbol: string
  baseAsset: string
  timeframe: string
  direction: 'long' | 'short'
  entry: number
  stopLoss: number
  tp1: number
  tp2: number
  tp3: number
  openedAt: number
  status: 'open' | 'tp1' | 'tp2' | 'tp3' | 'stop' | 'exited'
  currentPrice: number
}

interface Alert {
  id: string
  tradeId: string
  message: string
  level: 'target' | 'stop' | 'info'
  time: Date
}

const STORAGE_KEY = 'crypto_agent_trades'

function loadTrades(): Trade[] {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]')
  } catch { return [] }
}

function saveTrades(trades: Trade[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(trades))
}

function pnlPct(trade: Trade): number {
  if (!trade.currentPrice || !trade.entry) return 0
  const diff = trade.direction === 'long'
    ? (trade.currentPrice - trade.entry) / trade.entry
    : (trade.entry - trade.currentPrice) / trade.entry
  return diff * 100
}

function fmtPrice(p: number): string {
  if (p >= 1000) return p.toLocaleString('pt-BR', { maximumFractionDigits: 2 })
  if (p >= 1) return p.toFixed(4)
  return p.toFixed(6)
}

interface Props {
  onClose: () => void
  onSelectSymbol?: (symbol: string) => void
}

export default function TradeManager({ onClose, onSelectSymbol }: Props) {
  const [trades, setTrades] = useState<Trade[]>(loadTrades)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [showAddForm, setShowAddForm] = useState(false)

  // Form state
  const [form, setForm] = useState({
    symbol: 'BTC/USDT:USDT', timeframe: '1d', direction: 'long',
    entry: '', stopLoss: '', tp1: '', tp2: '', tp3: '',
  })

  const addAlert = useCallback((tradeId: string, message: string, level: Alert['level']) => {
    const a: Alert = { id: Date.now().toString(), tradeId, message, level, time: new Date() }
    setAlerts(prev => [a, ...prev].slice(0, 20))
  }, [])

  // Poll prices every 8 seconds
  useEffect(() => {
    if (!trades.length) return
    const poll = async () => {
      for (const trade of trades) {
        const bs = trade.symbol.split(':')[0].replace('/', '')
        try {
          const r = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${bs}`)
          const data = await r.json()
          const price = parseFloat(data.price)
          if (!price) continue

          setTrades(prev => prev.map(t => {
            if (t.id !== trade.id) return t
            const updated = { ...t, currentPrice: price }

            // Check levels
            if (t.status === 'open') {
              if (t.direction === 'long') {
                if (price >= t.tp3 && t.status === 'open') {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 3 (${fmtPrice(t.tp3)})`, 'target')
                  return { ...updated, status: 'tp3' as const }
                }
                if (price >= t.tp2) {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 2 (${fmtPrice(t.tp2)})`, 'target')
                  return { ...updated, status: 'tp2' as const }
                }
                if (price >= t.tp1) {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 1 (${fmtPrice(t.tp1)})`, 'target')
                  return { ...updated, status: 'tp1' as const }
                }
                if (price <= t.stopLoss) {
                  addAlert(t.id, `🛑 ${t.baseAsset} STOP atingido (${fmtPrice(t.stopLoss)})`, 'stop')
                  return { ...updated, status: 'stop' as const }
                }
              } else {
                if (price <= t.tp3 && t.status === 'open') {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 3 (${fmtPrice(t.tp3)})`, 'target')
                  return { ...updated, status: 'tp3' as const }
                }
                if (price <= t.tp2) {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 2 (${fmtPrice(t.tp2)})`, 'target')
                  return { ...updated, status: 'tp2' as const }
                }
                if (price <= t.tp1) {
                  addAlert(t.id, `🎯 ${t.baseAsset} atingiu ALVO 1 (${fmtPrice(t.tp1)})`, 'target')
                  return { ...updated, status: 'tp1' as const }
                }
                if (price >= t.stopLoss) {
                  addAlert(t.id, `🛑 ${t.baseAsset} STOP atingido (${fmtPrice(t.stopLoss)})`, 'stop')
                  return { ...updated, status: 'stop' as const }
                }
              }
            }
            return updated
          }))
        } catch {}
      }
    }
    poll()
    const id = setInterval(poll, 8000)
    return () => clearInterval(id)
  }, [trades.length, addAlert])

  // Persist trades
  useEffect(() => { saveTrades(trades) }, [trades])

  const addTrade = () => {
    const base = form.symbol.split('/')[0]
    const trade: Trade = {
      id: Date.now().toString(),
      symbol: form.symbol,
      baseAsset: base,
      timeframe: form.timeframe,
      direction: form.direction as 'long' | 'short',
      entry: parseFloat(form.entry),
      stopLoss: parseFloat(form.stopLoss),
      tp1: parseFloat(form.tp1),
      tp2: parseFloat(form.tp2),
      tp3: parseFloat(form.tp3),
      openedAt: Date.now(),
      status: 'open',
      currentPrice: parseFloat(form.entry),
    }
    if (!trade.entry || !trade.stopLoss || !trade.tp1) return
    setTrades(prev => [trade, ...prev])
    setShowAddForm(false)
    addAlert(trade.id, `📋 Trade ${base} ${form.direction.toUpperCase()} adicionado`, 'info')
  }

  const removeTrade = (id: string) => {
    setTrades(prev => prev.filter(t => t.id !== id))
  }

  const exitTrade = (id: string) => {
    setTrades(prev => prev.map(t =>
      t.id === id ? { ...t, status: 'exited' as const } : t
    ))
    const trade = trades.find(t => t.id === id)
    if (trade) addAlert(id, `🚪 ${trade.baseAsset} saiu a mercado`, 'info')
  }

  const statusBadge = (status: Trade['status']) => {
    const map: Record<Trade['status'], { label: string; cls: string }> = {
      open:   { label: '🟢 ABERTO',  cls: 'bg-green-500/20 text-green-400 border-green-500/40' },
      tp1:    { label: '🎯 ALVO 1',  cls: 'bg-blue-500/20 text-blue-400 border-blue-500/40' },
      tp2:    { label: '🎯 ALVO 2',  cls: 'bg-blue-500/30 text-blue-300 border-blue-500/50' },
      tp3:    { label: '🎯 ALVO 3',  cls: 'bg-violet-500/20 text-violet-400 border-violet-500/40' },
      stop:   { label: '🛑 STOP',    cls: 'bg-red-500/20 text-red-400 border-red-500/40' },
      exited: { label: '⬜ SAIU',    cls: 'bg-slate-700 text-slate-400 border-slate-600' },
    }
    const cfg = map[status] ?? map.open
    return <span className={`text-xs px-2 py-0.5 rounded border font-semibold ${cfg.cls}`}>{cfg.label}</span>
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/70 flex justify-end">
      <div className="w-full max-w-md bg-[#0d1320] border-l border-slate-700 flex flex-col h-full">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <Bell className="w-4 h-4 text-yellow-400" />
            <span className="font-bold text-white text-sm">Gestão de Trades</span>
            <span className="text-xs bg-slate-700 text-slate-300 px-1.5 py-0.5 rounded">
              {trades.filter(t => t.status === 'open').length} abertos
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowAddForm(v => !v)}
              className="flex items-center gap-1 px-2 py-1 bg-blue-600 hover:bg-blue-500 rounded text-xs font-semibold"
            >
              <Plus className="w-3 h-3" /> Adicionar
            </button>
            <button onClick={onClose} className="p-1 bg-slate-800 hover:bg-slate-700 rounded">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Add form */}
        {showAddForm && (
          <div className="px-4 py-3 border-b border-slate-800 bg-slate-800/30">
            <p className="text-xs text-slate-400 font-semibold mb-2">NOVO TRADE</p>
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
                { key: 'entry', label: 'Entrada' },
                { key: 'stopLoss', label: 'Stop Loss' },
                { key: 'tp1', label: 'Alvo 1' },
                { key: 'tp2', label: 'Alvo 2' },
                { key: 'tp3', label: 'Alvo 3' },
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
              onClick={addTrade}
              className="w-full mt-2 py-1.5 bg-green-600 hover:bg-green-500 rounded text-xs font-bold"
            >
              ✅ Confirmar Trade
            </button>
          </div>
        )}

        {/* Alerts */}
        {alerts.length > 0 && (
          <div className="px-3 py-2 border-b border-slate-800 max-h-32 overflow-y-auto">
            <p className="text-xs text-slate-500 mb-1 font-semibold">ALERTAS RECENTES</p>
            {alerts.slice(0, 5).map(a => (
              <div key={a.id} className={`text-xs py-0.5 ${a.level === 'stop' ? 'text-red-400' : a.level === 'target' ? 'text-green-400' : 'text-slate-400'}`}>
                {a.message} <span className="text-slate-600">{a.time.toLocaleTimeString('pt-BR')}</span>
              </div>
            ))}
          </div>
        )}

        {/* Trade list */}
        <div className="flex-1 overflow-y-auto">
          {trades.length === 0 && (
            <div className="flex flex-col items-center justify-center h-48 text-slate-600">
              <Bell className="w-8 h-8 mb-2" />
              <p className="text-sm">Nenhum trade monitorado</p>
              <p className="text-xs mt-1">Clique em "Adicionar" para começar</p>
            </div>
          )}
          {trades.map(trade => {
            const pnl = pnlPct(trade)
            const isOpen = trade.status === 'open' || trade.status === 'tp1' || trade.status === 'tp2'
            return (
              <div key={trade.id} className="border-b border-slate-800/60 p-3">
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-2">
                    {trade.direction === 'long'
                      ? <TrendingUp className="w-4 h-4 text-green-400" />
                      : <TrendingDown className="w-4 h-4 text-red-400" />}
                    <div>
                      <button
                        className="font-bold text-sm text-white hover:text-blue-400"
                        onClick={() => onSelectSymbol?.(trade.symbol)}
                      >
                        {trade.baseAsset}/USDT
                      </button>
                      <span className="text-xs text-slate-500 ml-1">{trade.timeframe}</span>
                    </div>
                    {statusBadge(trade.status)}
                  </div>
                  <div className="flex items-center gap-1">
                    {isOpen && (
                      <button
                        onClick={() => exitTrade(trade.id)}
                        className="text-xs px-1.5 py-0.5 bg-slate-700 hover:bg-slate-600 rounded text-slate-300"
                        title="Sair a mercado"
                      >
                        🚪
                      </button>
                    )}
                    <button
                      onClick={() => removeTrade(trade.id)}
                      className="p-0.5 hover:text-red-400 text-slate-600"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </div>

                <div className="mt-2 grid grid-cols-3 gap-x-3 text-xs">
                  <div>
                    <span className="text-slate-500">Entrada</span>
                    <div className="text-yellow-400 font-mono">{fmtPrice(trade.entry)}</div>
                  </div>
                  <div>
                    <span className="text-slate-500">Atual</span>
                    <div className={`font-mono ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {trade.currentPrice ? fmtPrice(trade.currentPrice) : '-'}
                    </div>
                  </div>
                  <div>
                    <span className="text-slate-500">PnL</span>
                    <div className={`font-bold ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)}%
                    </div>
                  </div>
                </div>

                <div className="mt-1.5 flex items-center gap-2 text-xs">
                  <span className="text-red-400">🛑 {fmtPrice(trade.stopLoss)}</span>
                  <span className="text-slate-600">|</span>
                  <span className="text-green-400">🎯 {fmtPrice(trade.tp1)}</span>
                  <span className="text-green-500">🎯 {fmtPrice(trade.tp2)}</span>
                  <span className="text-emerald-400">🎯 {fmtPrice(trade.tp3)}</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
