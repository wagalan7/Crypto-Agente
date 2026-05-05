import { MousePointer2, Minus, TrendingUp, Trash2, Bot, Loader2, Square, Percent } from 'lucide-react'
import type { DrawingTool } from '../types'

interface Props {
  activeTool: DrawingTool
  onToolChange: (t: DrawingTool) => void
  onClear: () => void
  onValidate: () => void
  hasDrawings: boolean
  validating: boolean
}

const TOOLS: { key: DrawingTool; icon: React.ComponentType<{ className?: string }>; label: string; hint: string }[] = [
  { key: 'cursor',    icon: MousePointer2, label: 'Cursor',             hint: '' },
  { key: 'hline',     icon: Minus,         label: 'Linha Horizontal',   hint: 'Clique no preço desejado' },
  { key: 'trendline', icon: TrendingUp,    label: 'Linha de Tendência', hint: 'Clique em 2 pontos' },
  { key: 'fibonacci', icon: Percent,       label: 'Fibonacci',          hint: 'Clique no topo e no fundo' },
  { key: 'rectangle', icon: Square,        label: 'Retângulo',          hint: 'Clique em 2 cantos' },
]

export default function DrawingToolbar({ activeTool, onToolChange, onClear, onValidate, hasDrawings, validating }: Props) {
  const active = TOOLS.find(t => t.key === activeTool)
  return (
    <div className="absolute top-2 left-2 z-10 flex flex-col gap-1">
      <div className="bg-slate-900/90 border border-slate-700/60 rounded-lg p-1 flex flex-col gap-0.5 shadow-lg backdrop-blur-sm">
        {TOOLS.map(({ key, icon: Icon, label }) => (
          <button
            key={key}
            title={label}
            onClick={() => onToolChange(key)}
            className={`p-1.5 rounded transition-colors ${
              activeTool === key
                ? 'bg-blue-600 text-white'
                : 'text-slate-400 hover:text-white hover:bg-slate-700'
            }`}
          >
            <Icon className="w-3.5 h-3.5" />
          </button>
        ))}
        {hasDrawings && (
          <>
            <div className="border-t border-slate-700 my-0.5" />
            <button title="Apagar todos os desenhos" onClick={onClear}
              className="p-1.5 rounded text-red-400 hover:text-white hover:bg-red-600/50 transition-colors">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
            <button title="Validar padrão com IA" onClick={onValidate} disabled={validating}
              className="p-1.5 rounded text-violet-400 hover:text-white hover:bg-violet-600/50 transition-colors disabled:opacity-50">
              {validating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Bot className="w-3.5 h-3.5" />}
            </button>
          </>
        )}
      </div>
      {active && active.hint && (
        <div className="bg-slate-900/80 border border-slate-700/40 rounded px-2 py-1 text-[10px] text-slate-400 text-center backdrop-blur-sm">
          {active.hint}
        </div>
      )}
    </div>
  )
}
