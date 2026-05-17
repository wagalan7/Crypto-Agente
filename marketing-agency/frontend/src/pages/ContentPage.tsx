import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api } from '../services/api'
import type { ContentPiece, ContentVersion } from '../types'
import { STATUS_LABELS, STATUS_COLORS, FORMAT_LABELS, OBJECTIVE_LABELS, OBJECTIVE_COLORS, FUNNEL_STAGE_LABELS } from '../types'
import { SectionRegenButton } from '../components/SectionRegenButton'

const STATUSES = ['pending', 'approved', 'recorded', 'published'] as const

export function ContentPage() {
  const { clientId } = useParams<{ clientId: string }>()
  const id = Number(clientId)
  const [contents, setContents] = useState<ContentPiece[]>([])
  const [filter, setFilter] = useState<string>('')
  const [search, setSearch] = useState<string>('')
  const [debouncedSearch, setDebouncedSearch] = useState<string>('')
  const [pageSize, setPageSize] = useState(20)
  const [listLoading, setListLoading] = useState(true)
  const [selected, setSelected] = useState<ContentPiece | null>(null)
  const [mediaUrl, setMediaUrl] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [approving, setApproving] = useState(false)
  const [regenBrief, setRegenBrief] = useState(false)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [bulkBusy, setBulkBusy] = useState(false)
  const [hookVarLoading, setHookVarLoading] = useState(false)
  const [hookVariations, setHookVariations] = useState<Array<{ style: string; hook: string }> | null>(null)
  const [voiceBusy, setVoiceBusy] = useState(false)
  const [remixOpen, setRemixOpen] = useState(false)
  const [remixFmt, setRemixFmt] = useState('carousel')
  const [remixPlat, setRemixPlat] = useState('instagram')
  const [remixInstr, setRemixInstr] = useState('')
  const [remixBusy, setRemixBusy] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [versions, setVersions] = useState<ContentVersion[] | null>(null)
  const [versionsLoading, setVersionsLoading] = useState(false)
  const [requestChangesOpen, setRequestChangesOpen] = useState(false)
  const [changesNote, setChangesNote] = useState('')
  const [changesBusy, setChangesBusy] = useState(false)
  const [humanizeBusy, setHumanizeBusy] = useState(false)
  const [alignLoading, setAlignLoading] = useState(false)
  const [alignResult, setAlignResult] = useState<{
    best_match: string | null
    alignment_score: number
    strengths: string[]
    divergences: string[]
    adjustment_suggestion: string
  } | null>(null)

  async function load() {
    setListLoading(true)
    try {
      const data: any = await api.content.list(id, filter || undefined)
      setContents(data)
    } finally { setListLoading(false) }
  }

  useEffect(() => { load() }, [id, filter])

  // Debounce search input (250ms) so we don't filter on every keystroke
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim().toLowerCase()), 250)
    return () => clearTimeout(t)
  }, [search])

  // Reset page size when filter or search changes
  useEffect(() => { setPageSize(20) }, [filter, debouncedSearch])

  const filtered = debouncedSearch
    ? contents.filter(c => {
        const hay = `${c.title || ''} ${c.hook || ''} ${c.platform || ''} ${c.format || ''} ${c.linked_product_name || ''}`.toLowerCase()
        return hay.includes(debouncedSearch)
      })
    : contents
  const visible = filtered.slice(0, pageSize)

  useEffect(() => {
    if (selected) setMediaUrl(selected.media_url || '')
  }, [selected?.id])

  async function approve(contentId: number) {
    setApproving(true)
    try {
      const updated: any = await api.content.approve(contentId)
      setContents(prev => prev.map(c => c.id === contentId ? updated : c))
      if (selected?.id === contentId) setSelected(updated)
    } finally {
      setApproving(false)
    }
  }

  async function regenerateBrief(contentId: number) {
    setRegenBrief(true)
    try {
      const updated: any = await api.content.regenerateBrief(contentId)
      setContents(prev => prev.map(c => c.id === contentId ? updated : c))
      if (selected?.id === contentId) setSelected(updated)
    } catch (e: any) {
      alert('Erro ao gerar briefing: ' + e.message)
    } finally {
      setRegenBrief(false)
    }
  }

  async function setStatus(contentId: number, status: string) {
    const updated: any = await api.content.update(contentId, { status })
    setContents(prev => prev.map(c => c.id === contentId ? updated : c))
    if (selected?.id === contentId) setSelected(updated)
  }

  async function saveMediaUrl() {
    if (!selected) return
    const updated: any = await api.content.update(selected.id, { media_url: mediaUrl || null })
    setContents(prev => prev.map(c => c.id === selected.id ? updated : c))
    setSelected(updated)
  }

  function toggleSelect(cid: number) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(cid)) next.delete(cid); else next.add(cid)
      return next
    })
  }
  function selectAllVisible() {
    setSelectedIds(new Set(contents.map(c => c.id)))
  }
  function clearSelection() { setSelectedIds(new Set()) }

  async function bulkApprove() {
    if (selectedIds.size === 0) return
    if (!confirm(`Aprovar ${selectedIds.size} conteúdo(s)? A IA vai gerar briefing de produção pra cada um.`)) return
    setBulkBusy(true)
    try {
      const res: any = await api.content.bulkApprove(Array.from(selectedIds))
      await load()
      clearSelection()
      alert(`✓ ${res.approved?.length || 0} aprovado(s)${res.failed?.length ? ` · ${res.failed.length} falharam` : ''}`)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setBulkBusy(false) }
  }

  async function bulkDelete() {
    if (selectedIds.size === 0) return
    if (!confirm(`Excluir ${selectedIds.size} conteúdo(s)? Essa ação não pode ser desfeita.`)) return
    setBulkBusy(true)
    try {
      const res: any = await api.content.bulkDelete(Array.from(selectedIds))
      await load()
      clearSelection()
      alert(`✓ ${res.deleted || 0} excluído(s)`)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setBulkBusy(false) }
  }

  async function generateHookVariations() {
    if (!selected) return
    setHookVarLoading(true)
    setHookVariations(null)
    try {
      const res: any = await api.content.hookVariations(selected.id, 3)
      setHookVariations(res.variations || [])
    } catch (e: any) {
      alert('Erro ao gerar variações: ' + e.message)
    } finally { setHookVarLoading(false) }
  }

  async function selectHookVariation(hook: string, style?: string) {
    if (!selected) return
    const updated: any = await api.content.selectHook(selected.id, hook, style)
    setContents(prev => prev.map(c => c.id === selected.id ? updated : c))
    setSelected(updated)
    setHookVariations(null)
  }

  async function runRemix() {
    if (!selected) return
    setRemixBusy(true)
    try {
      const created: any = await api.content.repurpose(selected.id, {
        target_format: remixFmt,
        target_platform: remixPlat,
        instruction: remixInstr || undefined,
      })
      await load()
      setRemixOpen(false)
      setRemixInstr('')
      setSelected(created)
      alert('✓ Nova peça criada — você está vendo a adaptação')
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setRemixBusy(false) }
  }

  async function runInspirationAlignment() {
    if (!selected) return
    setAlignLoading(true)
    setAlignResult(null)
    try {
      const r: any = await api.content.inspirationAlignment(selected.id)
      setAlignResult(r)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setAlignLoading(false) }
  }

  // Clear alignment when switching pieces
  useEffect(() => { setAlignResult(null) }, [selected?.id])

  async function runVoiceScore() {
    if (!selected) return
    setVoiceBusy(true)
    try {
      const updated: any = await api.content.voiceScore(selected.id)
      setContents(prev => prev.map(c => c.id === updated.id ? updated : c))
      setSelected(updated)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setVoiceBusy(false) }
  }

  async function openVersions() {
    if (!selected) return
    setVersionsOpen(true)
    setVersionsLoading(true)
    try {
      const r: any = await api.content.versions(selected.id)
      setVersions(r)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setVersionsLoading(false) }
  }

  async function restoreVersion(versionId: number) {
    if (!selected) return
    if (!confirm('Restaurar esta versão? A versão atual será arquivada antes.')) return
    try {
      const updated: any = await api.content.restoreVersion(selected.id, versionId)
      setContents(prev => prev.map(c => c.id === updated.id ? updated : c))
      setSelected(updated)
      setVersionsOpen(false)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    }
  }

  async function submitRequestChanges() {
    if (!selected || !changesNote.trim()) return
    setChangesBusy(true)
    try {
      const updated: any = await api.content.requestChanges(selected.id, changesNote)
      setContents(prev => prev.map(c => c.id === updated.id ? updated : c))
      setSelected(updated)
      setRequestChangesOpen(false)
      setChangesNote('')
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setChangesBusy(false) }
  }

  async function runHumanize() {
    if (!selected) return
    if (!confirm('Reescrever as seções na voz da marca? (afeta hook, copy, script)')) return
    setHumanizeBusy(true)
    try {
      const updated: any = await api.content.humanize(selected.id)
      setContents(prev => prev.map(c => c.id === updated.id ? updated : c))
      setSelected(updated)
    } catch (e: any) {
      alert('Erro: ' + e.message)
    } finally { setHumanizeBusy(false) }
  }

  async function publishNow() {
    if (!selected) return
    if (!confirm(`Publicar agora no ${selected.platform}?`)) return
    setPublishing(true)
    try {
      await api.social.publish(selected.id)
      const refreshed: any = await api.content.get(selected.id)
      setContents(prev => prev.map(c => c.id === selected.id ? refreshed : c))
      setSelected(refreshed)
      alert('Publicado com sucesso!')
    } catch (e: any) {
      alert('Erro ao publicar: ' + e.message)
    } finally {
      setPublishing(false)
    }
  }

  // Mobile: show detail as full overlay
  if (selected) {
    return (
      <div className="p-4 md:p-6 max-w-2xl">
        <button onClick={() => setSelected(null)} className="flex items-center gap-1.5 text-sm text-gray-400 mb-4">
          ← Voltar
        </button>
        <div className="space-y-4">
          <div className="flex items-start gap-2 flex-wrap">
            <span className={`badge ${STATUS_COLORS[selected.status]}`}>{STATUS_LABELS[selected.status]}</span>
            <span className="badge bg-gray-800 text-gray-400">{selected.platform}</span>
            <span className="badge bg-gray-800 text-gray-400">{FORMAT_LABELS[selected.format] || selected.format}</span>
            <span className={`badge border ${OBJECTIVE_COLORS[selected.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
              {OBJECTIVE_LABELS[selected.objective] || selected.objective}
            </span>
            {selected.linked_product_name && (
              <span className="badge bg-cyan-900/30 text-cyan-300 border border-cyan-800/60">
                → {selected.linked_product_name}
              </span>
            )}
          </div>

          <h2 className="text-base font-semibold text-white">{selected.title}</h2>

          {selected.hook && (
            <div className="card">
              <div className="flex items-center justify-between mb-1 gap-2 flex-wrap">
                <p className="text-xs text-violet-400 font-semibold">HOOK</p>
                <div className="flex items-center gap-1.5">
                  <button onClick={generateHookVariations} disabled={hookVarLoading}
                    className="text-[10px] text-fuchsia-400 hover:text-fuchsia-300 px-1.5 py-0.5 rounded disabled:opacity-50">
                    {hookVarLoading ? 'Gerando...' : '⚖ A/B 3 variações'}
                  </button>
                  <SectionRegenButton contentId={selected.id} section="hook" onUpdated={(u) => { setSelected(u); setContents(prev => prev.map(c => c.id === u.id ? u : c)) }} />
                </div>
              </div>
              <p className="text-sm text-gray-300">{selected.hook}</p>
              {hookVariations && hookVariations.length > 0 && (
                <div className="mt-2 space-y-1.5 border-t border-fuchsia-800/40 pt-2">
                  <p className="text-[10px] text-fuchsia-400 font-semibold">ESCOLHA O MELHOR HOOK</p>
                  {hookVariations.map((v, i) => (
                    <div key={i} className="flex items-start gap-2 bg-fuchsia-950/30 border border-fuchsia-800/40 rounded p-2">
                      <div className="flex-1 min-w-0">
                        <p className="text-[10px] text-fuchsia-300/80 mb-0.5">{v.style}</p>
                        <p className="text-xs text-gray-200">{v.hook}</p>
                      </div>
                      <button onClick={() => selectHookVariation(v.hook, v.style)} className="text-[10px] px-2 py-1 rounded bg-fuchsia-700 hover:bg-fuchsia-600 text-white shrink-0">
                        Usar
                      </button>
                    </div>
                  ))}
                  <button onClick={() => setHookVariations(null)} className="text-[10px] text-gray-500 hover:text-gray-300">× Cancelar</button>
                </div>
              )}
            </div>
          )}
          {selected.script && (
            <div className="card">
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs text-violet-400 font-semibold">ROTEIRO</p>
                <SectionRegenButton contentId={selected.id} section="script" onUpdated={(u) => { setSelected(u); setContents(prev => prev.map(c => c.id === u.id ? u : c)) }} />
              </div>
              <div className="max-h-60 overflow-y-auto">
                <p className="text-sm text-gray-300 whitespace-pre-wrap">{selected.script}</p>
              </div>
            </div>
          )}
          {selected.copy && (
            <div className="card">
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs text-violet-400 font-semibold">COPY</p>
                <SectionRegenButton contentId={selected.id} section="copy" onUpdated={(u) => { setSelected(u); setContents(prev => prev.map(c => c.id === u.id ? u : c)) }} />
              </div>
              <p className="text-sm text-gray-300">{selected.copy}</p>
            </div>
          )}
          {selected.design_brief && (
            <div className="card">
              <div className="flex items-center justify-between mb-1">
                <p className="text-xs text-violet-400 font-semibold">BRIEFING VISUAL</p>
                <SectionRegenButton contentId={selected.id} section="design_brief" onUpdated={(u) => { setSelected(u); setContents(prev => prev.map(c => c.id === u.id ? u : c)) }} />
              </div>
              <div className="max-h-48 overflow-y-auto">
                <p className="text-sm text-gray-300 whitespace-pre-wrap">{selected.design_brief}</p>
              </div>
            </div>
          )}
          {/* Repurpose / Remix */}
          <div className="card bg-amber-900/10 border-amber-800/50 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-amber-300 font-semibold">♻ REAPROVEITAR EM OUTRO FORMATO</p>
              <button onClick={() => setRemixOpen(o => !o)} className="text-[10px] text-amber-400 hover:text-amber-300">
                {remixOpen ? '× Fechar' : 'Abrir →'}
              </button>
            </div>
            {!remixOpen && (
              <p className="text-[11px] text-gray-500">Transforma esse post numa adaptação pra outro formato/plataforma sem perder o ângulo estratégico.</p>
            )}
            {remixOpen && (
              <div className="space-y-2">
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="text-[10px] text-amber-400 font-semibold">FORMATO ALVO</label>
                    <select value={remixFmt} onChange={e => setRemixFmt(e.target.value)} className="input text-sm w-full">
                      <option value="reel">Reel/Short</option>
                      <option value="carousel">Carrossel</option>
                      <option value="post">Post estático</option>
                      <option value="story">Story</option>
                      <option value="long_video">Vídeo longo (YouTube)</option>
                      <option value="thread">Thread</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-[10px] text-amber-400 font-semibold">PLATAFORMA ALVO</label>
                    <select value={remixPlat} onChange={e => setRemixPlat(e.target.value)} className="input text-sm w-full">
                      <option value="instagram">Instagram</option>
                      <option value="tiktok">TikTok</option>
                      <option value="youtube">YouTube</option>
                      <option value="linkedin">LinkedIn</option>
                      <option value="twitter">Twitter/X</option>
                    </select>
                  </div>
                </div>
                <input value={remixInstr} onChange={e => setRemixInstr(e.target.value)}
                  placeholder="Instrução opcional: 'mais técnico', 'tom analítico'..."
                  className="input text-sm w-full" />
                <button onClick={runRemix} disabled={remixBusy}
                  className="btn-primary text-xs w-full py-2 bg-amber-700 hover:bg-amber-600">
                  {remixBusy ? 'Adaptando...' : '♻ Gerar adaptação'}
                </button>
              </div>
            )}
          </div>

          {/* Inspiration alignment */}
          <div className="card bg-purple-900/10 border-purple-800/50 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-purple-300 font-semibold">🎯 ALINHAMENTO COM INSPIRAÇÕES</p>
              <button onClick={runInspirationAlignment} disabled={alignLoading}
                className="text-[10px] text-purple-400 hover:text-purple-300 disabled:opacity-50">
                {alignLoading ? 'Analisando...' : alignResult ? '↻ Re-analisar' : '✦ Analisar'}
              </button>
            </div>
            {!alignResult && !alignLoading && (
              <p className="text-[11px] text-gray-500">Compara esse post contra as referências que você cadastrou e mostra o que ajustar.</p>
            )}
            {alignResult && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <div className={`text-2xl font-bold ${
                    alignResult.alignment_score >= 80 ? 'text-green-400'
                    : alignResult.alignment_score >= 50 ? 'text-yellow-400'
                    : 'text-red-400'
                  }`}>{alignResult.alignment_score}</div>
                  <div className="text-[10px] text-gray-400">
                    <p>/ 100 de alinhamento</p>
                    {alignResult.best_match && <p className="text-purple-300">Mais próximo: {alignResult.best_match}</p>}
                  </div>
                </div>
                {alignResult.strengths.length > 0 && (
                  <div>
                    <p className="text-[10px] text-green-400 font-semibold">ACERTOS</p>
                    <ul className="text-xs text-gray-300 space-y-0.5">
                      {alignResult.strengths.map((s, i) => <li key={i}>· {s}</li>)}
                    </ul>
                  </div>
                )}
                {alignResult.divergences.length > 0 && (
                  <div>
                    <p className="text-[10px] text-orange-400 font-semibold">DIVERGÊNCIAS</p>
                    <ul className="text-xs text-gray-300 space-y-0.5">
                      {alignResult.divergences.map((d, i) => <li key={i}>· {d}</li>)}
                    </ul>
                  </div>
                )}
                {alignResult.adjustment_suggestion && (
                  <div className="bg-purple-950/30 border border-purple-800/40 rounded p-2">
                    <p className="text-[10px] text-purple-300 font-semibold mb-0.5">SUGESTÃO DE AJUSTE</p>
                    <p className="text-xs text-gray-200">{alignResult.adjustment_suggestion}</p>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Voice consistency scorer */}
          <div className="card bg-cyan-900/10 border-cyan-800/50 space-y-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-cyan-300 font-semibold">🎙 CONSISTÊNCIA DE VOZ</p>
              <button onClick={runVoiceScore} disabled={voiceBusy}
                className="text-[10px] text-cyan-400 hover:text-cyan-300 disabled:opacity-50">
                {voiceBusy ? 'Avaliando...' : selected.voice_score != null ? '↻ Re-avaliar' : '✦ Avaliar'}
              </button>
            </div>
            {selected.voice_score == null && !voiceBusy && (
              <p className="text-[11px] text-gray-500">Pontua de 0 a 100 o quanto esse post soa como a marca (tom, personalidade, posicionamento).</p>
            )}
            {selected.voice_score != null && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <div className={`text-2xl font-bold ${
                    selected.voice_score >= 70 ? 'text-green-400'
                    : selected.voice_score >= 40 ? 'text-yellow-400'
                    : 'text-red-400'
                  }`}>{selected.voice_score}</div>
                  <div className="text-[10px] text-gray-400">
                    <p>/ 100 on-brand</p>
                    {selected.voice_feedback?.verdict && <p className="text-cyan-300">{selected.voice_feedback.verdict}</p>}
                  </div>
                </div>
                {selected.voice_feedback?.weakest_part && (
                  <div>
                    <p className="text-[10px] text-orange-400 font-semibold">PONTO MAIS FRACO</p>
                    <p className="text-xs text-gray-300">{selected.voice_feedback.weakest_part}</p>
                  </div>
                )}
                {selected.voice_feedback?.fix_hint && (
                  <div className="bg-cyan-950/30 border border-cyan-800/40 rounded p-2">
                    <p className="text-[10px] text-cyan-300 font-semibold mb-0.5">COMO AJUSTAR</p>
                    <p className="text-xs text-gray-200">{selected.voice_feedback.fix_hint}</p>
                  </div>
                )}
              </div>
            )}
          </div>

          {selected.strategic_note && (
            <div className="card bg-violet-900/10 border-violet-800/50">
              <p className="text-xs text-violet-400 font-semibold mb-1">NOTA ESTRATÉGICA</p>
              <p className="text-sm text-gray-300">{selected.strategic_note}</p>
            </div>
          )}

          {(selected.objective_reasoning || selected.emotion_used || selected.funnel_stage || selected.format_reasoning) && (
            <div className="card bg-indigo-900/10 border-indigo-800/50 space-y-2">
              <p className="text-xs text-indigo-300 font-semibold">JUSTIFICATIVA ESTRATÉGICA</p>
              {selected.objective_reasoning && (
                <div>
                  <p className="text-[10px] text-indigo-400 font-semibold">POR QUE ESSE OBJETIVO</p>
                  <p className="text-xs text-gray-300">{selected.objective_reasoning}</p>
                </div>
              )}
              <div className="flex flex-wrap gap-1.5">
                {selected.emotion_used && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-orange-900/30 text-orange-200 border border-orange-800/50">
                    Emoção: {selected.emotion_used}
                  </span>
                )}
                {selected.funnel_stage && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-cyan-900/30 text-cyan-200 border border-cyan-800/50">
                    Funil: {FUNNEL_STAGE_LABELS[selected.funnel_stage] || selected.funnel_stage}
                  </span>
                )}
              </div>
              {selected.format_reasoning && (
                <div>
                  <p className="text-[10px] text-indigo-400 font-semibold">POR QUE ESSE FORMATO</p>
                  <p className="text-xs text-gray-300">{selected.format_reasoning}</p>
                </div>
              )}
            </div>
          )}

          {selected.production_brief && (
            <div className="card bg-emerald-900/10 border-emerald-800/50 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs text-emerald-300 font-semibold">📋 BRIEFING DE PRODUÇÃO</p>
                <button onClick={() => regenerateBrief(selected.id)} disabled={regenBrief} className="text-[10px] text-emerald-400 hover:text-emerald-300 disabled:opacity-50">
                  {regenBrief ? 'Regenerando...' : '↻ Regenerar'}
                </button>
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                {selected.production_brief.location && <div><span className="text-emerald-400 text-[10px] font-semibold">LOCAL:</span> <span className="text-gray-300">{selected.production_brief.location}</span></div>}
                {selected.production_brief.wardrobe && <div><span className="text-emerald-400 text-[10px] font-semibold">FIGURINO:</span> <span className="text-gray-300">{selected.production_brief.wardrobe}</span></div>}
                {selected.production_brief.lighting && <div><span className="text-emerald-400 text-[10px] font-semibold">LUZ:</span> <span className="text-gray-300">{selected.production_brief.lighting}</span></div>}
                {selected.production_brief.audio && <div><span className="text-emerald-400 text-[10px] font-semibold">ÁUDIO:</span> <span className="text-gray-300">{selected.production_brief.audio}</span></div>}
                {selected.production_brief.duration_estimate_seconds != null && <div><span className="text-emerald-400 text-[10px] font-semibold">DURAÇÃO:</span> <span className="text-gray-300">~{selected.production_brief.duration_estimate_seconds}s</span></div>}
              </div>
              {selected.production_brief.props && selected.production_brief.props.length > 0 && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold">PROPS</p>
                  <p className="text-xs text-gray-300">{selected.production_brief.props.join(' · ')}</p>
                </div>
              )}
              {selected.production_brief.shots && selected.production_brief.shots.length > 0 && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold mb-1">SHOTS</p>
                  <ol className="text-xs text-gray-300 space-y-0.5">
                    {selected.production_brief.shots.map(s => (
                      <li key={s.order} className="flex gap-2">
                        <span className="text-emerald-500 shrink-0">{s.order}.</span>
                        <span><span className="text-emerald-300">[{s.type}]</span> {s.description}</span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
              {selected.production_brief.captions_overlay && selected.production_brief.captions_overlay.length > 0 && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold">LEGENDAS NA TELA</p>
                  <p className="text-xs text-gray-300">{selected.production_brief.captions_overlay.map(c => `"${c}"`).join(' · ')}</p>
                </div>
              )}
              {selected.production_brief.equipment_minimum && selected.production_brief.equipment_minimum.length > 0 && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold">EQUIPAMENTO MÍNIMO</p>
                  <p className="text-xs text-gray-300">{selected.production_brief.equipment_minimum.join(' · ')}</p>
                </div>
              )}
              {selected.production_brief.edit_notes && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold">EDIÇÃO</p>
                  <p className="text-xs text-gray-300">{selected.production_brief.edit_notes}</p>
                </div>
              )}
              {selected.production_brief.production_tips && selected.production_brief.production_tips.length > 0 && (
                <div>
                  <p className="text-[10px] text-emerald-400 font-semibold">DICAS</p>
                  <ul className="text-xs text-gray-300 space-y-0.5">
                    {selected.production_brief.production_tips.map((t, i) => <li key={i}>· {t}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}

          {selected.status !== 'pending' && !selected.production_brief && (
            <div className="card">
              <button onClick={() => regenerateBrief(selected.id)} disabled={regenBrief} className="btn-secondary w-full py-2 text-xs">
                {regenBrief ? 'Gerando briefing...' : '📋 Gerar briefing de produção'}
              </button>
            </div>
          )}

          <div className="card">
            <p className="text-xs text-violet-400 font-semibold mb-2">MÍDIA (URL pública)</p>
            <div className="flex gap-2">
              <input
                type="url"
                className="input-field text-xs"
                placeholder="https://... (imagem ou vídeo público)"
                value={mediaUrl}
                onChange={e => setMediaUrl(e.target.value)}
              />
              <button onClick={saveMediaUrl} className="btn-secondary px-3 py-1.5 text-xs shrink-0">Salvar</button>
            </div>
            <p className="text-[11px] text-gray-500 mt-1.5">Obrigatório para publicar no Instagram. URL deve ser pública.</p>
          </div>

          {selected.external_post_id && (
            <div className="card bg-green-900/10 border-green-800/40">
              <p className="text-xs text-green-400 font-semibold mb-1">PUBLICADO</p>
              <p className="text-xs text-gray-400">ID externo: <span className="font-mono">{selected.external_post_id}</span></p>
            </div>
          )}

          {selected.publish_error && (
            <div className="card bg-red-900/10 border-red-800/40">
              <p className="text-xs text-red-400 font-semibold mb-1">ERRO AO PUBLICAR</p>
              <p className="text-xs text-gray-400">{selected.publish_error}</p>
            </div>
          )}

          <div className="space-y-2 pt-1">
            <div className="flex gap-2 flex-wrap">
              <button onClick={openVersions} className="text-xs px-2.5 py-1.5 rounded border border-gray-700 text-gray-300 hover:bg-gray-800">
                ⟲ Histórico{selected.edit_count ? ` (${selected.edit_count})` : ''}
              </button>
              <button onClick={() => setRequestChangesOpen(true)} className="text-xs px-2.5 py-1.5 rounded border border-amber-700 bg-amber-900/15 text-amber-300 hover:bg-amber-900/30">
                ✎ Pedir ajustes
              </button>
              <button onClick={runHumanize} disabled={humanizeBusy} className="text-xs px-2.5 py-1.5 rounded border border-cyan-700 bg-cyan-900/15 text-cyan-300 hover:bg-cyan-900/30 disabled:opacity-50">
                {humanizeBusy ? 'Humanizando...' : '✨ Humanizar na voz da marca'}
              </button>
            </div>

            {selected.review_notes && (
              <div className="card bg-amber-900/10 border-amber-800/50 text-xs">
                <p className="text-amber-400 font-semibold mb-0.5">REVISÃO PEDIDA</p>
                <p className="text-gray-300 whitespace-pre-line">{selected.review_notes}</p>
              </div>
            )}

            {selected.status === 'pending' && (
              <button onClick={() => approve(selected.id)} disabled={approving} className="btn-primary w-full py-3 disabled:opacity-60">
                {approving ? 'Aprovando + gerando briefing...' : '✓ Aprovar conteúdo (gera briefing de gravação)'}
              </button>
            )}
            {selected.status === 'approved' && (
              <button onClick={() => setStatus(selected.id, 'recorded')}
                className="btn-primary w-full py-3 bg-blue-600 hover:bg-blue-700">
                Marcar como gravado
              </button>
            )}
            {(selected.status === 'approved' || selected.status === 'recorded') &&
             (selected.platform === 'instagram' || selected.platform === 'facebook') && (
              <button onClick={publishNow} disabled={publishing}
                className="btn-primary w-full py-3 bg-violet-600 hover:bg-violet-700">
                {publishing ? 'Publicando...' : `📤 Publicar agora no ${selected.platform}`}
              </button>
            )}
            {selected.status === 'recorded' && (
              <button onClick={() => setStatus(selected.id, 'published')}
                className="btn-secondary w-full py-2 text-xs">
                Marcar como publicado (manualmente)
              </button>
            )}
          </div>
        </div>

        {/* Versions modal */}
        {versionsOpen && (
          <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" onClick={() => setVersionsOpen(false)}>
            <div className="card max-w-2xl w-full max-h-[80vh] overflow-y-auto space-y-3" onClick={e => e.stopPropagation()}>
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-white">Histórico de versões</p>
                <button onClick={() => setVersionsOpen(false)} className="text-gray-400">×</button>
              </div>
              {versionsLoading && <p className="text-xs text-gray-400">Carregando...</p>}
              {!versionsLoading && versions && versions.length === 0 && (
                <p className="text-xs text-gray-500">Sem edições anteriores — ainda é a versão original</p>
              )}
              {versions && versions.map(v => (
                <div key={v.id} className="card">
                  <div className="flex items-center justify-between mb-1">
                    <p className="text-xs font-semibold text-violet-300">v{v.version_number} {v.edited_by_user ? '· editado' : '· auto'}</p>
                    <button onClick={() => restoreVersion(v.id)} className="text-[10px] text-violet-400 hover:text-violet-300">Restaurar</button>
                  </div>
                  {v.change_summary && <p className="text-[11px] text-gray-400 mb-1">{v.change_summary}</p>}
                  {v.title && <p className="text-xs text-white">{v.title}</p>}
                  {v.hook && <p className="text-[11px] text-gray-300 mt-0.5 line-clamp-2">{v.hook}</p>}
                  {v.created_at && <p className="text-[10px] text-gray-500 mt-1">{new Date(v.created_at).toLocaleString('pt-BR')}</p>}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Request changes modal */}
        {requestChangesOpen && (
          <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4" onClick={() => setRequestChangesOpen(false)}>
            <div className="card max-w-lg w-full space-y-3" onClick={e => e.stopPropagation()}>
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-amber-300">Pedir ajustes</p>
                <button onClick={() => setRequestChangesOpen(false)} className="text-gray-400">×</button>
              </div>
              <textarea
                rows={5}
                className="input text-sm"
                placeholder="Ex: tom muito formal, ajustar pra mais casual. Hook precisa de tensão maior nos 3 primeiros segundos."
                value={changesNote}
                onChange={e => setChangesNote(e.target.value)}
              />
              <p className="text-[10px] text-gray-500">A nota fica registrada no conteúdo. Use o "Regenerar" em cada seção pra aplicar.</p>
              <div className="flex gap-2 justify-end">
                <button onClick={() => setRequestChangesOpen(false)} className="text-xs px-3 py-1.5 text-gray-400 border border-gray-700 rounded-md">Cancelar</button>
                <button onClick={submitRequestChanges} disabled={changesBusy || !changesNote.trim()} className="btn-primary text-xs">
                  {changesBusy ? '...' : 'Salvar nota'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 max-w-5xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-lg font-bold text-white">Conteúdo</h1>
        {contents.length > 0 && (
          <div className="flex items-center gap-2">
            {selectedIds.size > 0 ? (
              <>
                <span className="text-xs text-gray-400">{selectedIds.size} selecionado(s)</span>
                <button onClick={bulkApprove} disabled={bulkBusy} className="text-xs px-2.5 py-1 rounded bg-green-700 hover:bg-green-600 text-white disabled:opacity-50">
                  ✓ Aprovar
                </button>
                <button onClick={bulkDelete} disabled={bulkBusy} className="text-xs px-2.5 py-1 rounded bg-red-700 hover:bg-red-600 text-white disabled:opacity-50">
                  Excluir
                </button>
                <button onClick={clearSelection} className="text-xs text-gray-400 hover:text-gray-200">×</button>
              </>
            ) : (
              <button onClick={selectAllVisible} className="text-xs text-violet-400 hover:text-violet-300">
                Selecionar todos
              </button>
            )}
          </div>
        )}
      </div>

      {/* Search */}
      <div className="mb-2 relative">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="🔍 Buscar por título, hook, plataforma, produto..."
          className="input-field pl-3 pr-9 text-sm"
        />
        {search && (
          <button onClick={() => setSearch('')} aria-label="Limpar busca"
            className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300 text-xs">×</button>
        )}
      </div>

      {/* Filter pills - horizontal scroll on mobile */}
      <div className="flex gap-1.5 overflow-x-auto pb-2 -mx-4 px-4 md:mx-0 md:px-0 mb-4 scrollbar-none">
        <button onClick={() => setFilter('')}
          className={`px-3 py-1.5 rounded-lg text-xs font-medium border shrink-0 transition-colors ${
            !filter ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
          }`}>
          Todos
        </button>
        {STATUSES.map(s => (
          <button key={s} onClick={() => setFilter(s)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium border shrink-0 transition-colors ${
              filter === s ? 'bg-violet-600/20 border-violet-500 text-violet-300' : 'bg-gray-800 border-gray-700 text-gray-400'
            }`}>
            {STATUS_LABELS[s]}
          </button>
        ))}
      </div>

      {listLoading ? (
        <div className="space-y-2">
          {[0, 1, 2, 3].map(i => (
            <div key={i} className="card animate-pulse">
              <div className="h-3 w-1/3 bg-gray-800 rounded mb-2"></div>
              <div className="h-4 w-2/3 bg-gray-800 rounded mb-2"></div>
              <div className="h-3 w-1/2 bg-gray-800 rounded"></div>
            </div>
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="card text-center py-12">
          <p className="text-gray-500 text-sm">
            {debouncedSearch ? `Nada encontrado para "${debouncedSearch}"` : 'Nenhum conteúdo encontrado'}
          </p>
          <p className="text-gray-600 text-xs mt-1">
            {debouncedSearch ? 'Tente outro termo ou limpe a busca' : 'Use os Agentes IA para gerar conteúdo'}
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {visible.map(content => (
            <div
              key={content.id}
              className={`card flex items-start gap-2 active:border-violet-600 hover:border-violet-700 transition-colors ${selectedIds.has(content.id) ? 'border-violet-500 bg-violet-900/10' : ''}`}
            >
              <input
                type="checkbox"
                checked={selectedIds.has(content.id)}
                onChange={(e) => { e.stopPropagation(); toggleSelect(content.id) }}
                onClick={(e) => e.stopPropagation()}
                className="mt-1 shrink-0 accent-violet-500"
              />
              <button
                onClick={() => setSelected(content)}
                className="flex-1 text-left min-w-0"
              >
              <div className="flex items-start gap-2">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                    <span className={`badge text-[10px] ${STATUS_COLORS[content.status]}`}>
                      {STATUS_LABELS[content.status]}
                    </span>
                    <span className={`badge border text-[10px] ${OBJECTIVE_COLORS[content.objective] || 'bg-gray-700 text-gray-300 border-gray-600'}`}>
                      {OBJECTIVE_LABELS[content.objective] || content.objective}
                    </span>
                    {content.linked_product_name && (
                      <span className="badge bg-cyan-900/30 text-cyan-300 border border-cyan-800/60 text-[10px]">
                        → {content.linked_product_name}
                      </span>
                    )}
                  </div>
                  <p className="text-sm font-medium text-white truncate">{content.title}</p>
                  {content.hook && (
                    <p className="text-xs text-gray-400 truncate mt-0.5">{content.hook}</p>
                  )}
                </div>
                <div className="text-right shrink-0 ml-2">
                  <p className="text-xs text-gray-500">{content.platform}</p>
                  <p className="text-xs text-gray-600">{FORMAT_LABELS[content.format] || content.format}</p>
                </div>
              </div>
              </button>
            </div>
          ))}
          {filtered.length > visible.length && (
            <div className="text-center pt-2">
              <button onClick={() => setPageSize(p => p + 20)}
                className="text-xs text-violet-400 hover:text-violet-300 px-4 py-2">
                Mostrar mais ({filtered.length - visible.length} restantes)
              </button>
            </div>
          )}
          <p className="text-[10px] text-gray-600 text-center pt-1">
            Mostrando {visible.length} de {filtered.length}
            {debouncedSearch ? ` · busca: "${debouncedSearch}"` : ''}
          </p>
        </div>
      )}
    </div>
  )
}
