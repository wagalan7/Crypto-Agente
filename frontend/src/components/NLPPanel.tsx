import { useState, useCallback } from 'react'
import { X, Brain, Loader2, ChevronDown, ChevronUp, Clock, RotateCcw } from 'lucide-react'

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

// ─── Tipos ────────────────────────────────────────────────────────────────────

type EstadoId = 'calmo' | 'foco' | 'medo' | 'fomo' | 'ganancia' | 'raiva' | 'euforia' | 'confuso' | 'exausto'

interface Estado {
  id: EstadoId
  emoji: string
  label: string
  cor: string
  descricao: string
}

interface HistoricoItem {
  hora: string
  estado: EstadoId
  emoji: string
  intensidade: number
}

// ─── Dados ────────────────────────────────────────────────────────────────────

const ESTADOS: Estado[] = [
  { id: 'calmo',    emoji: '😌', label: 'Calmo',    cor: 'border-green-500  bg-green-500/10  text-green-400',  descricao: 'Estado ideal para trading' },
  { id: 'foco',     emoji: '🎯', label: 'Focado',   cor: 'border-blue-500   bg-blue-500/10   text-blue-400',   descricao: 'Mente clara, plano definido' },
  { id: 'medo',     emoji: '😰', label: 'Medo',     cor: 'border-yellow-500 bg-yellow-500/10 text-yellow-400', descricao: 'Receio de perda ou entrada' },
  { id: 'fomo',     emoji: '🔥', label: 'FOMO',     cor: 'border-orange-500 bg-orange-500/10 text-orange-400', descricao: 'Medo de perder movimento' },
  { id: 'ganancia', emoji: '🤑', label: 'Ganância', cor: 'border-red-400    bg-red-400/10    text-red-400',    descricao: 'Querendo ganhar mais' },
  { id: 'raiva',    emoji: '😤', label: 'Raiva',    cor: 'border-red-600    bg-red-600/10    text-red-500',    descricao: 'Frustrado com o mercado' },
  { id: 'euforia',  emoji: '🚀', label: 'Euforia',  cor: 'border-violet-500 bg-violet-500/10 text-violet-400', descricao: 'Animado demais após win' },
  { id: 'confuso',  emoji: '🤔', label: 'Confuso',  cor: 'border-slate-400  bg-slate-400/10  text-slate-400',  descricao: 'Incerto sobre a direção' },
  { id: 'exausto',  emoji: '😴', label: 'Exausto',  cor: 'border-slate-600  bg-slate-600/10  text-slate-500',  descricao: 'Cansado, sem energia' },
]

const INTENSIDADE_LABELS = ['', 'Leve', 'Moderado', 'Forte', 'Intenso', 'Extremo']

// ─── Componente ───────────────────────────────────────────────────────────────

interface Props {
  onClose: () => void
}

export default function NLPPanel({ onClose }: Props) {
  const [estadoSel, setEstadoSel] = useState<EstadoId | null>(null)
  const [intensidade, setIntensidade] = useState(3)
  const [contexto, setContexto] = useState('')
  const [coaching, setCoaching] = useState('')
  const [loading, setLoading] = useState(false)
  const [historico, setHistorico] = useState<HistoricoItem[]>([])
  const [showHistorico, setShowHistorico] = useState(false)
  const [showContexto, setShowContexto] = useState(false)

  const estadoObj = ESTADOS.find(e => e.id === estadoSel)

  const buscarCoaching = useCallback(async () => {
    if (!estadoSel) return
    setLoading(true)
    setCoaching('')

    const agora = new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })

    // Adiciona ao histórico
    const item: HistoricoItem = {
      hora: agora,
      estado: estadoSel,
      emoji: estadoObj?.emoji ?? '',
      intensidade,
    }
    setHistorico(prev => [item, ...prev].slice(0, 20))

    try {
      const res = await fetch(`${BACKEND}/api/nlp-coach`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          estado: estadoSel,
          intensidade,
          contexto,
          historico: historico.slice(0, 5),
        }),
      })
      const data = await res.json()
      setCoaching(data.coaching ?? 'Sem resposta.')
    } catch {
      setCoaching('Erro ao conectar ao servidor. Tente novamente.')
    } finally {
      setLoading(false)
    }
  }, [estadoSel, intensidade, contexto, historico, estadoObj])

  const resetar = () => {
    setEstadoSel(null)
    setIntensidade(3)
    setContexto('')
    setCoaching('')
    setShowContexto(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-stretch sm:items-center justify-end sm:justify-end pointer-events-none">
      {/* Overlay clicável */}
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm pointer-events-auto"
        onClick={onClose}
      />

      {/* Painel */}
      <div className="relative w-full sm:w-96 h-full sm:h-auto sm:max-h-[90vh] bg-[#0d1320] border-l border-slate-800 flex flex-col pointer-events-auto shadow-2xl overflow-hidden">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 flex-shrink-0 bg-gradient-to-r from-violet-900/30 to-transparent">
          <div className="flex items-center gap-2">
            <Brain className="w-5 h-5 text-violet-400" />
            <div>
              <h2 className="text-sm font-bold text-white">Coach PNL</h2>
              <p className="text-[10px] text-slate-500">Programação Neurolinguística</p>
            </div>
          </div>
          <div className="flex items-center gap-1">
            {(estadoSel || coaching) && (
              <button onClick={resetar} className="p-1.5 text-slate-500 hover:text-slate-300 transition-colors" title="Reiniciar">
                <RotateCcw className="w-3.5 h-3.5" />
              </button>
            )}
            <button onClick={onClose} className="p-1.5 text-slate-500 hover:text-white transition-colors">
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">

          {/* Seleção de Estado */}
          <div className="p-4 border-b border-slate-800/60">
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-3">
              Como você está agora?
            </p>
            <div className="grid grid-cols-3 gap-2">
              {ESTADOS.map(e => (
                <button
                  key={e.id}
                  onClick={() => { setEstadoSel(e.id); setCoaching('') }}
                  className={`flex flex-col items-center gap-1 p-2 rounded-lg border text-center transition-all ${
                    estadoSel === e.id
                      ? e.cor + ' border-opacity-100'
                      : 'border-slate-800 bg-slate-900/40 text-slate-400 hover:border-slate-600'
                  }`}
                >
                  <span className="text-xl">{e.emoji}</span>
                  <span className="text-[10px] font-semibold leading-tight">{e.label}</span>
                </button>
              ))}
            </div>

            {estadoObj && (
              <p className="text-[10px] text-slate-500 text-center mt-2 italic">
                {estadoObj.descricao}
              </p>
            )}
          </div>

          {/* Intensidade */}
          {estadoSel && (
            <div className="px-4 py-3 border-b border-slate-800/60">
              <div className="flex items-center justify-between mb-2">
                <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">
                  Intensidade
                </p>
                <span className={`text-xs font-bold ${
                  intensidade <= 2 ? 'text-green-400' :
                  intensidade === 3 ? 'text-yellow-400' :
                  intensidade >= 4 ? 'text-red-400' : ''
                }`}>
                  {INTENSIDADE_LABELS[intensidade]}
                </span>
              </div>
              <input
                type="range" min={1} max={5} value={intensidade}
                onChange={e => setIntensidade(Number(e.target.value))}
                className="w-full accent-violet-500 cursor-pointer"
              />
              <div className="flex justify-between text-[9px] text-slate-600 mt-0.5">
                <span>Leve</span><span>Moderado</span><span>Extremo</span>
              </div>
            </div>
          )}

          {/* Contexto (opcional) */}
          {estadoSel && (
            <div className="px-4 py-3 border-b border-slate-800/60">
              <button
                onClick={() => setShowContexto(v => !v)}
                className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-300 transition-colors"
              >
                {showContexto ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                O que aconteceu? <span className="text-slate-600">(opcional)</span>
              </button>
              {showContexto && (
                <textarea
                  value={contexto}
                  onChange={e => setContexto(e.target.value)}
                  placeholder="Ex: Tomei um stop, mercado virou rápido, perdi oportunidade..."
                  rows={2}
                  className="mt-2 w-full bg-slate-800/60 border border-slate-700 rounded px-2 py-1.5 text-xs text-slate-300 placeholder-slate-600 resize-none focus:outline-none focus:border-violet-500"
                />
              )}
            </div>
          )}

          {/* Botão de coaching */}
          {estadoSel && (
            <div className="px-4 py-3 border-b border-slate-800/60">
              <button
                onClick={buscarCoaching}
                disabled={loading}
                className="w-full flex items-center justify-center gap-2 py-2.5 bg-violet-700 hover:bg-violet-600 disabled:opacity-50 rounded-lg text-sm font-semibold text-white transition-colors"
              >
                {loading ? (
                  <><Loader2 className="w-4 h-4 animate-spin" /> Analisando seu estado...</>
                ) : (
                  <><Brain className="w-4 h-4" /> Receber Coaching PNL</>
                )}
              </button>
            </div>
          )}

          {/* Resposta do coach */}
          {coaching && (
            <div className="px-4 py-3 border-b border-slate-800/60">
              <div className="flex items-center gap-1.5 mb-2">
                <Brain className="w-3.5 h-3.5 text-violet-400" />
                <span className="text-xs font-semibold text-violet-400">Coaching Personalizado</span>
              </div>
              <div className="bg-slate-900/60 rounded-lg p-3 border border-violet-800/30">
                <p className="text-xs text-slate-300 whitespace-pre-line leading-relaxed">
                  {coaching}
                </p>
              </div>
            </div>
          )}

          {/* Estado inicial (sem seleção) */}
          {!estadoSel && (
            <div className="px-4 py-8 flex flex-col items-center text-center gap-3">
              <div className="text-4xl">🧠</div>
              <p className="text-sm font-semibold text-slate-300">Gestão Emocional em Tempo Real</p>
              <p className="text-xs text-slate-500 max-w-64 leading-relaxed">
                Selecione como você está se sentindo agora. A IA usará técnicas de PNL para te ajudar a tomar decisões mais racionais.
              </p>
              <div className="mt-2 grid grid-cols-2 gap-2 text-[10px] text-slate-500 w-full">
                {['Ancoragem', 'Reencadramento', 'Dissociação', 'Rapport Interno'].map(t => (
                  <div key={t} className="bg-slate-800/40 rounded px-2 py-1 border border-slate-800">
                    ✦ {t}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Histórico da sessão */}
          {historico.length > 0 && (
            <div className="px-4 py-3">
              <button
                onClick={() => setShowHistorico(v => !v)}
                className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors w-full"
              >
                <Clock className="w-3 h-3" />
                Histórico da sessão ({historico.length})
                {showHistorico ? <ChevronUp className="w-3 h-3 ml-auto" /> : <ChevronDown className="w-3 h-3 ml-auto" />}
              </button>
              {showHistorico && (
                <div className="mt-2 flex flex-col gap-1">
                  {historico.map((h, i) => (
                    <div key={i} className="flex items-center gap-2 text-[10px] text-slate-500 py-1 border-b border-slate-800/40 last:border-0">
                      <span className="text-slate-600">{h.hora}</span>
                      <span>{h.emoji}</span>
                      <span className="capitalize">{h.estado}</span>
                      <span className="ml-auto text-slate-700">
                        {'●'.repeat(h.intensidade)}{'○'.repeat(5 - h.intensidade)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
