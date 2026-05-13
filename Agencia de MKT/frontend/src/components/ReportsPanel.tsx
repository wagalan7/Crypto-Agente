import { useState, useCallback } from 'react'
import jsPDF from 'jspdf'
import autoTable from 'jspdf-autotable'
import * as XLSX from 'xlsx'

interface Props {
  authHeaders: Record<string, string>
}

interface CampaignRow {
  campaign_id: string
  campaign_name: string
  status: string
  impressions: number
  clicks: number
  ctr: number
  avg_cpc: number
  cost: number
  conversions: number
  error?: string
}

interface TikTokRow {
  campaign_id: string
  campaign_name: string
  impressions: number
  clicks: number
  ctr: number
  cpc: number
  spend: number
  conversions: number
  error?: string
}

interface FbInsights {
  page_impressions?: number
  page_reach?: number
  page_post_engagements?: number
  page_fan_adds?: number
  error?: string
}

type TabType = 'google' | 'facebook' | 'tiktok'

const DATE_RANGES = [
  { value: 'LAST_7_DAYS',  label: 'Últimos 7 dias' },
  { value: 'LAST_30_DAYS', label: 'Últimos 30 dias' },
  { value: 'LAST_90_DAYS', label: 'Últimos 90 dias' },
  { value: 'THIS_MONTH',   label: 'Este mês' },
  { value: 'LAST_MONTH',   label: 'Mês passado' },
]

const STATUS_COLOR: Record<string, string> = {
  ENABLED: 'text-emerald-400',
  PAUSED:  'text-amber-400',
  REMOVED: 'text-red-400',
}

function fmt(n: number, prefix = '') {
  if (n >= 1000) return prefix + (n / 1000).toFixed(1) + 'k'
  return prefix + n.toLocaleString('pt-BR')
}

function fmtBRL(n: number) {
  return 'R$ ' + n.toFixed(2).replace('.', ',')
}

function todayLabel() {
  return new Date().toLocaleDateString('pt-BR')
}

// ── PDF Export ───────────────────────────────────────────────
function exportPDF(
  tab: TabType,
  dateLabel: string,
  campaigns: CampaignRow[],
  tiktokRows: TikTokRow[],
  fbInsights: FbInsights | null,
  suggestions: string,
) {
  const doc = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a4' })
  const platformName = tab === 'google' ? 'Google Ads' : tab === 'tiktok' ? 'TikTok Ads' : 'Facebook'
  const title = `Relatório de Performance — ${platformName}`

  doc.setFontSize(16)
  doc.setTextColor(120, 80, 220)
  doc.text(title, 14, 18)
  doc.setFontSize(9)
  doc.setTextColor(120, 120, 140)
  doc.text(`Período: ${dateLabel}   ·   Gerado em: ${todayLabel()}   ·   Maga One`, 14, 25)

  let y = 32

  if (tab === 'google' && campaigns.length > 0) {
    const totals = campaigns.reduce((a, c) => ({
      impressions: a.impressions + c.impressions,
      clicks:      a.clicks + c.clicks,
      cost:        a.cost + c.cost,
      conversions: a.conversions + c.conversions,
    }), { impressions: 0, clicks: 0, cost: 0, conversions: 0 })
    const avgCtr = totals.impressions > 0
      ? ((totals.clicks / totals.impressions) * 100).toFixed(2) + '%'
      : '0.00%'

    autoTable(doc, {
      startY: y,
      head: [['Impressões', 'Cliques', 'CTR Médio', 'Gasto Total', 'Conversões']],
      body: [[fmt(totals.impressions), fmt(totals.clicks), avgCtr, fmtBRL(totals.cost), totals.conversions.toString()]],
      theme: 'grid',
      headStyles: { fillColor: [80, 40, 180] },
      styles: { fontSize: 9 },
    })
    y = (doc as any).lastAutoTable.finalY + 6

    autoTable(doc, {
      startY: y,
      head: [['Campanha', 'Status', 'Impressões', 'Cliques', 'CTR', 'CPC Médio', 'Gasto', 'Conversões']],
      body: campaigns.map(c => [
        c.campaign_name || '—',
        c.status === 'ENABLED' ? 'Ativa' : c.status === 'PAUSED' ? 'Pausada' : c.status,
        fmt(c.impressions), fmt(c.clicks), c.ctr + '%', fmtBRL(c.avg_cpc), fmtBRL(c.cost), c.conversions,
      ]),
      theme: 'striped',
      headStyles: { fillColor: [60, 60, 90] },
      styles: { fontSize: 8, cellWidth: 'auto' },
    })
    y = (doc as any).lastAutoTable.finalY + 6
  }

  if (tab === 'tiktok' && tiktokRows.length > 0) {
    const totals = tiktokRows.reduce((a, c) => ({
      impressions: a.impressions + c.impressions,
      clicks:      a.clicks + c.clicks,
      spend:       a.spend + c.spend,
      conversions: a.conversions + c.conversions,
    }), { impressions: 0, clicks: 0, spend: 0, conversions: 0 })

    autoTable(doc, {
      startY: y,
      head: [['Impressões', 'Cliques', 'Gasto Total', 'Conversões']],
      body: [[fmt(totals.impressions), fmt(totals.clicks), fmtBRL(totals.spend), totals.conversions.toString()]],
      theme: 'grid',
      headStyles: { fillColor: [200, 30, 80] },
      styles: { fontSize: 9 },
    })
    y = (doc as any).lastAutoTable.finalY + 6

    autoTable(doc, {
      startY: y,
      head: [['Campanha', 'Impressões', 'Cliques', 'CTR (%)', 'CPC (R$)', 'Gasto (R$)', 'Conversões']],
      body: tiktokRows.map(c => [
        c.campaign_name || '—',
        fmt(c.impressions), fmt(c.clicks), c.ctr + '%', fmtBRL(c.cpc), fmtBRL(c.spend), c.conversions,
      ]),
      theme: 'striped',
      headStyles: { fillColor: [60, 60, 90] },
      styles: { fontSize: 8 },
    })
    y = (doc as any).lastAutoTable.finalY + 6
  }

  if (tab === 'facebook' && fbInsights) {
    autoTable(doc, {
      startY: y,
      head: [['Alcance', 'Impressões', 'Engajamentos', 'Novos Seguidores']],
      body: [[
        fmt(fbInsights.page_reach ?? 0),
        fmt(fbInsights.page_impressions ?? 0),
        fmt(fbInsights.page_post_engagements ?? 0),
        fmt(fbInsights.page_fan_adds ?? 0),
      ]],
      theme: 'grid',
      headStyles: { fillColor: [30, 80, 180] },
      styles: { fontSize: 9 },
    })
    y = (doc as any).lastAutoTable.finalY + 6
  }

  if (suggestions) {
    const lines = suggestions.split('\n').filter(l => l.trim())
    doc.setFontSize(10)
    doc.setTextColor(160, 120, 255)
    doc.text('✦ Sugestões de Otimização (IA)', 14, y + 5)
    y += 10
    doc.setFontSize(8)
    doc.setTextColor(200, 200, 200)
    lines.forEach(line => {
      const wrapped = doc.splitTextToSize(line, 260)
      doc.text(wrapped, 14, y)
      y += wrapped.length * 4.5
    })
  }

  doc.save(`relatorio-${tab}-${new Date().toISOString().slice(0, 10)}.pdf`)
}

// ── Excel Export ─────────────────────────────────────────────
function exportExcel(
  tab: TabType,
  dateLabel: string,
  campaigns: CampaignRow[],
  tiktokRows: TikTokRow[],
  fbInsights: FbInsights | null,
) {
  const wb = XLSX.utils.book_new()

  if (tab === 'google' && campaigns.length > 0) {
    const rows = campaigns.map(c => ({
      Campanha:    c.campaign_name || '—',
      Status:      c.status,
      Impressões:  c.impressions,
      Cliques:     c.clicks,
      'CTR (%)':   c.ctr,
      'CPC (R$)':  c.avg_cpc,
      'Gasto (R$)': c.cost,
      Conversões:  c.conversions,
    }))
    const ws = XLSX.utils.json_to_sheet(rows)
    XLSX.utils.book_append_sheet(wb, ws, 'Google Ads')
  }

  if (tab === 'tiktok' && tiktokRows.length > 0) {
    const rows = tiktokRows.map(c => ({
      Campanha:    c.campaign_name || '—',
      Impressões:  c.impressions,
      Cliques:     c.clicks,
      'CTR (%)':   c.ctr,
      'CPC (R$)':  c.cpc,
      'Gasto (R$)': c.spend,
      Conversões:  c.conversions,
    }))
    const ws = XLSX.utils.json_to_sheet(rows)
    XLSX.utils.book_append_sheet(wb, ws, 'TikTok Ads')
  }

  if (tab === 'facebook' && fbInsights) {
    const rows = [{
      Período:          dateLabel,
      Alcance:          fbInsights.page_reach ?? 0,
      Impressões:       fbInsights.page_impressions ?? 0,
      Engajamentos:     fbInsights.page_post_engagements ?? 0,
      'Novos Seguidores': fbInsights.page_fan_adds ?? 0,
    }]
    const ws = XLSX.utils.json_to_sheet(rows)
    XLSX.utils.book_append_sheet(wb, ws, 'Facebook')
  }

  XLSX.writeFile(wb, `relatorio-${tab}-${new Date().toISOString().slice(0, 10)}.xlsx`)
}

// ─────────────────────────────────────────────────────────────

export function ReportsPanel({ authHeaders }: Props) {
  const [tab, setTab]               = useState<TabType>('google')
  const [dateRange, setDateRange]   = useState('LAST_30_DAYS')
  const [loading, setLoading]           = useState(false)
  const [campaigns, setCampaigns]       = useState<CampaignRow[]>([])
  const [tiktokRows, setTiktokRows]     = useState<TikTokRow[]>([])
  const [fbInsights, setFbInsights]     = useState<FbInsights | null>(null)
  const [error, setError]               = useState('')
  const [fetched, setFetched]           = useState(false)
  const [optimizing, setOptimizing]     = useState(false)
  const [suggestions, setSuggestions]   = useState('')
  const [tokensUsed, setTokensUsed]     = useState(0)
  const [toggling, setToggling]         = useState<Set<string>>(new Set())
  const [toggleMsg, setToggleMsg]       = useState('')

  const fetchGoogle = useCallback(async (range: string) => {
    setLoading(true); setError(''); setCampaigns([])
    try {
      const r = await fetch(`/reports/google-ads?date_range=${range}`, { headers: authHeaders })
      const d = await r.json()
      if (!r.ok) { setError(d.detail || 'Erro ao buscar dados'); return }
      const rows: CampaignRow[] = d.campaigns || []
      if (rows.length === 1 && rows[0].error) { setError(rows[0].error); return }
      setCampaigns(rows); setFetched(true)
    } catch (e) { setError(String(e)) }
    setLoading(false)
  }, [authHeaders])

  const fetchFacebook = useCallback(async () => {
    setLoading(true); setError(''); setFbInsights(null)
    try {
      const r = await fetch('/reports/facebook?date_preset=last_30d', { headers: authHeaders })
      const d = await r.json()
      if (!r.ok) { setError(d.detail || 'Erro ao buscar dados'); return }
      const ins = d.insights?.[0] || {}
      if (ins.error) { setError(ins.error); return }
      setFbInsights(ins); setFetched(true)
    } catch (e) { setError(String(e)) }
    setLoading(false)
  }, [authHeaders])

  const fetchTikTok = useCallback(async (range: string) => {
    setLoading(true); setError(''); setTiktokRows([])
    try {
      const r = await fetch(`/reports/tiktok?date_range=${range}`, { headers: authHeaders })
      const d = await r.json()
      if (!r.ok) { setError(d.detail || 'Erro ao buscar dados'); return }
      const rows: TikTokRow[] = d.campaigns || []
      if (rows.length === 1 && rows[0].error) { setError(rows[0].error); return }
      setTiktokRows(rows); setFetched(true)
    } catch (e) { setError(String(e)) }
    setLoading(false)
  }, [authHeaders])

  const handleFetch = () => {
    setSuggestions(''); setTokensUsed(0)
    if (tab === 'google')   fetchGoogle(dateRange)
    else if (tab === 'tiktok') fetchTikTok(dateRange)
    else fetchFacebook()
  }

  const handleOptimize = useCallback(async () => {
    const data = tab === 'tiktok'
      ? tiktokRows.map(r => ({ ...r, status: 'ENABLED', avg_cpc: r.cpc, cost: r.spend }))
      : campaigns
    if (!data.length) return
    setOptimizing(true); setSuggestions('')
    try {
      const r = await fetch('/reports/optimize', {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ campaigns: data, platform: tab }),
      })
      const d = await r.json()
      if (!r.ok) { setError(d.detail || 'Erro ao otimizar'); return }
      setSuggestions(d.suggestions || ''); setTokensUsed(d.tokens_used || 0)
    } catch (e) { setError(String(e)) }
    setOptimizing(false)
  }, [campaigns, tiktokRows, tab, authHeaders])

  const handleToggle = useCallback(async (campaignId: string, currentStatus: string) => {
    const newStatus = currentStatus === 'ENABLED' ? 'PAUSED' : 'ENABLED'
    setToggling(t => new Set(t).add(campaignId))
    setToggleMsg('')
    try {
      const r = await fetch('/reports/google-ads/toggle', {
        method: 'POST', headers: authHeaders,
        body: JSON.stringify({ campaign_id: campaignId, new_status: newStatus }),
      })
      const d = await r.json()
      if (r.ok) {
        setCampaigns(prev => prev.map(c =>
          c.campaign_id === campaignId ? { ...c, status: newStatus } : c
        ))
        setToggleMsg(`✓ Campanha ${newStatus === 'PAUSED' ? 'pausada' : 'ativada'} com sucesso`)
        setTimeout(() => setToggleMsg(''), 3000)
      } else {
        setToggleMsg(`✗ ${d.detail || 'Erro ao alterar status'}`)
      }
    } catch (e) { setToggleMsg(`✗ ${String(e)}`) }
    setToggling(t => { const n = new Set(t); n.delete(campaignId); return n })
  }, [authHeaders])

  const handleExportPDF = () => {
    const label = DATE_RANGES.find(d => d.value === dateRange)?.label ?? dateRange
    exportPDF(tab, label, campaigns, tiktokRows, fbInsights, suggestions)
  }

  const handleExportExcel = () => {
    const label = DATE_RANGES.find(d => d.value === dateRange)?.label ?? dateRange
    exportExcel(tab, label, campaigns, tiktokRows, fbInsights)
  }

  const switchTab = (t: TabType) => {
    setTab(t); setFetched(false); setCampaigns([]); setTiktokRows([])
    setFbInsights(null); setError(''); setSuggestions('')
  }

  // Google/TikTok totals
  const gTotals = campaigns.reduce((acc, c) => ({
    impressions: acc.impressions + c.impressions,
    clicks:      acc.clicks + c.clicks,
    cost:        acc.cost + c.cost,
    conversions: acc.conversions + c.conversions,
  }), { impressions: 0, clicks: 0, cost: 0, conversions: 0 })
  const gAvgCtr = gTotals.impressions > 0
    ? ((gTotals.clicks / gTotals.impressions) * 100).toFixed(2) : '0.00'

  const tTotals = tiktokRows.reduce((acc, c) => ({
    impressions: acc.impressions + c.impressions,
    clicks:      acc.clicks + c.clicks,
    spend:       acc.spend + c.spend,
    conversions: acc.conversions + c.conversions,
  }), { impressions: 0, clicks: 0, spend: 0, conversions: 0 })
  const tAvgCtr = tTotals.impressions > 0
    ? ((tTotals.clicks / tTotals.impressions) * 100).toFixed(2) : '0.00'

  const hasData = fetched && !error && (campaigns.length > 0 || tiktokRows.length > 0 || !!fbInsights)

  return (
    <div className="bg-gray-900/60 border border-gray-800 rounded-xl overflow-hidden">
      {/* Header */}
      <div className="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg">📊</span>
          <span className="text-sm font-bold text-gray-200 tracking-wide">RELATÓRIOS DE PERFORMANCE</span>
        </div>
        <div className="flex items-center gap-1.5">
          {([
            { id: 'google',   icon: 'G',  label: 'Google Ads', active: 'border-yellow-600 bg-yellow-900/30 text-yellow-300' },
            { id: 'facebook', icon: '𝕗',  label: 'Facebook',   active: 'border-blue-600 bg-blue-900/30 text-blue-300' },
            { id: 'tiktok',   icon: '♪',  label: 'TikTok',     active: 'border-rose-600 bg-rose-900/30 text-rose-300' },
          ] as const).map(t => (
            <button key={t.id}
              onClick={() => switchTab(t.id)}
              className={`text-[11px] px-2.5 py-1 rounded-lg border transition-all
                ${tab === t.id ? t.active : 'border-gray-700 text-gray-500 hover:text-gray-300'}`}>
              {t.icon} {t.label}
            </button>
          ))}
        </div>
      </div>

      <div className="px-5 py-4 space-y-4">
        {/* Controls */}
        <div className="flex items-center gap-3 flex-wrap">
          {tab !== 'facebook' && (
            <select
              value={dateRange}
              onChange={e => setDateRange(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200
                         focus:outline-none focus:border-violet-500">
              {DATE_RANGES.map(d => (
                <option key={d.value} value={d.value}>{d.label}</option>
              ))}
            </select>
          )}
          <button
            onClick={handleFetch}
            disabled={loading}
            className="px-4 py-1.5 rounded-lg text-xs font-semibold text-white
              bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
              disabled:opacity-50 transition-all flex items-center gap-1.5">
            {loading
              ? <><span className="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin" />Buscando...</>
              : '⟳ Buscar dados'}
          </button>

          {/* Export buttons — only when data is available */}
          {hasData && (
            <div className="flex items-center gap-1.5 ml-auto">
              <button
                onClick={handleExportPDF}
                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-[11px] font-medium
                  border border-red-800 text-red-400 hover:bg-red-900/20 transition-all">
                ⬇ PDF
              </button>
              <button
                onClick={handleExportExcel}
                className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-[11px] font-medium
                  border border-emerald-800 text-emerald-400 hover:bg-emerald-900/20 transition-all">
                ⬇ Excel
              </button>
            </div>
          )}
        </div>

        {/* Error */}
        {error && (
          <div className="px-3 py-2 bg-red-900/30 border border-red-800 rounded-lg text-xs text-red-400">
            {error}
          </div>
        )}

        {/* Google Ads Results */}
        {tab === 'google' && fetched && campaigns.length > 0 && (
          <div className="space-y-3">
            <div className="grid grid-cols-4 gap-2">
              {[
                { label: 'Impressões', value: fmt(gTotals.impressions), color: 'text-blue-400' },
                { label: 'Cliques',    value: fmt(gTotals.clicks),      color: 'text-violet-400' },
                { label: 'CTR médio',  value: gAvgCtr + '%',            color: 'text-emerald-400' },
                { label: 'Gasto total',value: fmtBRL(gTotals.cost),     color: 'text-amber-400' },
              ].map(k => (
                <div key={k.label} className="bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2.5 text-center">
                  <p className={`text-base font-bold ${k.color}`}>{k.value}</p>
                  <p className="text-[9px] text-gray-500 mt-0.5 uppercase tracking-wider">{k.label}</p>
                </div>
              ))}
            </div>

            {toggleMsg && (
              <p className={`text-[11px] px-3 py-1.5 rounded-lg border ${toggleMsg.startsWith('✓')
                ? 'text-emerald-400 border-emerald-800 bg-emerald-900/20'
                : 'text-red-400 border-red-800 bg-red-900/20'}`}>
                {toggleMsg}
              </p>
            )}

            <div className="overflow-x-auto rounded-lg border border-gray-700">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="bg-gray-800/80 text-gray-400 uppercase tracking-wider">
                    {['Campanha', 'Status', 'Impressões', 'Cliques', 'CTR', 'CPC Médio', 'Gasto', 'Conversões', ''].map(h => (
                      <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {campaigns.map((c, i) => {
                    const isToggling = toggling.has(c.campaign_id)
                    return (
                      <tr key={i} className={`border-t border-gray-800 ${i % 2 === 0 ? 'bg-gray-900/40' : ''}`}>
                        <td className="px-3 py-2 text-gray-200 max-w-[160px] truncate font-medium">{c.campaign_name || '—'}</td>
                        <td className="px-3 py-2">
                          <span className={`text-[10px] font-semibold ${STATUS_COLOR[c.status] || 'text-gray-400'}`}>
                            {c.status === 'ENABLED' ? '● Ativa' : c.status === 'PAUSED' ? '⏸ Pausada' : c.status}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-gray-300">{fmt(c.impressions)}</td>
                        <td className="px-3 py-2 text-gray-300">{fmt(c.clicks)}</td>
                        <td className="px-3 py-2 text-gray-300">{c.ctr}%</td>
                        <td className="px-3 py-2 text-gray-300">{fmtBRL(c.avg_cpc)}</td>
                        <td className="px-3 py-2 text-amber-400 font-semibold">{fmtBRL(c.cost)}</td>
                        <td className="px-3 py-2 text-emerald-400">{c.conversions}</td>
                        <td className="px-3 py-2">
                          {c.campaign_id && (
                            <button
                              onClick={() => handleToggle(c.campaign_id, c.status)}
                              disabled={isToggling}
                              className={`text-[9px] px-2 py-1 rounded border font-medium transition-all disabled:opacity-40
                                ${c.status === 'ENABLED'
                                  ? 'border-amber-700 text-amber-400 hover:bg-amber-900/20'
                                  : 'border-emerald-700 text-emerald-400 hover:bg-emerald-900/20'}`}>
                              {isToggling ? '...' : c.status === 'ENABLED' ? '⏸ Pausar' : '▶ Ativar'}
                            </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            <div className="flex items-center justify-between">
              <p className="text-[9px] text-gray-600">
                {DATE_RANGES.find(d => d.value === dateRange)?.label} · {campaigns.length} campanha{campaigns.length !== 1 ? 's' : ''}
              </p>
              <button onClick={handleOptimize} disabled={optimizing}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold
                  bg-gradient-to-r from-violet-700 to-blue-700 hover:from-violet-600 hover:to-blue-600
                  disabled:opacity-50 text-white transition-all">
                {optimizing
                  ? <><span className="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin"/>Analisando...</>
                  : '✦ Otimizar com IA'}
              </button>
            </div>
          </div>
        )}

        {/* TikTok Results */}
        {tab === 'tiktok' && fetched && tiktokRows.length > 0 && (
          <div className="space-y-3">
            <div className="grid grid-cols-4 gap-2">
              {[
                { label: 'Impressões', value: fmt(tTotals.impressions), color: 'text-rose-400' },
                { label: 'Cliques',    value: fmt(tTotals.clicks),      color: 'text-violet-400' },
                { label: 'CTR médio',  value: tAvgCtr + '%',            color: 'text-emerald-400' },
                { label: 'Gasto total',value: fmtBRL(tTotals.spend),    color: 'text-amber-400' },
              ].map(k => (
                <div key={k.label} className="bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-2.5 text-center">
                  <p className={`text-base font-bold ${k.color}`}>{k.value}</p>
                  <p className="text-[9px] text-gray-500 mt-0.5 uppercase tracking-wider">{k.label}</p>
                </div>
              ))}
            </div>

            <div className="overflow-x-auto rounded-lg border border-gray-700">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="bg-gray-800/80 text-gray-400 uppercase tracking-wider">
                    {['Campanha', 'Impressões', 'Cliques', 'CTR', 'CPC', 'Gasto', 'Conversões'].map(h => (
                      <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tiktokRows.map((c, i) => (
                    <tr key={i} className={`border-t border-gray-800 ${i % 2 === 0 ? 'bg-gray-900/40' : ''}`}>
                      <td className="px-3 py-2 text-gray-200 max-w-[200px] truncate font-medium">{c.campaign_name || '—'}</td>
                      <td className="px-3 py-2 text-gray-300">{fmt(c.impressions)}</td>
                      <td className="px-3 py-2 text-gray-300">{fmt(c.clicks)}</td>
                      <td className="px-3 py-2 text-gray-300">{c.ctr}%</td>
                      <td className="px-3 py-2 text-gray-300">{fmtBRL(c.cpc)}</td>
                      <td className="px-3 py-2 text-amber-400 font-semibold">{fmtBRL(c.spend)}</td>
                      <td className="px-3 py-2 text-emerald-400">{c.conversions}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="flex items-center justify-between">
              <p className="text-[9px] text-gray-600">
                {DATE_RANGES.find(d => d.value === dateRange)?.label} · {tiktokRows.length} campanha{tiktokRows.length !== 1 ? 's' : ''}
              </p>
              <button onClick={handleOptimize} disabled={optimizing}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[11px] font-semibold
                  bg-gradient-to-r from-rose-700 to-pink-700 hover:from-rose-600 hover:to-pink-600
                  disabled:opacity-50 text-white transition-all">
                {optimizing
                  ? <><span className="w-3 h-3 border-2 border-white/30 border-t-white rounded-full animate-spin"/>Analisando...</>
                  : '✦ Otimizar com IA'}
              </button>
            </div>
          </div>
        )}

        {/* Facebook Results */}
        {tab === 'facebook' && fetched && fbInsights && !fbInsights.error && (
          <div className="grid grid-cols-2 gap-3">
            {[
              { label: 'Alcance da Página',  value: fbInsights.page_reach ?? 0,            color: 'text-blue-400' },
              { label: 'Impressões',         value: fbInsights.page_impressions ?? 0,      color: 'text-violet-400' },
              { label: 'Engajamentos',       value: fbInsights.page_post_engagements ?? 0, color: 'text-emerald-400' },
              { label: 'Novos Seguidores',   value: fbInsights.page_fan_adds ?? 0,         color: 'text-pink-400' },
            ].map(k => (
              <div key={k.label} className="bg-gray-800/60 border border-gray-700 rounded-xl px-4 py-3">
                <p className={`text-2xl font-bold ${k.color}`}>{fmt(k.value)}</p>
                <p className="text-[10px] text-gray-500 mt-1">{k.label}</p>
                <p className="text-[9px] text-gray-600">Últimos 30 dias</p>
              </div>
            ))}
          </div>
        )}

        {/* AI Suggestions */}
        {suggestions && (
          <div className="bg-violet-950/30 border border-violet-800/50 rounded-xl p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-xs font-bold text-violet-300 flex items-center gap-1.5">
                <span>✦</span> Sugestões de Otimização
              </p>
              <span className="text-[9px] text-gray-600">{tokensUsed} tokens usados</span>
            </div>
            <div className="space-y-1.5">
              {suggestions.split('\n').filter(l => l.trim()).map((line, i) => (
                <p key={i} className="text-[11px] text-gray-300 leading-relaxed">{line}</p>
              ))}
            </div>
          </div>
        )}

        {/* Empty state */}
        {!loading && !error && !fetched && (
          <div className="text-center py-8">
            <p className="text-3xl mb-2">📈</p>
            <p className="text-xs text-gray-500">
              Selecione o período e clique em "Buscar dados"<br />para ver a performance das suas campanhas.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
