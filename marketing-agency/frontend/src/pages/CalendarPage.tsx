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
  const [frequency, setFrequency] = useState(7)
  const [days, setDays] = useState(14)

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
  const days14 = Array.from({ length: 14 }, (_, i) => {
    const d = new Date()
    d.setDate(d.getDate() + i)
    return d.toISOString().slice(0, 10)
  })

  return (
    <div className="p-6 space-y-5 max-w-5xl">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold text-white">Calendário de Conteúdo</h1>
        <div className="flex items-center gap-3">
          <select
            className="input-field w-auto text-xs"
            value={frequency}
            onChange={e => setFrequency(Number(e.target.value))}
          >
            {[3, 4, 5, 6, 7].map(n => (
              <option key={n} value={n}>{n}x por semana</option>
            ))}
          </select>
          <select
            className="input-field w-auto text-xs"
            value={days}
            onChange={e => setDays(Number(e.target.value))}
          >
            <option value={7}>7 dias</option>
            <option value={14}>14 dias</option>
            <option value={30}>30 dias</option>
          </select>
          <button onClick={generate} disabled={generating} className="btn-primary w-auto px-5">
            {generating ? 'Gerando...' : 'Gerar semana'}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-7 gap-2">
        {['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom'].map(d => (
          <div key={d} className="text-center text-xs text-gray-500 font-medium py-1">{d}</div>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-2">
        {days14.map(day => {
          const daySlots = grouped.get(day) || []
          const date = new Date(day + 'T12:00:00')
          const isToday = day === new Date().toISOString().slice(0, 10)

          return (
            <div key={day} className={`min-h-20 rounded-lg border p-2 ${
              isToday ? 'border-violet-600 bg-violet-900/10' : 'border-gray-800 bg-gray-900'
            }`}>
              <p className={`text-xs font-semibold mb-1.5 ${isToday ? 'text-violet-400' : 'text-gray-400'}`}>
                {date.getDate()}
              </p>
              {daySlots.map(slot => (
                <div
                  key={slot.id}
                  className={`text-xs rounded px-1.5 py-1 mb-1 border ${
                    OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'
                  }`}
                >
                  <p className="font-medium truncate">{FORMAT_LABELS[slot.format] || slot.format}</p>
                  <p className="text-[10px] opacity-70">{OBJECTIVE_LABELS[slot.objective] || slot.objective}</p>
                </div>
              ))}
            </div>
          )
        })}
      </div>

      {slots.length > 0 && (
        <div className="card space-y-2">
          <h2 className="text-sm font-semibold text-white mb-3">Lista de publicações</h2>
          {slots.map(slot => {
            const date = new Date(slot.scheduled_at)
            return (
              <div key={slot.id} className="flex items-center gap-3 py-2 border-b border-gray-800 last:border-0">
                <div className="w-20 text-xs text-gray-400">
                  {date.toLocaleDateString('pt-BR', { day: '2-digit', month: '2-digit' })}
                  {' '}
                  {date.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })}
                </div>
                <span className={`badge border ${OBJECTIVE_COLORS[slot.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                  {OBJECTIVE_LABELS[slot.objective] || slot.objective}
                </span>
                <span className="text-xs text-gray-400">{FORMAT_LABELS[slot.format] || slot.format}</span>
                <span className="text-xs text-gray-500">{slot.platform}</span>
                <span className={`ml-auto text-xs ${slot.content_id ? 'text-green-400' : 'text-gray-600'}`}>
                  {slot.content_id ? '● Conteúdo vinculado' : '○ Sem conteúdo'}
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
