import { useState, useEffect, useCallback, useRef } from 'react'
import { X, Sparkles, TrendingUp, TrendingDown, RefreshCw, AlertTriangle, Brain } from 'lucide-react'
import { api } from '../services/api'
import { fmtRR } from '../utils/rr'
import type { Recommendation, RecommendationTier } from '../types'

interface HistoricalStat {
  trades: number
  win_rate: number | null
  avg_r: number | null
  sample_ok: boolean
  verdict: 'winning' | 'losing' | 'neutro' | 'amostra_pequena' | 'sem_historico'
}

const BACKEND = import.meta.env.VITE_API_URL ?? 'https://crypto-agente-production.up.railway.app'

interface Props {
  onClose: () => void
  onSelectSymbol: (symbol: string, timeframe: string) => void
  focus?: { symbol: string; timeframe: string; event?: string } | null
  /** Chamado quando foco vem de push mas rec já saiu do top — App roteia pra Abertos. */
  onFocusNotFound?: (focus: { symbol: string; timeframe: string }) => void
}

const TIER_CONFIG: Record<RecommendationTier, { label: string; bg: string; border: string; text: string; ring: string }> = {
  'A+': {
    label: 'A+',
    bg: 'bg-gradient-to-r from-emerald-500/20 to-green-500/20',
    border: 'border-emerald-400/60',
    text: 'text-emerald-300',
    ring: 'ring-2 ring-emerald-400/40',
  },
  'A': {
    label: 'A',
    bg: 'bg-green-500/10',
    border: 'border-green-500/40',
    text: 'text-green-400',
    ring: '',
  },
  'B': {
    label: 'B',
    bg: 'bg-blue-500/10',
    border: 'border-blue-500/40',
    text: 'text-blue-400',
    ring: '',
  },
}

const TIER_DESC: Record<RecommendationTier, string> = {
  'A+': 'Setup premium — confluência alta + MTF alinhado + R:R ≥ 2.5',
  'A':  'Setup forte — confluência boa + MTF a favor + R:R ≥ 2.0',
  'B':  'Setup aceitável — confluência média, validar entrada',
}

function fmt(n: number) {
  if (n >= 1000) return n.toFixed(2)
  if (n >= 1) return n.toFixed(4)
  return n.toFixed(6)
}

function symbolBase(s: string): string {
  return s.split('/')[0]
}

// Traduz o código do gate de execução (backend) num rótulo curto PT-BR pro selo
// "bot não opera". O motivo completo (com números) vai no tooltip.
function gateLabel(gate: string): string {
  const map: Record<string, string> = {
    'liquidity-gate': 'liquidez baixa',
    'prob-gate': 'P(TP1) baixa',
    'rr-gate': 'R:R fraco',
    'proximity': 'preço esticado',
    'atr-gate': 'volatilidade alta',
    'score-min': 'score baixo',
    'exec-universe': 'fora da allowlist',
    'blacklist': 'símbolo banido',
    'time-block': 'horário bloqueado',
    'funding-gate': 'funding contra',
    'mtf-gate': 'timeframes desalinhados',
    'quality-edge-gate': 'score marginal sem edge',
    'regime-guard': 'regime adverso',
    'cluster-cap': 'limite de exposição',
    'direction-cap': 'limite direcional',
    'entry-throttle': 'cadência (throttle)',
    'cooldown': 'cooldown',
  }
  return map[gate] ?? gate
}

// Classifica a operação pelo timeframe — define expectativa de holding period
// e ajuda o user a saber se entra/sai no mesmo dia ou carrega posição.
function operationType(tf: string): { label: string; cls: string; hint: string } {
  const t = tf.toLowerCase()
  if (['1m', '3m', '5m', '15m'].includes(t)) {
    return {
      label: 'SCALP',
      cls: 'bg-pink-500/15 text-pink-300 border-pink-500/40',
      hint: 'Operação rápida (minutos a 1h). Entra e sai no mesmo dia, geralmente em janela curta.',
    }
  }
  if (['30m', '1h', '2h'].includes(t)) {
    return {
      label: 'DAY TRADE',
      cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40',
      hint: 'Operação intradiária (horas). Abre e fecha no mesmo dia, sem carregar overnight.',
    }
  }
  return {
    label: 'SWING',
    cls: 'bg-purple-500/15 text-purple-300 border-purple-500/40',
    hint: 'Posição multi-dia (4h+). Carrega overnight — atenção a funding e gaps.',
  }
}

// Edges = sinais que historicamente elevam o win-rate (learning-insights: A+ ~92%,
// funding em squeeze ~100%, padrão forte ~90%, MTF alinhado ~82% vs baseline ~72%).
// Tooltip explica cada um; o bot usa essas tags pra escalar o size (EDGE_SIZING).
const EDGE_TAG_META: Record<string, { label: string; hint: string }> = {
  'A+': { label: 'A+', hint: 'Tier A+ — setup premium. Historicamente ~92% win-rate.' },
  'funding': { label: 'funding', hint: 'Funding em squeeze a favor da direção (~100% wr histórico em squeeze).' },
  'padrão': { label: 'padrão', hint: 'Padrão gráfico forte alinhado (conf ≥ 0.65). ~90% wr em padrões fortes.' },
  'MTF': { label: 'MTF', hint: 'Múltiplos timeframes alinhados (≥ 2). ~82% wr quando MTF concorda.' },
}

// Nomes legíveis (PT-BR) dos padrões gráficos — pro chip de "padrão" dizer QUAL
// padrão foi detectado (OB, Triângulo, Cunha…) em vez do genérico "padrão".
const PATTERN_LABEL_PT: Record<string, string> = {
  lta: 'LTA', ltb: 'LTB',
  ascending_channel: 'Canal de Alta', descending_channel: 'Canal de Baixa',
  horizontal_channel: 'Canal Lateral',
  symmetric_triangle: 'Triângulo Simétrico',
  ascending_triangle: 'Triângulo Ascendente',
  descending_triangle: 'Triângulo Descendente',
  ascending_wedge: 'Cunha Ascendente', descending_wedge: 'Cunha Descendente',
  head_and_shoulders: 'OCO', inverse_head_and_shoulders: 'OCO Invertido',
  double_top: 'Topo Duplo', double_bottom: 'Fundo Duplo',
  bull_flag: 'Bandeira de Alta', bear_flag: 'Bandeira de Baixa',
  cup_and_handle: 'Xícara c/ Alça',
}

// Deriva os chips de PADRÃO a partir do signal embutido na rec: nomeia a zona
// SMC que originou a entrada (OB/FVG/Value Area) + os padrões gráficos alinhados
// à direção, anexando o alvo de rompimento quando houver. Substitui o chip
// genérico "padrão".
function patternChips(r: Recommendation): { label: string; hint: string }[] {
  const out: { label: string; hint: string }[] = []
  const seen = new Set<string>()
  const zoneMeta: Record<string, { label: string; hint: string }> = {
    limit_ob: { label: 'OB', hint: 'Order Block — zona de oferta/demanda institucional. Entrada no reteste da zona.' },
    limit_fvg_fill: { label: 'FVG', hint: 'Fair Value Gap (imbalance) — entrada no preenchimento do gap.' },
    limit_value_area: { label: 'Value Area', hint: 'Borda da área de valor (VAL/VAH) — reação esperada na zona.' },
    limit_pattern_fade: { label: 'Fade de padrão', hint: 'Entrada contra o exagero do padrão (fade), aguardando reversão à média.' },
  }
  if (r.entry_zone_type && zoneMeta[r.entry_zone_type]) {
    out.push(zoneMeta[r.entry_zone_type])
    seen.add('zone')
  }
  const pats = [...(r.signal?.patterns ?? [])]
    .filter(p => p.direction === r.direction || p.direction === 'neutral')
    .sort((a, b) => b.confidence - a.confidence)
  for (const p of pats) {
    if (seen.has(p.type)) continue
    seen.add(p.type)
    const name = PATTERN_LABEL_PT[p.type] ?? p.type
    let label = name
    let hint = p.description || name
    if (p.breakout_target != null) {
      label += ` · rompe ${fmt(p.breakout_target)}`
      hint += ` · Possível rompimento — alvo ~${fmt(p.breakout_target)}.`
    }
    out.push({ label, hint })
    if (out.length >= 3) break
  }
  return out
}

export default function RecommendationsPanel({ onClose, onSelectSymbol, focus, onFocusNotFound }: Props) {
  const cardRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const [highlightKey, setHighlightKey] = useState<string | null>(null)
  const [recs, setRecs] = useState<Recommendation[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)
  // Chaves (símbolo_tf_direção) dos setups que JÁ estão em "Abertos" — usadas
  // pra esconder de Recomendados o que já virou trade aberto (item A).
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set())
  const [filter, setFilter] = useState<RecommendationTier | 'all'>('all')
  const [originFilter, setOriginFilter] = useState<'all' | 'bot' | 'observation'>('all')
  // "Só aprovados": mostra apenas recs que passam nos gates de qualidade do bot
  // (bot_verdict.ok === true — R:R/P(TP1)/liquidez), independente da origem.
  const [qualityOnly, setQualityOnly] = useState(false)
  const [progress, setProgress] = useState<string>('')
  const [historical, setHistorical] = useState<Record<string, HistoricalStat>>({})
  const [probs, setProbs] = useState<Record<string, { p_tp1_pct: number; p_tp2_pct: number; n_total: number; confidence: string }>>({})
  // Confirmar entrada manual a partir de uma rec
  const [confirmFor, setConfirmFor] = useState<string | null>(null)
  const [confirmForm, setConfirmForm] = useState({ entry: '', qty: '' })
  const [confirmBusy, setConfirmBusy] = useState(false)
  const [confirmToast, setConfirmToast] = useState<{ k: string; msg: string; level: 'ok' | 'err' } | null>(null)
  const [newsStatus, setNewsStatus] = useState<{
    active: boolean
    event?: string
    country?: string
    minutes_until_event?: number
    minutes_until_resume?: number
    next_event?: string
    next_country?: string
    minutes_until_next?: number
  } | null>(null)
  const [regime, setRegime] = useState<{
    regime: string
    btc_24h_pct: number | null
    btc_dominance: number | null
    block_all: boolean
    block_alt_longs: boolean
    downgrade_alt_longs: boolean
    reasons: string[]
  } | null>(null)

  // Veredito de execução do bot por símbolo+direção: por que uma rec NÃO virou
  // trade real (gate que barrou). Alinha "recomendado" com "o bot vai operar".
  const [skipReasons, setSkipReasons] = useState<Record<string, { gate: string; reason: string; ts: string }>>({})

  useEffect(() => {
    const fetchStatus = async () => {
      try {
        const [n, r, sk] = await Promise.all([
          fetch(`${BACKEND}/api/news-status`).then(x => x.ok ? x.json() : null),
          fetch(`${BACKEND}/api/regime-status`).then(x => x.ok ? x.json() : null),
          fetch(`${BACKEND}/api/shadow/skip-reasons`).then(x => x.ok ? x.json() : null),
        ])
        if (n) setNewsStatus(n.status)
        if (r) setRegime(r)
        if (sk?.items) {
          // Só motivos recentes (<= 20min) — evita veredito velho após o cenário
          // mudar. Chaveado por símbolo+direção.
          const now = Date.now()
          const map: Record<string, { gate: string; reason: string; ts: string }> = {}
          for (const it of sk.items as Array<{ symbol: string; gate: string; reason: string; ts: string; direction?: string }>) {
            const age = now - new Date(it.ts).getTime()
            if (age > 20 * 60_000) continue
            const key = `${it.symbol}__${it.direction ?? ''}`
            map[key] = { gate: it.gate, reason: it.reason, ts: it.ts }
          }
          setSkipReasons(map)
        }
      } catch { /* fail-open */ }
    }
    fetchStatus()
    const id = setInterval(fetchStatus, 60_000)
    return () => clearInterval(id)
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setProgress('Carregando recomendações…')
    try {
      // Duas fontes, de propósito desacopladas:
      //  • PRD (api.recommendations) → as recs que o BOT realmente opera (top-60
      //    que ele executa na sexta). Marcadas 🤖 BOT OPERA.
      //  • TESTES (api.recommendationsObservation) → universo amplo de perpétuos,
      //    shadow, DB separado. O bot NÃO opera; são pro usuário analisar no
      //    TradingView. Marcadas 👁 OBSERVAÇÃO. Falha graciosa (se testes cair,
      //    o painel ainda mostra as do bot).
      const [botRes, obsRecs] = await Promise.all([
        api.recommendations(60),
        api.recommendationsObservation(300),
      ])

      const botRecs: Recommendation[] = botRes.recommendations.map(r => ({ ...r, origin: 'bot' as const }))
      // Chave de dedupe: símbolo base + timeframe + direção. Se a mesma rec
      // aparece nas duas fontes, a do BOT vence (é a que ele opera de fato).
      const botKeys = new Set(botRecs.map(r => `${symbolBase(r.symbol)}_${r.timeframe}_${r.direction}`))
      const obsOnly: Recommendation[] = obsRecs
        .filter(r => !botKeys.has(`${symbolBase(r.symbol)}_${r.timeframe}_${r.direction}`))
        .map(r => ({ ...r, origin: 'observation' as const }))

      // Bot primeiro, depois observação. Dentro de cada grupo a ordenação
      // original (tier+score) é preservada.
      const merged = [...botRecs, ...obsOnly]
      setRecs(merged)
      setLastUpdate(new Date())
      setProgress('')

      // Busca histórico (não bloqueia UI se falhar)
      try {
        const lookupItems = merged.map(r => ({
          tier: r.tier, timeframe: r.timeframe, direction: r.direction,
        }))
        if (lookupItems.length > 0) {
          const histRes = await fetch(`${BACKEND}/api/historical-lookup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items: lookupItems, days: 60 }),
          })
          if (histRes.ok) setHistorical(await histRes.json())
        }
      } catch { /* ignora — badge histórico é opcional */ }

      // Probabilidades empíricas por bucket (não bloqueia UI)
      try {
        const pRes = await fetch(`${BACKEND}/api/probabilities?days=90`)
        if (pRes.ok) {
          const pJson = await pRes.json()
          if (pJson.enabled) setProbs(pJson.buckets ?? {})
        }
      } catch { /* opcional */ }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro ao carregar')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 120_000)   // refresh a cada 2 min
    return () => clearInterval(id)
  }, [load])

  // ── Item A: esconder de Recomendados o que JÁ virou trade aberto ──────────
  // Busca os "Abertos" (PRD open_trades + universo amplo wide_open_trades) e
  // monta o set de chaves. Os setups que já estão abertos somem de Recomendados
  // no próximo ciclo — mostram-se só os que ainda NÃO foram pegos; os pegos
  // seguem visíveis no painel "Abertos". Falha-soft: se cair, mostra todas.
  useEffect(() => {
    let alive = true
    const fetchOpen = async () => {
      try {
        const today = new Date().toISOString().slice(0, 10)
        const res = await fetch(`${BACKEND}/api/daily-pnl?date=${today}`, {
          signal: AbortSignal.timeout(12000),
        })
        if (!res.ok) return
        const json = (await res.json()) as {
          open_trades?: { symbol?: string; timeframe?: string; direction?: string }[]
          wide_open_trades?: { symbol?: string; timeframe?: string; direction?: string }[]
        }
        if (!alive) return
        const keys = new Set<string>()
        for (const t of [...(json.open_trades ?? []), ...(json.wide_open_trades ?? [])]) {
          if (t?.symbol && t?.timeframe && t?.direction) {
            keys.add(`${symbolBase(t.symbol)}_${t.timeframe}_${t.direction}`)
          }
        }
        setOpenKeys(keys)
      } catch { /* fail-silent */ }
    }
    fetchOpen()
    const id = setInterval(fetchOpen, 120_000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  // Recs visíveis = todas menos as que já estão abertas (item A).
  const visibleRecs = recs.filter(
    r => !openKeys.has(`${symbolBase(r.symbol)}_${r.timeframe}_${r.direction}`)
  )
  const counts = {
    'A+': visibleRecs.filter(r => r.tier === 'A+').length,
    'A':  visibleRecs.filter(r => r.tier === 'A').length,
    'B':  visibleRecs.filter(r => r.tier === 'B').length,
  }
  const originCounts = {
    bot: visibleRecs.filter(r => r.origin === 'bot').length,
    observation: visibleRecs.filter(r => r.origin === 'observation').length,
  }
  const byTier = filter === 'all' ? visibleRecs : visibleRecs.filter(r => r.tier === filter)
  const byOrigin =
    originFilter === 'all' ? byTier : byTier.filter(r => (r.origin ?? 'bot') === originFilter)
  // Filtro de qualidade: só os que o bot aprovaria (passam nos gates R:R/P(TP1)/liquidez)
  const qualityApprovedCount = visibleRecs.filter(r => r.bot_verdict?.ok === true).length
  const filtered = qualityOnly ? byOrigin.filter(r => r.bot_verdict?.ok === true) : byOrigin

  // Quando chega foco via push, scrolla até o card e destaca por ~3s.
  // Roda quando focus muda OU quando as recs carregam (ordem indefinida entre os dois).
  useEffect(() => {
    if (!focus) return
    // Espera o loading terminar antes de decidir
    if (loading) return
    const match = recs.find(
      r => symbolBase(r.symbol) === focus.symbol && r.timeframe === focus.timeframe
    )
    if (!match) {
      // Rec saiu do top (preço andou ou rotou) — fallback pra Abertos
      if (onFocusNotFound) {
        onFocusNotFound({ symbol: focus.symbol, timeframe: focus.timeframe })
      }
      return
    }
    const key = `${match.symbol}-${match.timeframe}`
    // Se o filtro de tier esconde o card, abre "all" pra revelar
    if (filter !== 'all' && match.tier !== filter) {
      setFilter('all')
    }
    // Espera o DOM aplicar o filtro/render antes de scrollar
    const t = setTimeout(() => {
      const el = cardRefs.current[key]
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        setHighlightKey(key)
        setTimeout(() => setHighlightKey(null), 3000)
      }
    }, 100)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, recs, loading])

  const openConfirm = (r: Recommendation, key: string) => {
    setConfirmForm({ entry: String(r.current_price ?? r.entry ?? ''), qty: '' })
    setConfirmToast(null)
    setConfirmFor(prev => (prev === key ? null : key))
  }

  const submitConfirm = async (r: Recommendation, key: string) => {
    const entry = parseFloat(confirmForm.entry)
    if (!entry) {
      setConfirmToast({ k: key, msg: 'Informe o preço de entrada', level: 'err' })
      return
    }
    setConfirmBusy(true)
    try {
      const res = await api.confirmEntry({
        symbol: r.symbol,
        side: r.direction,
        entry_price: entry,
        qty: confirmForm.qty ? parseFloat(confirmForm.qty) : null,
        timeframe: r.timeframe,
        leverage: r.leverage,
        planned_stop: r.stop_loss,
        planned_tp1: r.signal?.tp1 ?? null,
        planned_tp2: r.tp2,
      })
      const prot = res.protection
      const protMsg = prot?.placed
        ? 'Bot colocou SL+TP1+TP2.'
        : prot?.error
          ? `⚠ bracket não criado (${prot.error}).`
          : '⚠ bracket pendente — auto-cura vai tentar.'
      setConfirmToast({
        k: key,
        msg: `✅ Entrada confirmada · qty ${res.qty_source}. ${protMsg} Acompanhe em "Operações Ativas".`,
        level: prot?.placed === false ? 'err' : 'ok',
      })
      setConfirmFor(null)
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'erro'
      setConfirmToast({
        k: key,
        msg: msg.includes('422')
          ? 'Sem posição aberta na conta — abra na corretora ou informe o qty.'
          : `Falha: ${msg}`,
        level: 'err',
      })
    } finally {
      setConfirmBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4">
      <div className="w-full max-w-5xl max-h-[92vh] bg-[#0a0e1a] border border-slate-700 rounded-xl flex flex-col overflow-hidden shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800 bg-gradient-to-r from-slate-900 to-slate-800">
          <div className="flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-yellow-400" />
            <h2 className="text-base font-bold text-white">Trades Recomendados</h2>
            <span className="text-xs text-slate-500 hidden sm:inline">
              · varredura dos top 30 perpétuos por volume
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              disabled={loading}
              className="flex items-center gap-1 px-2 py-1 bg-slate-800 hover:bg-slate-700 border border-slate-700 rounded text-xs text-slate-300 disabled:opacity-50"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
              <span className="hidden sm:inline">Atualizar</span>
            </button>
            <button onClick={onClose} className="p-1 hover:bg-slate-800 rounded">
              <X className="w-5 h-5 text-slate-400" />
            </button>
          </div>
        </div>

        {/* News blackout banner */}
        {newsStatus?.active && (
          <div className="px-4 py-2 bg-amber-900/30 border-b border-amber-700/50 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
            <div className="text-xs leading-snug">
              <div className="font-semibold text-amber-300">
                Blackout de notícia macro ativo — recomendações pausadas
              </div>
              <div className="text-amber-200/80 mt-0.5">
                {newsStatus.event} ({newsStatus.country})
                {typeof newsStatus.minutes_until_resume === 'number' && (
                  <> · retoma em {newsStatus.minutes_until_resume}min</>
                )}
              </div>
            </div>
          </div>
        )}
        {!newsStatus?.active && newsStatus?.next_event && typeof newsStatus.minutes_until_next === 'number' && newsStatus.minutes_until_next <= 120 && (
          <div className="px-4 py-1.5 bg-slate-900/60 border-b border-slate-800 text-[11px] text-slate-400">
            <span className="text-amber-400">⚠</span> Próx. evento macro: <span className="text-slate-200">{newsStatus.next_event}</span> ({newsStatus.next_country}) em {newsStatus.minutes_until_next}min
          </div>
        )}

        {/* Regime macro banner */}
        {regime && regime.regime !== 'NORMAL' && (
          <div className={`px-4 py-2 border-b flex items-start gap-2 ${
            regime.block_all ? 'bg-red-900/40 border-red-700/60' :
            regime.block_alt_longs ? 'bg-orange-900/30 border-orange-700/50' :
            'bg-yellow-900/20 border-yellow-700/40'
          }`}>
            <AlertTriangle className={`w-4 h-4 flex-shrink-0 mt-0.5 ${
              regime.block_all ? 'text-red-400' : regime.block_alt_longs ? 'text-orange-400' : 'text-yellow-400'
            }`} />
            <div className="text-xs leading-snug">
              <div className={`font-semibold ${
                regime.block_all ? 'text-red-300' : regime.block_alt_longs ? 'text-orange-300' : 'text-yellow-300'
              }`}>
                Regime: {regime.regime}
                {regime.block_all && ' — todas as recs bloqueadas'}
                {!regime.block_all && regime.block_alt_longs && ' — longs em alts bloqueados'}
                {!regime.block_all && !regime.block_alt_longs && regime.downgrade_alt_longs && ' — alt longs com tier reduzido'}
              </div>
              <div className="text-slate-300/80 mt-0.5">
                {regime.reasons?.join(' · ')}
                {(regime.btc_dominance !== null || regime.btc_24h_pct !== null) && (
                  <span className="ml-2 text-slate-500">
                    [BTC.D {regime.btc_dominance?.toFixed(1) ?? '—'}% · BTC 24h {regime.btc_24h_pct !== null ? (regime.btc_24h_pct >= 0 ? '+' : '') + regime.btc_24h_pct.toFixed(2) + '%' : '—'}]
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Filtros / contagem */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-800 overflow-x-auto">
          {(['all', 'A+', 'A', 'B'] as const).map(tier => {
            const cfg = tier === 'all' ? null : TIER_CONFIG[tier]
            const active = filter === tier
            const count = tier === 'all' ? recs.length : counts[tier]
            return (
              <button
                key={tier}
                onClick={() => setFilter(tier)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs font-semibold whitespace-nowrap transition-colors ${
                  active
                    ? cfg ? `${cfg.bg} ${cfg.border} ${cfg.text}` : 'bg-white/10 border-white/30 text-white'
                    : 'bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200'
                }`}
              >
                <span>{tier === 'all' ? 'Todos' : `Tier ${tier}`}</span>
                <span className="text-slate-500">({count})</span>
              </button>
            )
          })}
          {lastUpdate && (
            <span className="ml-auto text-[10px] text-slate-600 whitespace-nowrap">
              últ. {lastUpdate.toLocaleTimeString('pt-BR')}
            </span>
          )}
        </div>

        {/* Origem: o que o BOT opera vs só OBSERVAÇÃO (pro usuário analisar) */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-slate-800 overflow-x-auto">
          {([
            { key: 'all', label: 'Tudo', count: recs.length },
            { key: 'bot', label: '🤖 Bot opera', count: originCounts.bot },
            { key: 'observation', label: '👁 Observação', count: originCounts.observation },
          ] as const).map(opt => {
            const active = originFilter === opt.key
            const activeCls =
              opt.key === 'bot' ? 'bg-emerald-500/15 border-emerald-400/50 text-emerald-300'
              : opt.key === 'observation' ? 'bg-sky-500/15 border-sky-400/50 text-sky-300'
              : 'bg-white/10 border-white/30 text-white'
            return (
              <button
                key={opt.key}
                onClick={() => setOriginFilter(opt.key)}
                className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs font-semibold whitespace-nowrap transition-colors ${
                  active ? activeCls : 'bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200'
                }`}
              >
                <span>{opt.label}</span>
                <span className="text-slate-500">({opt.count})</span>
              </button>
            )
          })}
          {/* Toggle: só os que o bot APROVARIA (passam nos gates de qualidade) */}
          <button
            onClick={() => setQualityOnly(v => !v)}
            title="Mostra só as recomendações que passam nos MESMOS gates de qualidade do bot (R:R, P(TP1), liquidez). As que o bot operaria de verdade."
            className={`flex items-center gap-1.5 px-3 py-1 rounded-full border text-xs font-semibold whitespace-nowrap transition-colors ${
              qualityOnly
                ? 'bg-emerald-500/20 border-emerald-400/60 text-emerald-200'
                : 'bg-slate-800/60 border-slate-700 text-slate-400 hover:text-slate-200'
            }`}
          >
            <span>{qualityOnly ? '✅' : '☑️'} Só aprovados pelo bot</span>
            <span className="text-slate-500">({qualityApprovedCount})</span>
          </button>
          <span className="ml-auto text-[10px] text-slate-500 whitespace-nowrap hidden sm:inline">
            🤖 = o bot opera · 👁 = só pra você analisar (TradingView)
          </span>
        </div>

        {/* Lista */}
        <div className="flex-1 overflow-y-auto">
          {loading && recs.length === 0 && (
            <div className="flex flex-col items-center justify-center py-20 gap-3">
              <div className="w-8 h-8 border-2 border-yellow-500 border-t-transparent rounded-full animate-spin" />
              <span className="text-sm text-slate-500">{progress || 'Varredura em andamento…'}</span>
            </div>
          )}

          {error && (
            <div className="m-4 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300">
              ⚠ {error}
            </div>
          )}

          {!loading && !error && filtered.length === 0 && recs.length > 0 && visibleRecs.length === 0 && (
            <div className="flex flex-col items-center justify-center py-20 gap-3 px-4 text-center">
              <span className="text-4xl">📂</span>
              <p className="text-sm text-slate-300 font-semibold">
                Setups atuais já estão em “Abertos”
              </p>
              <p className="text-xs text-slate-500 max-w-md">
                Todas as recomendações da varredura já viraram trades abertos — acompanhe-as no
                painel de Abertos. Aqui aparecem só setups novos, ainda não pegos.
              </p>
            </div>
          )}

          {!loading && !error && filtered.length === 0 && !(recs.length > 0 && visibleRecs.length === 0) && (
            <div className="flex flex-col items-center justify-center py-20 gap-3 px-4 text-center">
              <span className="text-4xl">🧘</span>
              <p className="text-sm text-slate-300 font-semibold">
                Nenhum setup {filter === 'all' ? '' : `Tier ${filter}`} no momento
              </p>
              <p className="text-xs text-slate-500 max-w-md">
                Esperar é a operação correta. Forçar trade em mercado lateral é perda garantida —
                o sistema só recomenda quando há real confluência.
              </p>
            </div>
          )}

          <div className="flex flex-col gap-2 p-3">
            {filtered.map((r) => {
              const cfg = TIER_CONFIG[r.tier]
              const isLong = r.direction === 'long'
              const DirIcon = isLong ? TrendingUp : TrendingDown
              const dirColor = isLong ? 'text-green-400' : 'text-red-400'
              const histKey = `${r.tier}_${r.timeframe}_${r.direction}`
              const hist = historical[histKey]
              const rkey = `${r.symbol}-${r.timeframe}`
              const isObs = r.origin === 'observation'
              // Veredito de qualidade do backend (R:R/P(TP1)/liquidez) — mesma
              // lógica/limites do loop, anexado a TODA rec (inclusive observação)
              // e sempre fresco (não reseta no redeploy).
              const verdict = r.bot_verdict ?? undefined
              // Veredito de execução pra recs do BOT: combina skip-reasons (cobre
              // TODOS os gates do loop: proximity, atr, allowlist, time-block…) com
              // o veredito de qualidade. skip-reasons tem prioridade quando existe.
              const skip = isObs ? undefined : skipReasons[`${r.symbol}__${r.direction}`]
              const blocked = isObs
                ? undefined
                : skip
                  ? { gate: skip.gate, reason: skip.reason }
                  : verdict && verdict.ok === false && verdict.blocked_by
                    ? { gate: verdict.blocked_by, reason: verdict.reason ?? '' }
                    : undefined
              return (
                <div key={rkey} className="flex flex-col">
                <button
                  ref={(el) => { cardRefs.current[rkey] = el }}
                  onClick={() => onSelectSymbol(r.symbol, r.timeframe)}
                  className={`w-full text-left p-3 rounded-lg border ${cfg.bg} ${cfg.border} ${cfg.ring} hover:bg-slate-800/40 transition-colors ${
                    highlightKey === rkey
                      ? 'ring-2 ring-amber-400/80 shadow-lg shadow-amber-400/30 animate-pulse'
                      : ''
                  } ${isObs ? 'opacity-90' : ''}`}
                >
                  <div className="flex items-center gap-3">
                    {/* Tier badge */}
                    <div className={`flex-shrink-0 w-10 h-10 rounded-lg flex items-center justify-center font-black text-base border ${cfg.border} ${cfg.text}`}>
                      {cfg.label}
                    </div>

                    {/* Symbol + direction + op type */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        {/* Origem/veredito: 👁 observação · ⛔ bot recusou (gate) · 🤖 bot opera */}
                        <span
                          title={isObs
                            ? 'OBSERVAÇÃO — o bot NÃO opera essa. Vem do ambiente de testes (universo amplo). Use pra analisar no TradingView e aprender.'
                            : blocked
                              ? `BOT NÃO OPERA — recusada na execução pelo gate "${blocked.gate}": ${blocked.reason}. A recomendação continua válida pra você analisar, mas o bot não abriu por essa razão.`
                              : 'BOT OPERA — passou em todos os gates de execução; está no universo que o bot executa de verdade.'}
                          className={`px-1.5 py-0.5 rounded text-[9px] font-bold border whitespace-nowrap ${
                            isObs
                              ? 'bg-sky-500/15 text-sky-300 border-sky-500/40'
                              : blocked
                                ? 'bg-red-500/15 text-red-300 border-red-500/40'
                                : 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
                          }`}
                        >
                          {isObs ? '👁 OBSERVAÇÃO' : blocked ? `⛔ BOT NÃO OPERA · ${gateLabel(blocked.gate)}` : '🤖 BOT OPERA'}
                        </span>
                        {/* Vetagem de qualidade pra OBSERVAÇÃO: mesma lógica do bot
                            (R:R/P(TP1)/liquidez) aplicada às indicações do universo
                            amplo — diz se o setup atende o padrão que o bot exige. */}
                        {isObs && verdict && (
                          <span
                            title={verdict.ok
                              ? 'CRITÉRIO DO BOT: passa nos gates de qualidade (R:R, P(TP1), liquidez). Mesmo sendo observação, esse setup atende o padrão que o bot exige pra operar.'
                              : `CRITÉRIO DO BOT: NÃO passa — barrado em "${verdict.blocked_by}": ${verdict.reason}.`}
                            className={`px-1.5 py-0.5 rounded text-[9px] font-bold border whitespace-nowrap ${
                              verdict.ok
                                ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
                                : 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                            }`}
                          >
                            {verdict.ok ? '✅ critério do bot' : `⚠ ${gateLabel(verdict.blocked_by ?? '')}`}
                          </span>
                        )}
                        <span className="text-sm font-bold text-white">{symbolBase(r.symbol)}</span>
                        <span className="text-[10px] text-slate-500 font-mono">{r.timeframe}</span>
                        <DirIcon className={`w-3.5 h-3.5 ${dirColor}`} />
                        <span className={`text-xs font-bold ${dirColor}`}>
                          {isLong ? 'LONG' : 'SHORT'}
                        </span>
                        {(() => {
                          const op = operationType(r.timeframe)
                          return (
                            <span
                              title={op.hint}
                              className={`px-1.5 py-0.5 rounded text-[9px] font-bold border ${op.cls}`}
                            >
                              {op.label}
                            </span>
                          )
                        })()}
                        {/* Selo de TF ALTO de origem (12h+): padrão de swing/posição
                            em timeframe relevante — destaque vs ruído de TF baixo. */}
                        {['12h', '1d', '3d', '1w'].includes(r.timeframe) && (
                          <span
                            title={`Setup originado em timeframe ALTO (${r.timeframe.toUpperCase()}). Padrões gráficos em TFs altos são mais confiáveis — rompimentos limpos, menos ruído.`}
                            className="px-1.5 py-0.5 rounded text-[9px] font-bold border bg-indigo-500/15 text-indigo-300 border-indigo-500/40 whitespace-nowrap"
                          >
                            📊 {r.timeframe.toUpperCase()}
                          </span>
                        )}
                        {/* Rompeu + retestando: entrada limpa no pullback à linha
                            rompida (suporte/resistência invertido). */}
                        {r.retest_armed && (
                          <span
                            title="ROMPEU + RETESTANDO — o preço rompeu o nível-chave do padrão e voltou pra retestá-lo (agora como suporte/resistência invertido). É a entrada 'limpa' de pullback pós-rompimento."
                            className="px-1.5 py-0.5 rounded text-[9px] font-bold border bg-teal-500/15 text-teal-300 border-teal-500/40 whitespace-nowrap"
                          >
                            🎯 rompeu+retest
                          </span>
                        )}
                      </div>
                      <p className="text-[11px] text-slate-400 mt-0.5 leading-tight">{r.summary}</p>
                      {/* Edges — sinais de alta convicção (A+/funding/padrão/MTF).
                          O bot usa pra escalar o size; aqui é transparência pro user. */}
                      {r.edge_tags && r.edge_tags.length > 0 && (
                        <div className="flex items-center gap-1 flex-wrap mt-1">
                          <span className="text-[9px] text-violet-400/70 font-semibold">⚡ edges:</span>
                          {r.edge_tags.flatMap(tag => {
                            // 'padrão' genérico → nomeia o(s) padrão(ões) detectado(s)
                            // (OB/FVG/Triângulo/Cunha…) + alvo de rompimento se houver.
                            if (tag === 'padrão') {
                              const chips = patternChips(r)
                              if (chips.length === 0) {
                                const meta = EDGE_TAG_META['padrão']
                                return [(
                                  <span
                                    key="padrão"
                                    title={meta.hint}
                                    className="px-1.5 py-0.5 rounded text-[9px] font-bold border bg-violet-500/15 text-violet-300 border-violet-500/40 whitespace-nowrap"
                                  >
                                    {meta.label}
                                  </span>
                                )]
                              }
                              return chips.map((c, i) => (
                                <span
                                  key={`pat-${i}`}
                                  title={c.hint}
                                  className="px-1.5 py-0.5 rounded text-[9px] font-bold border bg-fuchsia-500/15 text-fuchsia-200 border-fuchsia-500/40 whitespace-nowrap"
                                >
                                  {c.label}
                                </span>
                              ))
                            }
                            const meta = EDGE_TAG_META[tag] ?? { label: tag, hint: 'Sinal de convicção' }
                            return [(
                              <span
                                key={tag}
                                title={meta.hint}
                                className="px-1.5 py-0.5 rounded text-[9px] font-bold border bg-violet-500/15 text-violet-300 border-violet-500/40 whitespace-nowrap"
                              >
                                {meta.label}
                              </span>
                            )]
                          })}
                        </div>
                      )}
                      {r.warnings.length > 0 && (
                        <div className="flex items-center gap-1 mt-1">
                          <AlertTriangle className="w-3 h-3 text-yellow-400 flex-shrink-0" />
                          <span className="text-[10px] text-yellow-300/80 truncate">{r.warnings[0]}</span>
                        </div>
                      )}
                    </div>

                    {/* Score + R:R + leverage + probabilidades */}
                    <div className="flex-shrink-0 text-right">
                      <div
                        className="text-sm font-bold text-white"
                        title="Score V2 (régua recalibrada): índice ponderado de confluência (60%), tendência/ADX (30%) e derivativos (10%). Faixa típica ~15–75 — números menores que o modelo antigo são esperados. Compare pelo TIER e pela P(TP1)%, não pelo valor absoluto. Cortes: A+ ≥65 · A ≥46 · B ≥18."
                      >
                        {r.score.toFixed(0)}
                      </div>
                      <div className="text-[10px] text-slate-500">score V2</div>
                      {/* Veredito de ENTRADA MANUAL — lê o entry_grade do backend
                          (qualidade dos gates + piso de auto-execução SCORE_MIN):
                          distingue "o bot abriria" de "só dá pra entrar manual".
                          Existe pra você não pular setup bom por causa do número
                          "baixo" do Score V2 (A já vale ≥46), mas sem fingir que um
                          setup abaixo do piso de execução é igual aos que o bot abre. */}
                      {(() => {
                        const grade = r.entry_grade
                          ?? (verdict?.ok === false ? 'avoid' : (r.tier === 'A+' || r.tier === 'A') ? 'good' : 'manual')
                        const ev = grade === 'avoid'
                          ? { lbl: '⛔ Evitar', cls: 'bg-red-500/15 text-red-300 border-red-500/40',
                              hint: `Não passa nos gates de qualidade do bot${verdict?.blocked_by ? ` (${verdict.blocked_by})` : ''} — melhor não entrar.` }
                          : grade === 'manual'
                            ? { lbl: '🟡 Dá pra entrar', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/40',
                                hint: 'Passa na qualidade (R:R/P(TP1)/liquidez), mas o Score está abaixo do piso de auto-execução do bot — ele NÃO abriria sozinho. Dá pra entrar manualmente, com cautela.' }
                            : { lbl: '✅ Bom pra entrar', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40',
                                hint: 'Passa nos gates de qualidade E o Score atinge o piso de auto-execução: é o tipo de setup que o próprio bot abre. Ignore o número "baixo" do Score — a régua V2 vai só até ~75.' }
                        return (
                          <div title={ev.hint} className={`mt-1 inline-block px-1.5 py-0.5 rounded text-[9px] font-bold border whitespace-nowrap ${ev.cls}`}>
                            {ev.lbl}
                          </div>
                        )
                      })()}
                      {r.prob_tp1 != null && (
                        <div
                          className="text-[10px] text-sky-300 font-mono mt-0.5"
                          title="P(TP1)/P(TP2) calibradas empiricamente — mapeiam score → win rate observado em snapshots resolvidos (PAV + shrinkage bayesiano). P(TP2) = prob de correr até o TP2 (subconjunto de P(TP1))."
                        >
                          P {(r.prob_tp1 * 100).toFixed(0)}%
                          {r.prob_tp2 != null && (
                            <span className="text-green-300/80"> · TP2 {(r.prob_tp2 * 100).toFixed(0)}%</span>
                          )}
                        </div>
                      )}
                      <div className="text-[10px] text-emerald-300 mt-1 font-mono">{fmtRR(r.risk_reward)}</div>
                      <div className="text-[11px] text-orange-300 mt-0.5 font-mono font-bold">{r.leverage}x</div>
                      {(() => {
                        const p = probs[`${r.tier}|${r.timeframe}|${r.direction}`]
                        if (!p || p.confidence === 'low') return null
                        return (
                          <div
                            className="mt-1 text-[9px] leading-tight"
                            title={`Baseado em ${p.n_total} trades históricos do mesmo bucket (tier/TF/direção). Confiança: ${p.confidence}.`}
                          >
                            <div className="text-emerald-400/80">TP1: <span className="font-mono font-bold">{p.p_tp1_pct.toFixed(0)}%</span></div>
                            <div className="text-green-400/80">TP2: <span className="font-mono font-bold">{p.p_tp2_pct.toFixed(0)}%</span></div>
                          </div>
                        )
                      })()}
                    </div>
                  </div>

                  {/* Entry zone + chase flag */}
                  {(r.entry_zone_low != null && r.entry_zone_high != null && r.entry_zone_type && r.entry_zone_type !== 'market') && (
                    <div className="mt-2 flex items-center gap-2 text-[10px] flex-wrap">
                      <span className="px-2 py-0.5 rounded border border-sky-500/40 bg-sky-500/10 text-sky-300">
                        🎯 zona limit: <span className="font-mono">{fmt(r.entry_zone_low)} – {fmt(r.entry_zone_high)}</span>
                      </span>
                      {r.chase_level === 'chasing' && (
                        <span className="px-2 py-0.5 rounded border border-red-500/40 bg-red-500/10 text-red-300" title={r.warnings[0]}>
                          🔴 já estendido {r.chase_atr}×ATR — aguarde pullback
                        </span>
                      )}
                      {r.chase_level === 'extended' && (
                        <span className="px-2 py-0.5 rounded border border-yellow-500/40 bg-yellow-500/10 text-yellow-300">
                          🟡 {r.chase_atr}×ATR adiantado
                        </span>
                      )}
                      {r.chase_level === 'ok' && (
                        <span className="px-2 py-0.5 rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-300">
                          🟢 entry ainda viável
                        </span>
                      )}
                    </div>
                  )}
                  {/* Levels resumido — agora com TP1 + TP2 */}
                  <div className="grid grid-cols-5 gap-2 mt-2 pt-2 border-t border-slate-800/60 text-[11px]">
                    <div>
                      <div className="text-slate-600">Entrada</div>
                      <div className="font-mono text-yellow-300">{fmt(r.entry)}</div>
                    </div>
                    <div>
                      <div className="text-slate-600">Stop</div>
                      <div className="font-mono text-red-300">{fmt(r.stop_loss)}</div>
                    </div>
                    <div title="50% da posição sai aqui — depois stop sobe pra entrada (breakeven)">
                      <div className="text-slate-600">TP1</div>
                      <div className="font-mono text-emerald-300">{fmt(r.signal?.tp1 ?? r.entry)}</div>
                    </div>
                    <div title="Saída final dos 50% restantes (ou trail por ATR)">
                      <div className="text-slate-600">TP2</div>
                      <div className="font-mono text-green-300">{fmt(r.tp2)}</div>
                    </div>
                    <div title={`Margem ${r.margin_pct}% da banca · risco ${r.risk_pct}% por trade · stop a ${r.stop_distance_pct.toFixed(2)}% do entry`}>
                      <div className="text-slate-600">Alavanc.</div>
                      <div className="font-mono text-orange-300 font-bold">{r.leverage}x</div>
                    </div>
                  </div>

                  {/* Size sugerido — Kelly fracionado × score × volatilidade.
                      Diferente de risk_pct (perda aceitável se stop bater).
                      Esse aqui é o TAMANHO da posição em % da banca. */}
                  {r.suggested_size_pct != null && (
                    <div
                      className="mt-2 flex items-center justify-between gap-2 text-[10px] rounded px-2 py-1 border border-sky-500/30 bg-sky-500/10"
                      title={r.size_rationale ?? ''}
                    >
                      <span className="text-sky-300">
                        💰 Size sugerido <span className="font-mono font-bold">{r.suggested_size_pct.toFixed(2)}%</span> da banca
                      </span>
                      <span className="text-[9px] text-slate-400">Kelly·score·vol</span>
                    </div>
                  )}

                  {/* Preço atual vs entry — mostra delta sempre que houver current_price.
                      Ajuda usuário a julgar se rec ainda é "fresca" ou se preço já fugiu. */}
                  {r.current_price != null && r.entry != null && (() => {
                    const isLong = r.direction === 'long'
                    const delta = r.current_price - r.entry
                    const deltaFav = isLong ? delta : -delta  // positivo = preço já correu a favor
                    const deltaPct = (deltaFav / r.entry) * 100
                    const lvl = r.chase_level  // 'ok' | 'extended' | 'chasing' | null
                    const color =
                      lvl === 'chasing' ? 'text-red-300 border-red-500/30 bg-red-500/10'
                      : lvl === 'extended' ? 'text-yellow-300 border-yellow-500/30 bg-yellow-500/10'
                      : deltaFav < 0 ? 'text-emerald-300 border-emerald-500/30 bg-emerald-500/10'
                      : 'text-slate-300 border-slate-700/50 bg-slate-800/40'
                    const arrow = deltaFav >= 0 ? '↑ a favor' : '↓ abaixo do entry'
                    const atrPart = r.chase_atr != null ? ` · ${r.chase_atr}×ATR` : ''
                    return (
                      <div className={`mt-2 flex items-center justify-between gap-2 text-[10px] rounded px-2 py-1 border ${color}`}>
                        <span>
                          Preço agora <span className="font-mono font-bold">{fmt(r.current_price)}</span>
                          <span className="text-slate-500 ml-1">vs entry {fmt(r.entry)}</span>
                        </span>
                        <span className="font-mono">
                          {deltaFav >= 0 ? '+' : ''}{deltaPct.toFixed(2)}% {arrow}{atrPart}
                        </span>
                      </div>
                    )
                  })()}

                  {/* Badge histórico — aprendizado contínuo */}
                  {hist && hist.trades > 0 && (
                    <div className={`mt-2 flex items-center gap-1.5 text-[10px] rounded px-2 py-1 ${
                      hist.verdict === 'winning' ? 'bg-emerald-500/10 text-emerald-300 border border-emerald-500/30'
                      : hist.verdict === 'losing' ? 'bg-red-500/10 text-red-300 border border-red-500/30'
                      : 'bg-slate-700/30 text-slate-400 border border-slate-700/50'
                    }`}>
                      <Brain className="w-3 h-3" />
                      <span>
                        {hist.verdict === 'winning' && '✓ setup forte: '}
                        {hist.verdict === 'losing' && '⚠ setups similares perderam: '}
                        {(hist.verdict === 'neutro' || hist.verdict === 'amostra_pequena') && 'histórico: '}
                        {hist.win_rate != null && <strong>{hist.win_rate.toFixed(0)}% win</strong>}
                        {hist.avg_r != null && <span className="ml-1">· {hist.avg_r >= 0 ? '+' : ''}{hist.avg_r.toFixed(2)}R médio</span>}
                        <span className="ml-1 text-slate-500">({hist.trades} trades{!hist.sample_ok && ', amostra pequena'})</span>
                      </span>
                    </div>
                  )}
                </button>

                {/* Confirmar entrada manual a partir desta rec */}
                <div className="px-1 pt-1.5">
                  {confirmFor === rkey ? (
                    <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-2 flex flex-col gap-2">
                      <div className="flex items-center gap-2">
                        <input
                          type="number" step="any" placeholder="Preço de entrada *"
                          className="flex-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 placeholder-slate-500"
                          value={confirmForm.entry}
                          onChange={e => setConfirmForm(f => ({ ...f, entry: e.target.value }))}
                        />
                        <input
                          type="number" step="any" placeholder="Qty (auto)"
                          className="w-24 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 placeholder-slate-500"
                          value={confirmForm.qty}
                          onChange={e => setConfirmForm(f => ({ ...f, qty: e.target.value }))}
                        />
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          disabled={confirmBusy}
                          onClick={() => submitConfirm(r, rkey)}
                          className="flex-1 py-1 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 rounded text-xs font-bold text-white"
                        >
                          {confirmBusy ? 'Registrando…' : 'Confirmar entrada'}
                        </button>
                        <button
                          onClick={() => setConfirmFor(null)}
                          className="px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded text-xs text-slate-300"
                        >
                          Cancelar
                        </button>
                      </div>
                      <p className="text-[9px] text-slate-500 leading-snug">
                        Você abre na corretora; o bot coloca SL+TP1+TP2 e gerencia o BE pós-TP1.
                        Níveis herdados da rec. Qty vazio = lê automático da posição na conta.
                      </p>
                    </div>
                  ) : (
                    <button
                      onClick={() => openConfirm(r, rkey)}
                      className="w-full py-1 rounded-lg border border-slate-700 bg-slate-800/40 hover:bg-slate-700/60 text-[11px] font-semibold text-emerald-300 transition-colors"
                    >
                      ✅ Confirmar que entrei nessa
                    </button>
                  )}
                  {confirmToast?.k === rkey && (
                    <div className={`mt-1 text-[10px] px-2 py-1 rounded ${
                      confirmToast.level === 'ok' ? 'text-emerald-300 bg-emerald-500/10' : 'text-red-300 bg-red-500/10'
                    }`}>
                      {confirmToast.msg}
                    </div>
                  )}
                </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Footer legend */}
        <div className="px-4 py-2 border-t border-slate-800 bg-slate-900/60 text-[10px] text-slate-500 leading-relaxed">
          <strong className="text-slate-400">Tiers:</strong>{' '}
          <span className="text-emerald-300">A+</span> {TIER_DESC['A+']} ·{' '}
          <span className="text-green-400">A</span> {TIER_DESC['A']} ·{' '}
          <span className="text-blue-400">B</span> {TIER_DESC['B']}
          <br />
          <strong className="text-slate-400">Score V2:</strong> régua recalibrada (faixa típica ~15–75; cortes A+ ≥65 · A ≥46 · B ≥18). Valores são menores que o modelo antigo — guie-se pelo tier e pela P(TP1)%, não pelo número absoluto.
          <br />
          Atualiza automaticamente a cada 2 min · Cache backend 90s
          <br />
          <strong className="text-slate-400">Alavancagem:</strong> dimensionada para 10% da banca em margem · risco por trade A+ 1.5% / A 1% / B 0.5%. Quanto mais perto o stop, mais leverage cabe (calculado individualmente por setup).
        </div>
      </div>
    </div>
  )
}
