import { useState } from 'react'
import { Bot, Trash2, Plus, ChevronDown, ChevronUp, Loader2, X } from 'lucide-react'
import { api } from '../../services/api'
import type { HLineDrawing, TrendLineDrawing, FibonacciDrawing, UserDrawing } from '../../types'

interface Props {
  symbol: string
  timeframe: string
}

type ToolMode = 'hline' | 'trendline' | 'fibonacci'

const TOOL_LABELS: Record<ToolMode, string> = {
  hline: 'Nível H',
  trendline: 'Tendência',
  fibonacci: 'Fibonacci',
}

function PriceInput({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-slate-500 text-xs w-8 flex-shrink-0">{label}</span>
      <input
        type="number"
        step="any"
        placeholder="preço"
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-white font-mono placeholder-slate-600 focus:outline-none focus:border-blue-500"
      />
    </div>
  )
}

export function DrawingPanel({ symbol, timeframe }: Props) {
  const [open, setOpen] = useState(false)
  const [tool, setTool] = useState<ToolMode>('hline')
  const [drawings, setDrawings] = useState<UserDrawing[]>([])

  // Input fields
  const [p1, setP1] = useState('')
  const [p2, setP2] = useState('')

  // AI state
  const [aiResult, setAiResult] = useState('')
  const [validating, setValidating] = useState(false)
  const [aiOpen, setAiOpen] = useState(false)

  const addDrawing = () => {
    const price1 = parseFloat(p1)
    const price2 = parseFloat(p2)
    if (isNaN(price1)) return

    const id = Date.now().toString()
    const now = Date.now()

    if (tool === 'hline') {
      const d: HLineDrawing = { id, type: 'hline', price: price1, color: '#4ade80', label: `HL ${price1}` }
      setDrawings(prev => [...prev, d])
    } else if (tool === 'trendline') {
      if (isNaN(price2)) return
      const d: TrendLineDrawing = {
        id, type: 'trendline',
        p1: { price: price1, time: now - 3600000 },
        p2: { price: price2, time: now },
        color: '#f59e0b',
      }
      setDrawings(prev => [...prev, d])
    } else if (tool === 'fibonacci') {
      if (isNaN(price2)) return
      const d: FibonacciDrawing = {
        id, type: 'fibonacci',
        p1: { price: price1, time: now - 3600000 },
        p2: { price: price2, time: now },
        color: '#f59e0b',
      }
      setDrawings(prev => [...prev, d])
    }
    setP1('')
    setP2('')
  }

  const removeDrawing = (id: string) => setDrawings(prev => prev.filter(d => d.id !== id))

  const validate = async () => {
    if (drawings.length === 0) return
    setValidating(true)
    setAiResult('')
    setAiOpen(true)
    try {
      const res = await api.validateDrawing(symbol, timeframe, drawings)
      setAiResult(res.analysis)
    } catch {
      setAiResult('Erro ao chamar IA. Tente novamente.')
    } finally {
      setValidating(false)
    }
  }

  const clear = () => {
    setDrawings([])
    setAiResult('')
    setAiOpen(false)
  }

  const drawingLabel = (d: UserDrawing): string => {
    if (d.type === 'hline') return `Nível ${d.price.toLocaleString('pt-BR', { maximumFractionDigits: 6 })}`
    if (d.type === 'trendline')
      return `Tend. ${d.p1.price.toLocaleString('pt-BR', { maximumFractionDigits: 6 })} → ${d.p2.price.toLocaleString('pt-BR', { maximumFractionDigits: 6 })}`
    return `Fib ${Math.min(d.p1.price, d.p2.price).toLocaleString('pt-BR', { maximumFractionDigits: 6 })} – ${Math.max(d.p1.price, d.p2.price).toLocaleString('pt-BR', { maximumFractionDigits: 6 })}`
  }

  const drawingColor = (d: UserDrawing) =>
    d.type === 'hline' ? 'text-green-400' : d.type === 'trendline' ? 'text-yellow-400' : 'text-blue-400'

  return (
    <div className="absolute top-2 left-2 z-20 flex flex-col gap-1" style={{ maxWidth: 260 }}>
      {/* Toggle button */}
      <button
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1.5 px-2.5 py-1.5 bg-slate-900/95 border border-slate-700 rounded-lg text-xs font-semibold text-slate-300 hover:text-white hover:border-slate-500 transition-colors backdrop-blur-sm shadow-lg"
      >
        <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M3 17l6-6 4 4 8-8" />
        </svg>
        Desenhos
        {drawings.length > 0 && (
          <span className="ml-auto bg-blue-600 text-white rounded-full w-4 h-4 flex items-center justify-center text-[10px] font-bold">
            {drawings.length}
          </span>
        )}
        {open ? <ChevronUp className="w-3 h-3 ml-1" /> : <ChevronDown className="w-3 h-3 ml-1" />}
      </button>

      {open && (
        <div className="bg-slate-900/97 border border-slate-700 rounded-lg shadow-xl backdrop-blur-sm overflow-hidden">
          {/* Tool tabs */}
          <div className="flex border-b border-slate-800">
            {(Object.keys(TOOL_LABELS) as ToolMode[]).map(t => (
              <button
                key={t}
                onClick={() => { setTool(t); setP1(''); setP2('') }}
                className={`flex-1 py-1.5 text-[11px] font-semibold transition-colors ${tool === t ? 'text-white bg-slate-800' : 'text-slate-500 hover:text-slate-300'}`}
              >
                {TOOL_LABELS[t]}
              </button>
            ))}
          </div>

          {/* Inputs */}
          <div className="p-2 flex flex-col gap-1.5 border-b border-slate-800">
            <PriceInput label={tool === 'hline' ? 'Preço' : 'De'} value={p1} onChange={setP1} />
            {tool !== 'hline' && (
              <PriceInput label="Para" value={p2} onChange={setP2} />
            )}
            <button
              onClick={addDrawing}
              className="flex items-center justify-center gap-1 w-full py-1 bg-blue-600 hover:bg-blue-500 rounded text-xs font-semibold text-white transition-colors"
            >
              <Plus className="w-3 h-3" /> Adicionar
            </button>
          </div>

          {/* Drawing list */}
          {drawings.length > 0 && (
            <div className="p-2 flex flex-col gap-1 border-b border-slate-800 max-h-36 overflow-y-auto">
              {drawings.map(d => (
                <div key={d.id} className="flex items-center justify-between gap-1">
                  <span className={`text-[11px] font-mono truncate ${drawingColor(d)}`}>
                    {drawingLabel(d)}
                  </span>
                  <button onClick={() => removeDrawing(d.id)} className="text-slate-600 hover:text-red-400 flex-shrink-0">
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Actions */}
          <div className="p-2 flex gap-1.5">
            <button
              onClick={validate}
              disabled={drawings.length === 0 || validating}
              className="flex-1 flex items-center justify-center gap-1 py-1.5 bg-violet-700 hover:bg-violet-600 disabled:opacity-40 rounded text-xs font-semibold text-white transition-colors"
            >
              {validating ? <Loader2 className="w-3 h-3 animate-spin" /> : <Bot className="w-3 h-3" />}
              Validar IA
            </button>
            <button
              onClick={clear}
              disabled={drawings.length === 0}
              className="p-1.5 bg-slate-800 hover:bg-red-900/50 disabled:opacity-40 rounded transition-colors text-slate-400 hover:text-red-400"
              title="Limpar tudo"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>

          {/* AI Result */}
          {(aiOpen && (aiResult || validating)) && (
            <div className="border-t border-violet-800/50">
              <button
                onClick={() => setAiOpen(v => !v)}
                className="flex items-center gap-1 w-full px-2 py-1.5 text-violet-400 hover:text-violet-300 text-[11px] font-semibold transition-colors"
              >
                <Bot className="w-3 h-3" />
                Análise IA
                {aiOpen ? <ChevronUp className="w-3 h-3 ml-auto" /> : <ChevronDown className="w-3 h-3 ml-auto" />}
              </button>
              {aiOpen && (
                <div className="px-2 pb-2 max-h-64 overflow-y-auto">
                  {validating ? (
                    <div className="flex items-center gap-2 text-slate-400 text-xs py-2">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" /> Analisando padrão...
                    </div>
                  ) : (
                    <p className="text-[11px] text-slate-300 whitespace-pre-line leading-relaxed">{aiResult}</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
