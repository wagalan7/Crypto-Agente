import { useEffect, useState } from 'react'
import { api } from '../services/api'

interface Point { score: number; recorded_at: string }

export function ScoreSparkline({ clientId, days = 30 }: { clientId: number; days?: number }) {
  const [points, setPoints] = useState<Point[]>([])

  useEffect(() => {
    api.clients.scoreHistory(clientId, days).then((d: any) => setPoints(d || [])).catch(() => setPoints([]))
  }, [clientId, days])

  if (points.length < 2) {
    return <p className="text-[10px] text-gray-500">Histórico aparecerá após 2+ atualizações de score.</p>
  }

  const w = 200
  const h = 40
  const padding = 2
  const xs = points.map((_, i) => (i / (points.length - 1)) * (w - padding * 2) + padding)
  const minS = Math.min(...points.map(p => p.score))
  const maxS = Math.max(...points.map(p => p.score))
  const range = Math.max(1, maxS - minS)
  const ys = points.map(p => h - padding - ((p.score - minS) / range) * (h - padding * 2))

  const path = points.map((_, i) => `${i === 0 ? 'M' : 'L'} ${xs[i].toFixed(1)} ${ys[i].toFixed(1)}`).join(' ')

  const first = points[0].score
  const last = points[points.length - 1].score
  const delta = last - first
  const trendColor = delta > 0 ? 'text-emerald-400' : delta < 0 ? 'text-red-400' : 'text-gray-400'
  const strokeColor = delta > 0 ? '#34d399' : delta < 0 ? '#f87171' : '#a78bfa'

  return (
    <div className="flex items-center gap-2">
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="overflow-visible">
        <path d={path} fill="none" stroke={strokeColor} strokeWidth="1.5" />
        <circle cx={xs[xs.length - 1]} cy={ys[ys.length - 1]} r="2.5" fill={strokeColor} />
      </svg>
      <div className="text-[10px] leading-tight">
        <p className={trendColor}>{delta > 0 ? '+' : ''}{delta.toFixed(1)}</p>
        <p className="text-gray-500">{points.length}d</p>
      </div>
    </div>
  )
}
