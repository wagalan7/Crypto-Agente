import { useEffect, useState } from 'react'
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
  const [frequency, setFrequency] = useState(5)
  const [days, setDays] = useState(14)
  const [view, setView] = useState<'grid' | 'list'>('list')

  async function load() {
    const data: any = await api.calendar.get(id, days)
    setSlots(data)
  }

  useEffect(() => { load() }, [id, days])

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
        </div>
      </div>

      {/* Grid view (desktop only) */}
      {view === 'grid' && (
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
              return (
                <div key={day} className={`min-h-20 rounded-lg border p-1.5 ${isToday ? 'border-violet-600 bg-violet-900/10' : 'border-gray-800 bg-gray-900'}`}>
                  <p className={`text-xs font-semibold mb-1 ${isToday ? 'text-violet-400' : 'text-gray-400'}`}>{date.getDate()}</p>
                  {daySlots.map(slot => (
                    <div key={slot.id} className={`text-[10px] rounded px-1 py-0.5 mb-0.5 border ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
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
      {(view === 'list' || true) && slots.length > 0 && (
        <div className="space-y-2">
          {slots.map(slot => {
            const date = new Date(slot.scheduled_at)
            const isToday = slot.scheduled_at.slice(0, 10) === today
            return (
              <div key={slot.id} className={`card flex items-center gap-3 py-3 ${isToday ? 'border-violet-700 bg-violet-900/5' : ''}`}>
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
                </div>
                <span className={`text-xs shrink-0 ${slot.content_id ? 'text-green-400' : 'text-gray-600'}`}>
                  {slot.content_id ? '●' : '○'}
                </span>
              </div>
            )
          })}
        </div>
      )}

      {slots.length === 0 && (
        <div className="card text-center py-12">
          <p className="text-gray-500 text-sm mb-1">Nenhum slot planejado</p>
          <p className="text-gray-600 text-xs">Clique em "Gerar semana" para criar o calendário</p>
        </div>
      )}
    </div>
  )
}
