import React, { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { CalendarSlot } from '../types'
import { OBJECTIVE_LABELS, OBJECTIVE_COLORS, FORMAT_LABELS } from '../types'

function groupByDay(slots: CalendarSlot[]) {
  const map = new Map<string, CalendarSlot[]>()
  for (const slot of slots) {
    const day = slot.scheduled_at.slice(0, 10)
    if (!map.has(day)) map.set(day, [])
    map.get(day)!.push(slot)
  }
  return map
}

export function CalendarPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [slots, setSlots] = useState<CalendarSlot[]>([])
  const [generating, setGenerating] = useState(false)
  const [populating, setPopulating] = useState(false)
  const [populateMsg, setPopulateMsg] = useState<string | null>(null)
  const [frequency, setFrequency] = useState(5)
  const [days, setDays] = useState(14)
  const [view, setView] = useState<'grid' | 'list'>('list')
  const [loading, setLoading] = useState(true)
  const [dragOverDay, setDragOverDay] = useState<string | null>(null)
  const [rescheduleMsg, setRescheduleMsg] = useState<string | null>(null)
  const [expandedSlot, setExpandedSlot] = useState<number | null>(null)

  async function load() {
    setLoading(true)
    try {
      const data: any = await api.calendar.get(id, days)
      setSlots(data)
    } finally { setLoading(false) }
  }

  useEffect(() => { load() }, [id, days])

  function onDragStart(e: React.DragEvent, slotId: number) {
    e.dataTransfer.setData('text/plain', String(slotId))
    e.dataTransfer.effectAllowed = 'move'
  }

  async function onDropOnDay(e: React.DragEvent, targetDay: string) {
    e.preventDefault()
    setDragOverDay(null)
    const slotId = Number(e.dataTransfer.getData('text/plain'))
    const slot = slots.find(s => s.id === slotId)
    if (!slot) return
    if (slot.scheduled_at.slice(0, 10) === targetDay) return
    // Preserve the existing time-of-day, swap only the date
    const orig = new Date(slot.scheduled_at)
    const [y, m, d] = targetDay.split('-').map(Number)
    const next = new Date(orig)
    next.setFullYear(y, m - 1, d)
    // Optimistic update
    setSlots(prev => prev.map(s => s.id === slotId ? { ...s, scheduled_at: next.toISOString() } : s))
    try {
      await api.calendar.reschedule(slotId, next.toISOString())
      setRescheduleMsg(`✓ Reagendado para ${next.toLocaleDateString('pt-BR')}`)
      setTimeout(() => setRescheduleMsg(null), 2500)
    } catch (err: any) {
      setRescheduleMsg(`Erro: ${err?.message?.slice(0, 100) || 'falha ao reagendar'}`)
      await load()
    }
  }

  async function generate() {
    setGenerating(true)
    try {
      const monday = new Date()
      monday.setDate(monday.getDate() - monday.getDay() + 1)
      monday.setHours(0, 0, 0, 0)
      await api.calendar.generateWeek({ client_id: id, start_date: monday.toISOString(), frequency_per_week: frequency })
      await load()
    } finally {
      setGenerating(false)
    }
  }

  async function populateFromWeekly() {
    setPopulating(true)
    setPopulateMsg(null)
    try {
      const created: any = await api.calendar.populateFromWeekly({ client_id: id, platform: 'instagram', default_hour: 18 })
      setPopulateMsg(`✓ ${created.length} slots criados a partir da sequência emocional`)
      await load()
    } catch (e: any) {
      setPopulateMsg(`Erro: ${e?.message?.slice(0, 200) || 'falha ao popular'}`)
    } finally {
      setPopulating(false)
    }
  }

  const grouped = groupByDay(slots)
  const today = new Date().toISOString().slice(0, 10)
  const days14 = Array.from({ length: 14 }, (_, i) => {
    const d = new Date()
    d.setDate(d.getDate() + i)
    return d.toISOString().slice(0, 10)
  })

  return (
    <div className="p-4 md:p-6 space-y-4 max-w-5xl">
      {/* Controls */}
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-bold text-white">Calendário</h1>
          <div className="flex gap-1.5">
            <button onClick={() => setView('list')}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${view === 'list' ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'}`}>
              Lista
            </button>
            <button onClick={() => setView('grid')}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors hidden md:block ${view === 'grid' ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'}`}>
              Grade
            </button>
          </div>
        </div>

        <div className="flex gap-2 overflow-x-auto scrollbar-none">
          <select className="input-field w-auto text-xs shrink-0" value={frequency}
            onChange={e => setFrequency(Number(e.target.value))}>
            {[3, 4, 5, 6, 7].map(n => <option key={n} value={n}>{n}x/semana</option>)}
          </select>
          <select className="input-field w-auto text-xs shrink-0" value={days}
            onChange={e => setDays(Number(e.target.value))}>
            <option value={7}>7 dias</option>
            <option value={14}>14 dias</option>
            <option value={30}>30 dias</option>
          </select>
          <button onClick={generate} disabled={generating} className="btn-primary w-auto px-4 py-2 shrink-0 text-xs">
            {generating ? 'Gerando...' : 'Gerar semana'}
          </button>
          <button onClick={populateFromWeekly} disabled={populating} className="w-auto px-3 py-2 shrink-0 text-xs rounded-lg border border-violet-700 bg-violet-900/20 text-violet-300 hover:bg-violet-900/40 transition-colors disabled:opacity-50">
            {populating ? 'Populando...' : '✦ Popular c/ sequência emocional'}
          </button>
        </div>
        {populateMsg && <p className="text-xs text-gray-400">{populateMsg}</p>}
        {rescheduleMsg && <p className="text-xs text-violet-400">{rescheduleMsg}</p>}
      </div>

      {view === 'grid' && !loading && (
        <p className="text-[10px] text-gray-500">Dica: arraste os slots entre os dias para reagendar</p>
      )}

      {loading && (
        <div className="space-y-2">
          {[0, 1, 2, 3].map(i => (
            <div key={i} className="card animate-pulse h-14 bg-gray-900/60" />
          ))}
        </div>
      )}

      {/* Grid view (desktop only) */}
      {!loading && view === 'grid' && (
        <div className="hidden md:block">
          <div className="grid grid-cols-7 gap-1 mb-1">
            {['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'].map(d => (
              <div key={d} className="text-center text-xs text-gray-500 font-medium py-1">{d}</div>
            ))}
          </div>
          <div className="grid grid-cols-7 gap-1">
            {days14.map(day => {
              const daySlots = grouped.get(day) || []
              const date = new Date(day + 'T12:00:00')
              const isToday = day === today
              const isDragOver = dragOverDay === day
              return (
                <div key={day}
                  onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; if (dragOverDay !== day) setDragOverDay(day) }}
                  onDragLeave={() => { if (dragOverDay === day) setDragOverDay(null) }}
                  onDrop={e => onDropOnDay(e, day)}
                  className={`min-h-20 rounded-lg border p-1.5 transition-colors ${isDragOver ? 'border-violet-400 bg-violet-900/30 ring-1 ring-violet-400' : isToday ? 'border-violet-600 bg-violet-900/10' : 'border-gray-800 bg-gray-900'}`}>
                  <p className={`text-xs font-semibold mb-1 ${isToday ? 'text-violet-400' : 'text-gray-400'}`}>{date.getDate()}</p>
                  {daySlots.map(slot => (
                    <div key={slot.id}
                      draggable
                      onDragStart={e => onDragStart(e, slot.id)}
                      title="Arraste para reagendar"
                      className={`text-[10px] rounded px-1 py-0.5 mb-0.5 border cursor-move hover:opacity-80 ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                      {FORMAT_LABELS[slot.format] || slot.format}
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* List view */}
      {!loading && (view === 'list' || true) && slots.length > 0 && (
        <div className="space-y-2">
          {slots.map(slot => {
            const date = new Date(slot.scheduled_at)
            const isToday = slot.scheduled_at.slice(0, 10) === today
            const isOpen = expandedSlot === slot.id
            const hasStrategy = !!(slot.narrative || slot.intent || slot.hook_idea || slot.strategic_reasoning)
            return (
              <div key={slot.id} className={`card py-3 ${isToday ? 'border-violet-700 bg-violet-900/5' : ''}`}>
                <div className="flex items-center gap-3">
                  <div className="w-12 text-center shrink-0">
                    <p className="text-[10px] text-gray-400">{date.toLocaleDateString('pt-BR', { weekday: 'short' })}</p>
                    <p className={`text-base font-bold ${isToday ? 'text-violet-400' : 'text-white'}`}>{date.getDate()}</p>
                    <p className="text-[10px] text-gray-500">{date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}</p>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span className={`badge border text-[10px] ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                        {OBJECTIVE_LABELS[slot.objective] || slot.objective}
                      </span>
                      <span className="text-xs text-gray-400">{FORMAT_LABELS[slot.format] || slot.format}</span>
                      <span className="text-xs text-gray-500">{slot.platform}</span>
                    </div>
                    {slot.hook_idea && !isOpen && (
                      <p className="text-xs text-gray-300 mt-1 truncate">💡 {slot.hook_idea}</p>
                    )}
                    {slot.intent && !isOpen && !slot.hook_idea && (
                      <p className="text-xs text-gray-400 mt-1 truncate">{slot.intent}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={`text-xs ${slot.content_id ? 'text-green-400' : 'text-gray-600'}`}>
                      {slot.content_id ? '●' : '○'}
                    </span>
                    <button onClick={() => setExpandedSlot(isOpen ? null : slot.id)} className="text-[10px] text-violet-400 hover:text-violet-300">
                      {isOpen ? 'fechar' : hasStrategy ? 'estratégia' : '+'}
                    </button>
                  </div>
                </div>
                {isOpen && <SlotEditor slot={slot} onSaved={load} />}
              </div>
            )
          })}
        </div>
      )}

      {!loading && slots.length === 0 && (
        <div className="card text-center py-12">
          <p className="text-gray-500 text-sm mb-1">Nenhum slot planejado</p>
          <p className="text-gray-600 text-xs">Clique em "Gerar semana" para criar o calendário</p>
        </div>
      )}
    </div>
  )
}

function SlotEditor({ slot, onSaved }: { slot: CalendarSlot; onSaved: () => void }) {
  const [narrative, setNarrative] = useState(slot.narrative || '')
  const [intent, setIntent] = useState(slot.intent || '')
  const [hookIdea, setHookIdea] = useState(slot.hook_idea || '')
  const [reasoning, setReasoning] = useState(slot.strategic_reasoning || '')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  async function save() {
    setSaving(true); setMsg('')
    try {
      await api.calendar.updateSlot(slot.id, {
        narrative: narrative || undefined,
        intent: intent || undefined,
        hook_idea: hookIdea || undefined,
        strategic_reasoning: reasoning || undefined,
      })
      setMsg('✓ Salvo')
      onSaved()
      setTimeout(() => setMsg(''), 1500)
    } catch (e: any) {
      setMsg(`Erro: ${e?.message?.slice(0, 80) || 'falha'}`)
    } finally { setSaving(false) }
  }

  return (
    <div className="mt-3 pt-3 border-t border-gray-800 space-y-2">
      <div>
        <label className="text-[10px] text-gray-500 font-semibold uppercase">Hook</label>
        <input value={hookIdea} onChange={e => setHookIdea(e.target.value)} placeholder="Ideia do hook em 1 linha" className="input text-xs" />
      </div>
      <div>
        <label className="text-[10px] text-gray-500 font-semibold uppercase">Intenção</label>
        <input value={intent} onChange={e => setIntent(e.target.value)} placeholder="O que a persona deve sentir/fazer" className="input text-xs" />
      </div>
      <div>
        <label className="text-[10px] text-gray-500 font-semibold uppercase">Narrativa</label>
        <textarea rows={2} value={narrative} onChange={e => setNarrative(e.target.value)} placeholder="Qual história/ângulo o post conta" className="input text-xs" />
      </div>
      <div>
        <label className="text-[10px] text-gray-500 font-semibold uppercase">Por que esse dia</label>
        <textarea rows={2} value={reasoning} onChange={e => setReasoning(e.target.value)} placeholder="Justificativa estratégica" className="input text-xs" />
      </div>
      <div className="flex items-center gap-2 justify-end">
        {msg && <span className="text-[10px] text-gray-400">{msg}</span>}
        <button onClick={save} disabled={saving} className="btn-primary text-xs">{saving ? '...' : 'Salvar'}</button>
      </div>
    </div>
  )
}
